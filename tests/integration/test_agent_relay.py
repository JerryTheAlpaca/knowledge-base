"""A 机 ↔ agent 容器的身份与回传边界（docs/27 §Phase 3）。

这里测的是三条容易做错、做错了又不容易发现的事：

1. **两边签出的凭据必须互认可**：容器侧那份独立实现（`deploy/agent/orchestrator/
   relaykey.py`）与 A 侧 `security/agent_relay.py` 要逐字节一致，否则换机器部署时
   才发现签验不上；
2. **`site` 与 `user_id` 只从凭据取**：请求体里写什么都没用，别的站点签的凭据
   在这里一律不认；
3. **回传是幂等的**：同一 `(session_id, seq)` 重发不产生第二条事件，容器重启与
   断连补传都靠这一点。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kbserver.app import create_app
from kbserver.config import get_settings
from kbserver.domain.provider_select import pick_profile
from kbserver.models import AgentEvent, AgentSession, Credential, ProviderProfile, User
from kbserver.security import credentials as cred_crypto
from kbserver.security.agent_relay import (
    SUB_INGEST,
    SUB_USER,
    issue_relay_token,
    read_relay_token,
    relay_signing_key,
)

# 容器侧那份实现是独立包，测试里显式把它请进来做互认比对
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy" / "agent"))
from orchestrator import relaykey  # noqa: E402


@pytest.fixture()
def relay_env(session_factory, monkeypatch):
    monkeypatch.setenv("AGENT_ENABLED", "1")
    monkeypatch.setenv("AGENT_BASE_URL", "http://agent-unreachable:8100")
    with session_factory() as db:
        user = User(name="中继用户")
        db.add(user)
        db.flush()
        profile = ProviderProfile(user_id=user.id, kind="llm", adapter="openai-compatible",
                                  endpoint="https://api.deepseek.com/v1", model="deepseek-chat",
                                  capabilities_json={})
        db.add(profile)
        db.flush()
        envelope = cred_crypto.encrypt_secret("sk-relay-test", get_settings().load_master_key(),
                                              user_id=user.id, profile_id=profile.id,
                                              credential_version=1)
        db.add(Credential(user_id=user.id, profile_id=profile.id, master_key_version=1,
                          **envelope))
        db.commit()
        ids = {"user_id": user.id, "profile_id": profile.id}
    with TestClient(create_app()) as client:
        yield {"client": client, "db": session_factory, **ids}


def _headers(key: bytes, *, subject: str, site: str = "kb", user_id: str = "u") -> dict:
    token, _ = issue_relay_token(key, site=site, user_id=user_id, subject=subject,
                                 ttl_seconds=300)
    return {"Authorization": f"Bearer {token}"}


def test_container_side_token_is_accepted_by_a_side():
    """两侧独立实现必须逐字节一致：容器签的回传凭据，A 机要能验。"""
    key = relay_signing_key(get_settings().load_master_key())
    token, _ = relaykey.issue(key, site="kb", user_id="u42", subject=relaykey.SUB_INGEST,
                               ttl_seconds=120)
    claims = read_relay_token(key, token, expect_subject=SUB_INGEST)
    assert claims is not None and claims["user_id"] == "u42" and claims["site"] == "kb"
    # 用途不能互换：回传凭据当用户凭据用要失败
    assert read_relay_token(key, token, expect_subject=SUB_USER) is None
    # 反过来也一样：A 机签的用户凭据过不了回传那道门
    user_token, _ = issue_relay_token(key, site="kb", user_id="u42", subject=SUB_USER,
                                      ttl_seconds=120)
    assert relaykey.read(key, user_token, expect_subject=relaykey.SUB_INGEST) is None


def test_ingest_writes_events_idempotently(relay_env):
    client, key = relay_env["client"], relay_signing_key(get_settings().load_master_key())
    user_id = relay_env["user_id"]
    headers = {"Authorization": f"Bearer {issue_relay_token(key, site='kb', user_id=user_id, subject=SUB_INGEST, ttl_seconds=300)[0]}"}

    created = client.post("/v1/agent/internal/sessions", json={"session_id": "sess-1",
                                                              "title": "第一句"},
                          headers=headers)
    assert created.status_code == 200, created.text

    events = {"session_id": "sess-1", "events": [
        {"seq": 1, "kind": "user_message", "payload": {"text": "你好"}},
        {"seq": 2, "kind": "assistant_message", "payload": {"text": "在的"}},
    ]}
    first = client.post("/v1/agent/internal/events", json=events, headers=headers)
    assert first.status_code == 200 and first.json()["accepted"] == 2
    # 断线补传：同一批原样重发，不该多出第二条，也不该把 last_seq 倒着推
    again = client.post("/v1/agent/internal/events", json=events, headers=headers)
    assert again.status_code == 200 and again.json()["accepted"] == 0
    assert again.json()["last_seq"] == 2

    with relay_env["db"]() as db:
        rows = db.query(AgentEvent).filter(AgentEvent.session_id == "sess-1").all()
        assert len(rows) == 2
        session = db.query(AgentSession).filter(AgentSession.session_id == "sess-1").one()
        assert session.last_seq == 2 and session.user_id == user_id


def test_ingest_cannot_write_into_another_users_session(relay_env):
    key = relay_signing_key(get_settings().load_master_key())
    victim = relay_env["user_id"]
    attacker = "someone-else"
    headers = {"Authorization": f"Bearer {issue_relay_token(key, site='kb', user_id=attacker, subject=SUB_INGEST, ttl_seconds=300)[0]}"}
    client = relay_env["client"]

    # 先由真正的用户登记会话
    owner_headers = {"Authorization": f"Bearer {issue_relay_token(key, site='kb', user_id=victim, subject=SUB_INGEST, ttl_seconds=300)[0]}"}
    assert client.post("/v1/agent/internal/sessions", json={"session_id": "sess-own",
                                                            "title": "t"},
                       headers=owner_headers).status_code == 200

    # 攻击者凭据想往同一个会话里塞事件：要么 403（会话已归属别人），要么 404（没登记）
    probe = client.post("/v1/agent/internal/events",
                        json={"session_id": "sess-own",
                              "events": [{"seq": 1, "kind": "user_message", "payload": {"text": "串一个"}}]},
                        headers=headers)
    assert probe.status_code in (403, 404), probe.text
    with relay_env["db"]() as db:
        assert db.query(AgentEvent).filter(AgentEvent.session_id == "sess-own").count() == 0


def test_ingest_rejects_other_site_credentials(relay_env):
    key = relay_signing_key(get_settings().load_master_key())
    headers = {"Authorization": f"Bearer {issue_relay_token(key, site='ledger', user_id=relay_env['user_id'], subject=SUB_INGEST, ttl_seconds=300)[0]}"}
    resp = relay_env["client"].post("/v1/agent/internal/sessions",
                                    json={"session_id": "sess-x", "title": "t"}, headers=headers)
    assert resp.status_code == 403
    # 缺凭据与乱码凭据都进不来
    assert relay_env["client"].post("/v1/agent/internal/sessions",
                                    json={"session_id": "sess-x"}).status_code == 401
    assert relay_env["client"].post("/v1/agent/internal/events",
                                    headers={"Authorization": "Bearer junk.junk"},
                                    json={"session_id": "sess-x", "events": []}).status_code == 401


def test_llm_token_picks_the_same_profile_as_the_web_conversation(relay_env):
    """中继给出的 profile 必须与网页对话同一条选择规则，否则两处会用上不同 Key。"""
    key = relay_signing_key(get_settings().load_master_key())
    headers = {"Authorization": f"Bearer {issue_relay_token(key, site='kb', user_id=relay_env['user_id'], subject=SUB_INGEST, ttl_seconds=300)[0]}"}
    resp = relay_env["client"].post("/v1/agent/internal/llm-token", json={}, headers=headers)
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    with relay_env["db"]() as db:
        picked = pick_profile(db, relay_env["user_id"], None)
    assert picked is not None
    assert payload["profile_id"] == picked[0].id
    assert payload["model"] == picked[0].model
    assert payload["base_url"].endswith("/v1/llm")
    assert payload["token"] and payload["expires_at"]

    # 别的账号要不到 token
    other = {"Authorization": f"Bearer {issue_relay_token(key, site='kb', user_id='ghost', subject=SUB_INGEST, ttl_seconds=300)[0]}"}
    assert relay_env["client"].post("/v1/agent/internal/llm-token", json={},
                                    headers=other).status_code == 422


def test_relay_gives_clear_error_when_container_unreachable(relay_env):
    """容器不可达时对话入口给明确错误，不能把收件箱其余功能一起拖死。"""
    client = relay_env["client"]
    # 设备 Token 通道即可：中继只要求一个已认证主体，不区分是从网页还是插件进来
    with relay_env["db"]() as db:
        from kbserver.models import Device
        from kbserver.security.tokens import WEB_SCOPES, issue_token
        device = Device(user_id=relay_env["user_id"], kind="desktop", name="中继设备")
        db.add(device)
        db.flush()
        raw, token = issue_token(relay_env["user_id"], device.id, WEB_SCOPES)
        db.add(token)
        db.commit()
        auth = {"Authorization": f"Bearer {raw}"}

    resp = client.get("/v1/agent/sessions", headers=auth)
    # 列表退回 A 机镜像并如实标注 degraded，不是 500、也不是空壳成功
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"sessions": [], "degraded": True}

    created = client.post("/v1/agent/sessions", json={"title": "打不通"}, headers=auth)
    assert created.status_code == 503
    assert "对话服务" in created.text or "AGENT_UNAVAILABLE" in created.text

    # 收件箱本身完全不受影响
    assert client.get("/v1/items", headers=auth).status_code == 200
