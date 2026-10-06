"""LLM 代理验收（docs/27 Phase 2）。

最要紧的一条是 **tool_calls 不被吞**：agent 循环靠它，而 `generate_conversation()`
在 `content` 为空时直接抛可重试错误。所以这里既测「透传形状对不对」，也测
「Key 只在一次请求里出现」和「预算超限根本不转发」。
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from kbserver.api import routes_llm_proxy
from kbserver.app import create_app
from kbserver.models import AgentBudget, Credential, ProviderProfile, User, utcnow
from kbserver.security import credentials as cred_crypto
from kbserver.security.agent_llm_tokens import issue_llm_token, read_llm_token

SECRET_A = "sk-secret-A-very-long-enough"
SECRET_B = "sk-secret-B-very-long-enough"


class _SseStream(httpx.AsyncByteStream):
    """一段真·异步流：让代理走 aiter_raw 的原样透传分支。"""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    async def __aiter__(self):
        half = len(self.payload) // 2
        for piece in (self.payload[:half], self.payload[half:]):
            yield piece


def _make_llm_user(db, name: str, secret: str) -> dict:
    user = User(name=name)
    db.add(user)
    db.flush()
    profile = ProviderProfile(user_id=user.id, kind="llm", adapter="openai-compatible",
                              endpoint="http://provider.test/v1", model="real-model",
                              capabilities_json={"api_protocol": "openai-chat",
                                                 "cache_mode": "prompt_cache_key",
                                                 "cache_retention": "24h"})
    db.add(profile)
    db.flush()
    envelope = cred_crypto.encrypt_secret(secret, get_master_key(), user_id=user.id,
                                          profile_id=profile.id, credential_version=1)
    db.add(Credential(user_id=user.id, profile_id=profile.id, master_key_version=1, **envelope))
    return {"user_id": user.id, "profile_id": profile.id}


def get_master_key() -> bytes:
    from kbserver.config import get_settings

    return get_settings().load_master_key()


@pytest.fixture()
def proxy_env(session_factory, monkeypatch):
    monkeypatch.setenv("AGENT_ENABLED", "1")
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append({"headers": dict(request.headers), "body": body,
                     "url": str(request.url)})
        if body.get("_echo_status"):
            return httpx.Response(int(body["_echo_status"]), json={"error": {"message": "nope"}})
        if body.get("stream"):
            payload = (
                b'data: {"id":"1","choices":[{"index":0,"delta":{"role":"assistant",'
                b'"reasoning_content":"\xe7\xa7\x81\xe5\xaf\x86\xe6\x80\x9d\xe8\x80\x83"}}]}\n\n'
                b'data: {"id":"1","choices":[{"index":0,"delta":{"content":"\xe4\xbd\xa0\xe5\xa5\xbd"},'
                b'"finish_reason":"stop"}],"usage":{"prompt_tokens":11,"completion_tokens":7,'
                b'"total_tokens":18}}\n\ndata: [DONE]\n\n'
            )
            # 必须是真流：MockTransport 给缓冲 content 的话 httpx 会当成已读完，
            # 代理的 aiter_raw 分支就测不到了
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=_SseStream(payload))
        return httpx.Response(200, json={
            "id": "chatcmpl-1", "model": body.get("model"),
            "choices": [{"index": 0, "finish_reason": "tool_calls",
                         "message": {"role": "assistant", "content": None,
                                     "tool_calls": [{"id": "c1", "type": "function",
                                                     "function": {"name": "mcp__kb__kb_list_items",
                                                                  "arguments": "{}"}}]}}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 8, "total_tokens": 48},
        })

    monkeypatch.setattr(routes_llm_proxy, "_TRANSPORT", httpx.MockTransport(handler))
    with session_factory() as db:
        a = _make_llm_user(db, "代理用户A", SECRET_A)
        b = _make_llm_user(db, "代理用户B", SECRET_B)
        db.commit()
    with TestClient(create_app()) as client:
        yield {"client": client, "a": a, "b": b, "seen": seen, "factory": session_factory}


def token_for(user_id: str, profile_id: str) -> str:
    raw, _ = issue_llm_token(get_master_key(), site="kb", user_id=user_id, profile_id=profile_id)
    return raw


def call(client: TestClient, token: str, body: dict, **kwargs: Any):
    return client.post("/v1/llm/chat/completions",
                       headers={"Authorization": f"Bearer {token}"},
                       json=body, **kwargs)


def test_proxy_forwards_tool_calls_verbatim_and_swaps_key(proxy_env):
    client, a, seen = proxy_env["client"], proxy_env["a"], proxy_env["seen"]
    resp = call(client, token_for(a["user_id"], a["profile_id"]), {
        "model": "客户端乱填的模型",
        "messages": [{"role": "user", "content": "列出我的条目"}],
        "tools": [{"type": "function", "function": {"name": "mcp__kb__kb_list_items",
                                                    "description": "…"}}],
        "tool_choice": "auto",
    })
    assert resp.status_code == 200, resp.text
    sent = seen[-1]
    # Key 换成该用户自己托管的那份，且发到 profile 的 endpoint
    assert sent["headers"]["authorization"] == f"Bearer {SECRET_A}"
    assert sent["url"].startswith("http://provider.test/v1")
    # 客户端给的 model 一律不算：强制用解析出的配置的 model
    assert sent["body"]["model"] == "real-model"
    # tools / tool_choice 原样带过去——这正是 generate_conversation() 会丢掉的部分
    assert sent["body"]["tools"][0]["function"]["name"] == "mcp__kb__kb_list_items"
    assert sent["body"]["tool_choice"] == "auto"

    got = resp.json()
    assert got["choices"][0]["message"]["tool_calls"][0]["function"]["name"] \
        == "mcp__kb__kb_list_items"
    assert got["choices"][0]["message"]["content"] is None
    assert got["choices"][0]["finish_reason"] == "tool_calls"


def test_proxy_usage_is_billed_to_the_right_bucket(proxy_env):
    client, a = proxy_env["client"], proxy_env["a"]
    call(client, token_for(a["user_id"], a["profile_id"]),
         {"messages": [{"role": "user", "content": "hi"}]})
    with proxy_env["factory"]() as db:
        row = db.query(AgentBudget).filter(AgentBudget.user_id == a["user_id"],
                                           AgentBudget.profile_id == a["profile_id"]).one()
        assert row.requests_used == 1
        assert row.input_tokens_used == 40 and row.output_tokens_used == 8
        # 归属只可能来自 token：别的用户那格不会被记上
        assert row.period == utcnow().strftime("%Y-%m-%d")


def test_proxy_stream_passes_wire_extensions_through(proxy_env):
    client, a, seen = proxy_env["client"], proxy_env["a"], proxy_env["seen"]
    resp = call(client, token_for(a["user_id"], a["profile_id"]),
                {"messages": [{"role": "user", "content": "hi"}], "stream": True})
    assert resp.status_code == 200
    text = resp.text
    # reasoning_content 这类扩展不在任何字段清单里，靠原样透传才不会掉
    assert "reasoning_content" in text
    assert "data: [DONE]" in text
    assert seen[-1]["headers"]["accept"] == "text/event-stream"
    with proxy_env["factory"]() as db:
        row = db.query(AgentBudget).filter(AgentBudget.user_id == a["user_id"]).one()
        assert row.input_tokens_used == 11 and row.output_tokens_used == 7


def test_proxy_rejects_another_users_profile_with_403(proxy_env):
    client, a, b = proxy_env["client"], proxy_env["a"], proxy_env["b"]
    resp = call(client, token_for(a["user_id"], b["profile_id"]),
                {"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 403
    assert not proxy_env["seen"], "越权请求根本不该发到供应商"


def test_proxy_expired_and_tampered_tokens_are_401(proxy_env):
    client, a = proxy_env["client"], proxy_env["a"]
    body = {"messages": [{"role": "user", "content": "hi"}]}
    resp = call(client, token_for(a["user_id"], a["profile_id"])[:-4] + "AAAA", body)
    assert resp.status_code == 401
    resp = call(client, "not-a-token", body)
    assert resp.status_code == 401
    assert read_llm_token(get_master_key(), "junk") is None


def test_provider_auth_failure_is_not_logged_with_the_key(proxy_env):
    client, a = proxy_env["client"], proxy_env["a"]
    resp = call(client, token_for(a["user_id"], a["profile_id"]),
                {"messages": [{"role": "user", "content": "hi"}], "_echo_status": 401})
    assert resp.status_code == 401
    assert SECRET_A not in resp.text
    assert "PROVIDER_AUTH_FAILED" in resp.text
    # 上游正文也不原样回传（里面常有账号 ID 与请求号）
    assert "nope" not in resp.text


def test_budget_exhausted_does_not_forward(proxy_env, monkeypatch):
    monkeypatch.setenv("AGENT_BUDGET_REQUESTS_PER_DAY", "1")
    client, a, seen = proxy_env["client"], proxy_env["a"], proxy_env["seen"]
    body = {"messages": [{"role": "user", "content": "hi"}]}
    assert call(client, token_for(a["user_id"], a["profile_id"]), body).status_code == 200
    second = call(client, token_for(a["user_id"], a["profile_id"]), body)
    assert second.status_code == 429
    assert "BUDGET_EXHAUSTED" in second.text
    assert len(seen) == 1, "超限之后不能再向供应商发请求"


def test_concurrency_gate_returns_503_with_retry_after(proxy_env, monkeypatch):
    client, a = proxy_env["client"], proxy_env["a"]
    monkeypatch.setattr(routes_llm_proxy._gates, "acquire", lambda user_id: False)
    resp = call(client, token_for(a["user_id"], a["profile_id"]),
                {"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 503
    assert resp.headers.get("retry-after")
    assert "RATE_LIMITED" in resp.text


def test_proxy_disabled_when_agent_off(session_factory, monkeypatch):
    monkeypatch.setenv("AGENT_ENABLED", "0")
    with TestClient(create_app()) as client:
        resp = client.post("/v1/llm/chat/completions",
                           headers={"Authorization": "Bearer whatever.whatever"},
                           json={"messages": [{"role": "user", "content": "hi"}]})
        assert resp.status_code == 403
        assert "AGENT_DISABLED" in resp.text
