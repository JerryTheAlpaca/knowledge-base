"""管理员接口与采集开关的集成测试。

- /v1/admin/asr-overview：管理员守卫 + 跨用户聚合（计数/累计分钟，不含文件名）。
- /v1/admin/server-stats：形状与降级（非 Linux 指标为 None）。
- /v1/admin/invitations*：中心站点代理（Cookie/Origin 转发、信封展开）。
- 采集 include_asr：网页/公众号条目提取完成后自动排队转写；B 站不受该开关
  影响（沿用「无字幕自动转写」设置），上传录音始终自动转写（transcribe_audio 意图）。
- 设置 auto_enrich：关闭时提取完成停在 extracted，不自动入 enrich；手动 reprocess 不受影响。

中心认证与上游代理均用假替身，不触网。
"""
from __future__ import annotations

from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.conftest import auth

from kbserver.app import create_app
from kbserver.extractors import bilibili as bili  # noqa: F401  (asr_env 替身依赖此模块)
from kbserver.models import AsrRun, Capture, Item, User
from kbserver.workers import worker

# 复用 test_asr 的 ASR 假环境夹具（部署开关/假模型/假 FFmpeg/假流）
from tests.integration.test_asr import asr_env  # noqa: F401

AUTH_COOKIE = "test_session"
BV = "BV1xxASRTest"


# ---- 夹具（与 test_web_inbox 同款的中心会话替身） ----

@pytest.fixture()
def central(monkeypatch):
    monkeypatch.setenv("AUTH_COOKIE_NAME", AUTH_COOKIE)
    monkeypatch.setenv("AUTH_SESSION_URL", "https://auth.example.com/api/auth/session")
    monkeypatch.setenv("AUTH_LOGIN_URL", "https://auth.example.com/login")
    monkeypatch.setenv("AUTH_LOGOUT_URL", "https://auth.example.com/api/auth/logout")
    monkeypatch.setenv("AUTH_COOKIE_DOMAIN", "")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://testserver")

    state = {"valid": True, "user": {"id": "central-uid-0001", "username": "老账号", "role": "admin"}}

    def fake_validate(cookie_value):
        if not state["valid"] or cookie_value != "fake-central-cookie":
            from kbserver.security import central_auth

            raise central_auth.CentralAuthRejected("中心会话无效或已过期")
        return {"user": dict(state["user"]), "expiresAt": "2026-09-09T00:00:00Z"}, None

    monkeypatch.setattr("kbserver.security.central_auth.validate_central_session", fake_validate)
    return state


@pytest.fixture()
def wc(engine):
    """独立 TestClient：Cookie 不与其他测试串扰；依赖 engine 保证建表。"""
    with TestClient(create_app()) as c:
        yield c


def _login(wc):
    wc.cookies.set(AUTH_COOKIE, "fake-central-cookie")
    return wc.get("/v1/auth/me")


@pytest.fixture()
def fresh_queue(session_factory):
    with session_factory() as db:
        for j in db.query(worker.Job).filter(worker.Job.state.in_(("queued", "retry_wait"))).all():
            j.state = "cancelled"
        db.commit()
    return session_factory


def _session_factory():
    from kbserver.db import get_session_factory

    return get_session_factory()


# ---- /v1/admin/asr-overview ----

def _seed_run(db, user_id, item_id, *, state, processed_seconds, chunk_count=0, done=0, tag="a"):
    db.add(AsrRun(user_id=user_id, item_id=item_id, source_revision=1,
                  recipe_hash=f"recipe-{tag}", model_alias="sense_voice", model_id="m",
                  state=state, processed_seconds=processed_seconds,
                  chunk_count=chunk_count, next_chunk_index=done))


def _seed_item(db, user_id, key: str) -> str:
    capture = Capture(user_id=user_id, client_capture_id=key, request_hash=key,
                      input_json={}, received_at=datetime.now())
    db.add(capture)
    db.flush()
    item = Item(user_id=user_id, capture_id=capture.id, pipeline_state="queued")
    db.add(item)
    db.flush()
    return item.id


def test_admin_endpoints_require_admin(client, user_a):
    r = client.get("/v1/admin/asr-overview", headers=auth(user_a["desktop"]["token"]))
    assert r.status_code == 403
    r = client.get("/v1/admin/server-stats", headers=auth(user_a["desktop"]["token"]))
    assert r.status_code == 403
    r = client.get("/v1/admin/invitations", headers=auth(user_a["desktop"]["token"]))
    assert r.status_code == 403


