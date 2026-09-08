"""Web 收件箱集成测试：配对码换会话、Cookie 认证、CSRF/Origin 校验、登出、
refetch 限频与内容无变化不新增版本（docs/02 §9.1、§10.1）。

不包含真实 Cookie/Key/私人内容；网页响应用合成数据。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kbserver.app import create_app
from kbserver.security.tokens import WEB_SCOPES, issue_pairing_code

from tests.conftest import auth


@pytest.fixture()
def web_pairing(db, user_a):
    raw, code = issue_pairing_code(user_a["user_id"], "web", WEB_SCOPES)
    db.add(code)
    db.commit()
    return raw


@pytest.fixture()
def desktop_pairing(db, user_a):
    raw, code = issue_pairing_code(user_a["user_id"], "desktop", WEB_SCOPES)
    db.add(code)
    db.commit()
    return raw


@pytest.fixture()
def wc():
    """独立 TestClient：Cookie 不与其他测试串扰。"""
    with TestClient(create_app()) as c:
        yield c


def _login(wc, code: str):
    return wc.post("/v1/web/session", json={"code": code, "device_name": "测试收件箱"})


def test_login_sets_session_cookies(wc, web_pairing):
    r = _login(wc, web_pairing)
    assert r.status_code == 200
    body = r.json()
    assert body["csrf_token"]
    assert body["expires_at"]

    set_cookies = r.headers.get_list("set-cookie")
    session = next(c for c in set_cookies if c.startswith("kb_session="))
    assert "httponly" in session.lower()
    assert "samesite=strict" in session.lower()
    assert any(c.startswith("kb_csrf=") for c in set_cookies)

    # Cookie 认证可用：无 Bearer 头列出条目
    r2 = wc.get("/v1/items")
    assert r2.status_code == 200


def test_login_rejects_desktop_pairing_code(wc, desktop_pairing):
    r = _login(wc, desktop_pairing)
    assert r.status_code == 403


def test_login_rejects_bad_code(wc):
    assert wc.post("/v1/web/session", json={"code": "KBP-00000000-00000000",
                                            "device_name": "x"}).status_code == 403


def test_cookie_write_requires_csrf_and_origin(wc, web_pairing):
    _login(wc, web_pairing)
    csrf = wc.cookies.get("kb_csrf")

    # 无 CSRF 头
    r = wc.post("/v1/items/nonexistent/reprocess", json={})
    assert r.status_code == 403
    # CSRF 头不匹配
    r = wc.post("/v1/items/nonexistent/reprocess", json={},
                headers={"X-CSRF-Token": "wrong"})
    assert r.status_code == 403
    # CSRF 正确但 Origin 是外站
    r = wc.post("/v1/items/nonexistent/reprocess", json={},
                headers={"X-CSRF-Token": csrf, "Origin": "https://evil.example"})
    assert r.status_code == 403
    # CSRF 与 Origin 均通过 → 通过认证，业务层 404
    r = wc.post("/v1/items/nonexistent/reprocess", json={},
                headers={"X-CSRF-Token": csrf, "Origin": "http://testserver"})
    assert r.status_code == 404


def test_web_device_cannot_auth_via_cookie_for_desktop_token(user_a, wc):
    """Bearer 通道仍然可用；Cookie 通道只认 web 设备签发的会话。"""
    r = wc.get("/v1/items", headers=auth(user_a["desktop"]["token"]))
    assert r.status_code == 200
    assert wc.get("/v1/items").status_code == 401


def test_logout_revokes_session(wc, web_pairing):
    _login(wc, web_pairing)
    csrf = wc.cookies.get("kb_csrf")
    assert wc.get("/v1/items").status_code == 200
    r = wc.post("/v1/web/logout", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    assert wc.get("/v1/items").status_code == 401


# ---- refetch ----

def _capture_url(client, token, key: str, url: str | None = None):
    return client.post(
        "/v1/captures",
        json={
            "client_capture_id": f"{key}-1111-2222-3333-444444444444",
            "input_kind": "url" if url else "text",
            "original_url": url,
            "text": None if url else "一段纯文本内容，用于测试。",
        },
        headers={**auth(token), "Idempotency-Key": key},
    )


def test_refetch_requires_url(client, user_a):
    r = _capture_url(client, user_a["desktop"]["token"], "rf-text-1")
    assert r.status_code == 202
    item_id = r.json()["item_id"]
    r2 = client.post(f"/v1/items/{item_id}/refetch", headers=auth(user_a["desktop"]["token"]))
    assert r2.status_code == 422


def test_refetch_rate_limited(client, user_a):
    r = _capture_url(client, user_a["desktop"]["token"], "rf-url-1",
                     url="https://example.com/posts/hello")
    assert r.status_code == 202
    item_id = r.json()["item_id"]
    ok = client.post(f"/v1/items/{item_id}/refetch", headers=auth(user_a["desktop"]["token"]))
    assert ok.status_code == 202
    assert ok.json()["pipeline_state"] == "queued"
    again = client.post(f"/v1/items/{item_id}/refetch", headers=auth(user_a["desktop"]["token"]))
    assert again.status_code == 429


def test_refetch_unchanged_keeps_revision(client, user_a, session_factory, monkeypatch):
    """同一页面重新提取：内容无变化时不新增来源版本（docs/02 §10.1）。"""
    from tests.integration.test_m4_web import GENERIC_HTML, GENERIC_URL, make_page_net

    net = make_page_net()
    from kbserver.extractors import bilibili as bili, webpages

    monkeypatch.setattr(webpages, "safe_fetch", net)
    monkeypatch.setattr(bili, "safe_fetch", net)

    r = _capture_url(client, user_a["desktop"]["token"], "rf-same-1", url=GENERIC_URL)
    assert r.status_code == 202
    item_id = r.json()["item_id"]

    from kbserver.workers import worker

    while worker.run_once(session_factory):
        pass
    it = client.get(f"/v1/items/{item_id}", headers=auth(user_a["desktop"]["token"])).json()
    assert it["source_revision"] == 2  # 提取产出 r2

    # 再次入队 extract（等价于 refetch 到来的任务执行），只跑这一轮
    from kbserver.domain import pipeline
    from kbserver.models import Item

    with session_factory() as db:
        item = db.query(Item).filter(Item.id == item_id).one()
        pipeline.enqueue_stage(
            db, user_id=item.user_id, item_id=item.id,
            source_revision=item.source_revision, stage="extract", reset_attempt=True,
        )
        db.commit()

    assert worker.run_once(session_factory) is True  # 执行 extract 任务
    it2 = client.get(f"/v1/items/{item_id}", headers=auth(user_a["desktop"]["token"])).json()
    assert it2["source_revision"] == 2  # 内容无变化，仍是 r2
    assert "无变化" in (it2["state_detail"] or "")