def test_asr_overview_aggregates_without_filenames(wc, central, db, user_a):
    assert _login(wc).status_code == 200
    uploader = User(name="录音用户A")  # 提交转写的普通用户（与查看总览的管理员不同人）
    db.add(uploader)
    db.flush()
    item_id = _seed_item(db, uploader.id, "admin-ov-1")
    _seed_run(db, uploader.id, item_id, state="succeeded", processed_seconds=600.0, tag="done")
    _seed_run(db, uploader.id, item_id, state="transcribing", processed_seconds=120.0,
              chunk_count=10, done=3, tag="live")
    db.commit()

    r = wc.get("/v1/admin/asr-overview")
    assert r.status_code == 200
    doc = r.json()
    assert doc["totals"]["submitted_users"] >= 1
    assert doc["totals"]["transcribing_runs"] >= 1
    entry = next(u for u in doc["users"] if u["user_id"] == uploader.id)
    assert entry["username"] == "录音用户A"
    assert entry["total_runs"] == 2
    assert entry["active_runs"] == 1
    # 累计分钟 = (600 + 120) / 60 = 12.0；进行中条目只给状态与段数，不给文件名
    assert entry["processed_minutes"] == 12.0
    assert entry["active"][0]["state"] == "transcribing"
    assert entry["active"][0]["done_chunks"] == 3 and entry["active"][0]["chunk_count"] == 10
    assert all("file" not in k and "name" not in k and "title" not in k for k in entry["active"][0])


def test_server_stats_shape(wc, central):
    assert _login(wc).status_code == 200
    r = wc.get("/v1/admin/server-stats")
    assert r.status_code == 200
    doc = r.json()
    assert set(doc) == {"cpu_percent", "memory", "disk"}
    for key in ("cpu_percent", "memory", "disk"):
        assert doc[key] is None or isinstance(doc[key], (float, int, dict))


# ---- /v1/admin/invitations 代理 ----

def _fake_upstream(monkeypatch, *, status=200, payload=None, calls=None):
    calls = calls if calls is not None else []

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, "cookies": kwargs.get("cookies"),
                      "headers": kwargs.get("headers"), "json": kwargs.get("json")})
        if status >= 400:
            return httpx.Response(status, json={"error": {"code": "FORBIDDEN", "message": "中心拒绝"}})
        return httpx.Response(status, json={"data": payload})

    monkeypatch.setattr("kbserver.api.routes_admin.httpx.request", fake_request)
    return calls


def test_invitations_list_proxies_with_admin_cookie(wc, central, monkeypatch):
    assert _login(wc).status_code == 200
    calls = _fake_upstream(monkeypatch, payload={"invitations": [
        {"id": "inv-1", "suffix": "ABCD", "status": "used", "usedByUsername": "老账号"}]})
    r = wc.get("/v1/admin/invitations")
    assert r.status_code == 200
    assert r.json()["invitations"][0]["suffix"] == "ABCD"
    call = calls[0]
    assert call["method"] == "GET" and call["url"].startswith("https://auth.example.com/api/invitations")
    assert call["cookies"] == {AUTH_COOKIE: "fake-central-cookie"}


def test_invitation_create_proxies_post_with_origin(wc, central, monkeypatch):
    assert _login(wc).status_code == 200
    csrf = wc.cookies.get("kb_csrf")
    assert csrf
    calls = _fake_upstream(monkeypatch, payload={"id": "inv-2", "code": "JLI-FULLCODE", "suffix": "CODE"})
    r = wc.post("/v1/admin/invitations", json={}, headers={
        "X-CSRF-Token": csrf, "Origin": "http://testserver"})
    assert r.status_code == 200
    assert r.json()["code"] == "JLI-FULLCODE"  # 完整码透传，只在创建响应出现
    call = calls[0]
    assert call["method"] == "POST"
    assert call["headers"].get("Origin") == "http://testserver"  # 中心白名单已含 KB 站点


def test_invitation_upstream_rejection_maps_status(wc, central, monkeypatch):
    assert _login(wc).status_code == 200
    _fake_upstream(monkeypatch, status=403)
    r = wc.get("/v1/admin/invitations")
    assert r.status_code == 403
    body = r.json()["error"]
    assert body["code"] == "FORBIDDEN" and "中心拒绝" in body["message"]


# ---- 采集 include_asr ----

def test_capture_include_asr_queues_run_for_webpage(client, user_a, fresh_queue, monkeypatch):
    """勾选「提取音轨」：网页/公众号条目提取完成后自动排队转写。"""
    from kbserver.extractors import fetch_base
    from tests.integration.test_m4_web import FakeWebNet, GENERIC_HTML, GENERIC_URL

    monkeypatch.setenv("ASR_ENABLED", "true")
    net = FakeWebNet(pages={GENERIC_URL: (200, "text/html", GENERIC_HTML)})
    monkeypatch.setattr(fetch_base, "safe_fetch", net)
    r = client.post(
        "/v1/captures",
        json={"client_capture_id": "asrflag-1111-2222-3333-444444444444",
              "input_kind": "url", "original_url": GENERIC_URL,
              "include_asr": True},
        headers={**auth(user_a["phone"]["token"]), "Idempotency-Key": "asrflag1"},
    )
    assert r.status_code == 202
    item_id = r.json()["item_id"]
    assert worker.run_once(_session_factory())  # extract：网页适配器发布后直通转写
    with _session_factory()() as db:
        run = db.query(AsrRun).filter(AsrRun.item_id == item_id).one_or_none()
        assert run is not None and run.requested_by == "manual"
        assert db.query(worker.Job).filter(
            worker.Job.item_id == item_id, worker.Job.stage == "asr_prepare").count() == 1


def test_bilibili_ignores_include_asr(client, user_a, asr_env, fresh_queue):
    """B 站不受「提取音轨」开关影响：无字幕且用户开关未开 → 照旧停在待补充。"""
    asr_env.install()  # extract 一律 no_track
    r = client.post(
        "/v1/captures",
        json={"client_capture_id": "asrnoB-1111-2222-3333-444444444444",
              "input_kind": "url", "original_url": f"https://www.bilibili.com/video/{BV}/",
              "include_asr": True},
        headers={**auth(user_a["phone"]["token"]), "Idempotency-Key": "asrnoB1"},
    )
    assert r.status_code == 202
    item_id = r.json()["item_id"]
    assert worker.run_once(_session_factory())
    with _session_factory()() as db:
        assert db.query(AsrRun).filter(AsrRun.item_id == item_id).count() == 0
        it = db.get(worker.Item, item_id)
        assert it.pipeline_state == "needs_input"


# ---- 设置 auto_enrich ----

def test_settings_auto_enrich_roundtrip(client, user_a):
    r = client.get("/v1/settings", headers=auth(user_a["desktop"]["token"]))
    assert r.status_code == 200 and r.json()["auto_enrich"] is True  # 默认开
    r = client.patch("/v1/settings", json={"auto_enrich": False},
                     headers=auth(user_a["desktop"]["token"]))
    assert r.status_code == 200 and r.json()["auto_enrich"] is False
    r = client.get("/v1/settings", headers=auth(user_a["desktop"]["token"]))
    assert r.json()["auto_enrich"] is False


def test_settings_ai_paragraphing_roundtrip(client, user_a):
    r = client.get("/v1/settings", headers=auth(user_a["desktop"]["token"]))
    assert r.status_code == 200 and r.json()["ai_paragraphing"] is True  # 默认开
    r = client.patch("/v1/settings", json={"ai_paragraphing": False},
                     headers=auth(user_a["desktop"]["token"]))
    assert r.status_code == 200 and r.json()["ai_paragraphing"] is False
    r = client.get("/v1/settings", headers=auth(user_a["desktop"]["token"]))
    assert r.json()["ai_paragraphing"] is False


def test_auto_enrich_off_stops_after_extract(client, user_a, fresh_queue):
    client.patch("/v1/settings", json={"auto_enrich": False},
                 headers=auth(user_a["desktop"]["token"]))
    r = client.post(
        "/v1/captures",
        json={"client_capture_id": "enrichoff-1111-2222-3333-444444444444",
              "input_kind": "text", "text": "第一段正文。\n第二段正文。"},
        headers={**auth(user_a["phone"]["token"]), "Idempotency-Key": "enrichoff1"},
    )
    assert r.status_code == 202
    item_id = r.json()["item_id"]
    assert worker.run_once(_session_factory())  # 领取并执行 extract
    with _session_factory()() as db:
        it = db.get(worker.Item, item_id)
        assert it.pipeline_state == "extracted"
        assert db.query(worker.Job).filter(
            worker.Job.item_id == item_id, worker.Job.stage == "enrich").count() == 0

    # 手动「开始加工」不受开关影响：reprocess 直接入 enrich
    r2 = client.post(f"/v1/items/{item_id}/reprocess", json={},
                     headers=auth(user_a["desktop"]["token"]))
    assert r2.status_code == 202
    with _session_factory()() as db:
        assert db.query(worker.Job).filter(
            worker.Job.item_id == item_id, worker.Job.stage == "enrich").count() == 1


def test_auto_enrich_default_enqueues_enrich(client, user_a, fresh_queue):
    r = client.post(
        "/v1/captures",
        json={"client_capture_id": "enrichon-1111-2222-3333-444444444444",
              "input_kind": "text", "text": "默认开启时的正文。"},
        headers={**auth(user_a["phone"]["token"]), "Idempotency-Key": "enrichon1"},
    )
    item_id = r.json()["item_id"]
    assert worker.run_once(_session_factory())  # extract → 自动入 enrich
    with _session_factory()() as db:
        it = db.get(worker.Item, item_id)
        assert it.pipeline_state in ("enriching", "queued", "waiting_key")
        assert db.query(worker.Job).filter(
            worker.Job.item_id == item_id, worker.Job.stage == "enrich").count() == 1
