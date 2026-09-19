"""Web 收件箱集成测试：中心会话认证、Cookie/CSRF/Origin 校验、登出、
续期 Cookie 转发、refetch 限频与内容无变化不新增版本
（docs/02 §9.1、§10.1；docs/05 §4.1、§4.5：配对码通道已关闭）。

中心认证服务用 fake 替身模拟：真实调用发生在 B1 联调验收（docs/06）。
不包含真实 Cookie/Key/私人内容；网页响应用合成数据。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from kbserver.app import create_app

from tests.conftest import auth

AUTH_COOKIE = "test_session"


@pytest.fixture()
def central(monkeypatch):
    """模拟中心认证：可切换有效/失效/不可用/续期。"""
    monkeypatch.setenv("AUTH_COOKIE_NAME", AUTH_COOKIE)
    monkeypatch.setenv("AUTH_SESSION_URL", "https://auth.example.com/api/auth/session")
    monkeypatch.setenv("AUTH_LOGIN_URL", "https://auth.example.com/login")
    monkeypatch.setenv("AUTH_LOGOUT_URL", "https://auth.example.com/api/auth/logout")
    monkeypatch.setenv("AUTH_COOKIE_DOMAIN", "")  # 只接受无域属性的续期 Cookie
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://testserver")

    state = {
        "valid": True,
        "unavailable": False,
        "logged_out": False,
        "renew": False,
        "logout_called": 0,
        "user": {"id": "central-uid-0001", "username": "老账号", "role": "admin"},
    }

    def fake_validate(cookie_value):
        if state["unavailable"]:
            from kbserver.security import central_auth

            raise central_auth.CentralAuthUnavailable("中心认证服务不可达：模拟")
        if state["logged_out"] or not state["valid"] or cookie_value != "fake-central-cookie":
            from kbserver.security import central_auth

            raise central_auth.CentralAuthRejected("中心会话无效或已过期")
        renewal = None
        if state["renew"]:
            renewal = {"value": "renewed-central-cookie", "max_age": 86400,
                       "domain": None, "secure": False}
        return {"user": dict(state["user"]), "expiresAt": "2026-09-09T00:00:00Z"}, renewal

    def fake_logout(cookie_value):
        state["logout_called"] += 1

    monkeypatch.setattr("kbserver.security.central_auth.validate_central_session", fake_validate)
    monkeypatch.setattr("kbserver.security.central_auth.central_logout", fake_logout)
    return state


@pytest.fixture()
def wc(engine):
    """独立 TestClient：Cookie 不与其他测试串扰；依赖 engine 保证建表。"""
    with TestClient(create_app()) as c:
        yield c


def _login(wc, central):
    """模拟「浏览器已有中心会话」：直接携带中心 Cookie 访问。"""
    wc.cookies.set(AUTH_COOKIE, "fake-central-cookie")
    return wc.get("/v1/auth/me")


def test_central_session_maps_local_user(wc, central, db):
    """首次进入：校验中心会话 → 幂等创建本地用户并绑定 auth_subject。"""
    r = _login(wc, central)
    assert r.status_code == 200
    me = r.json()
    assert me["central_username"] == "老账号"
    assert me["auth_method"] == "central_session"
    assert me["is_admin"] is True  # role=admin（管理员入口依据）

    from kbserver.models import User

    user = db.query(User).filter(User.auth_subject == "central-uid-0001").one()
    assert user.name == "老账号"
    assert me["user_id"] == user.id

    # Cookie 通道可访问业务接口（Web scopes）
    assert wc.get("/v1/items").status_code == 200


def test_second_visit_reuses_same_local_user(wc, central, db):
    _login(wc, central)
    me1 = wc.get("/v1/auth/me").json()
    _login(wc, central)
    me2 = wc.get("/v1/auth/me").json()
    assert me1["user_id"] == me2["user_id"]


def test_no_cookie_returns_401(wc, central):
    assert wc.get("/v1/auth/me").status_code == 401
    assert wc.get("/v1/items").status_code == 401


def test_central_rejected_returns_401(wc, central):
    central["valid"] = False
    wc.cookies.set(AUTH_COOKIE, "fake-central-cookie")
    assert wc.get("/v1/items").status_code == 401


def test_central_unavailable_returns_503(wc, central, db):
    """中心超时/异常：可恢复的 503，不当作无账号（不新增本地用户）。"""
    central["unavailable"] = True
    wc.cookies.set(AUTH_COOKIE, "fake-central-cookie")

    from kbserver.models import User

    before = db.query(User).count()
    r = wc.get("/v1/items")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "AUTH_UNAVAILABLE"
    assert db.query(User).count() == before, "503 不得创建新用户"


def test_invalid_bearer_does_not_fall_back_to_cookie(wc, central, user_a):
    """显式传入无效 Bearer 时直接拒绝，不悄悄使用浏览器 Cookie 换身份（docs/05 §4.2）。"""
    _login(wc, central)
    r = wc.get("/v1/items", headers=auth("kbi_" + "x" * 43))
    assert r.status_code == 401


def test_web_channel_cannot_send_receipts(wc, central, user_a):
    """同步回执只允许有合法设备的通道（docs/05 §4.2）。"""
    _login(wc, central)
    r = wc.post("/v1/receipts", json={
        "item_id": "whatever", "bundle_revision": 1,
        "manifest_sha256": "0" * 64, "local_commit_id": "",
    })
    assert r.status_code == 403


def test_login_redirect_goes_to_central(wc, central):
    r = wc.get("/login", follow_redirects=False)
    assert r.status_code == 307
    loc = r.headers["location"]
    assert loc.startswith("https://auth.example.com/login?return_to=")
    from urllib.parse import unquote, urlparse, parse_qs

    return_to = parse_qs(urlparse(loc).query)["return_to"][0]
    assert return_to.startswith("http://testserver/inbox")


def test_login_redirect_rejects_external_next(wc, central):
    r = wc.get("/login", params={"next": "https://evil.example"}, follow_redirects=False)
    assert r.status_code == 307
    assert "evil.example" not in r.headers["location"]


def test_central_unconfigured_returns_503(wc, monkeypatch):
    monkeypatch.delenv("AUTH_SESSION_URL", raising=False)
    monkeypatch.setenv("AUTH_COOKIE_NAME", AUTH_COOKIE)
    wc.cookies.set(AUTH_COOKIE, "fake-central-cookie")
    assert wc.get("/v1/items").status_code == 503


def test_renewal_cookie_relayed(wc, central):
    """中心的滑动续期 Set-Cookie 被转发给浏览器（docs/05 §4.1 第 5 条）。"""
    _login(wc, central)
    central["renew"] = True
    r = wc.get("/v1/items")
    assert r.status_code == 200
    set_cookies = r.headers.get_list("set-cookie")
    assert any(c.startswith(f"{AUTH_COOKIE}=renewed-central-cookie") for c in set_cookies)
    central["renew"] = False


def test_cookie_write_requires_csrf_and_origin(wc, central):
    _login(wc, central)
    csrf = wc.cookies.get("kb_csrf")
    assert csrf  # 首次会话响应补发了 CSRF Cookie

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


def test_bearer_channel_still_works(wc, central, user_a):
    _login(wc, central)
    r = wc.get("/v1/items", headers=auth(user_a["desktop"]["token"]))
    assert r.status_code == 200


def test_logout_revokes_central_session(wc, central):
    _login(wc, central)
    csrf = wc.cookies.get("kb_csrf")
    assert wc.get("/v1/items").status_code == 200
    r = wc.post("/v1/auth/logout", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    assert central["logout_called"] == 1
    central["logged_out"] = True
    assert wc.get("/v1/items").status_code == 401


def test_old_web_pairing_session_no_longer_authenticates(wc, central, db, user_a):
    """旧配对码/旧 kb_session 认证路径已关闭（docs/05 §4.5）。"""
    from kbserver.security.tokens import WEB_SCOPES, issue_pairing_code

    raw, code = issue_pairing_code(user_a["user_id"], "web", WEB_SCOPES)
    db.add(code)
    db.commit()
    # 配对交换接口已移除
    assert wc.post("/v1/pairing/exchange", json={"code": raw, "device_name": "x"}).status_code == 404
    assert wc.post("/v1/web/session", json={"code": raw, "device_name": "x"}).status_code == 404
    # 旧 kb_session Cookie 不再被当作凭据
    wc.cookies.set("kb_session", "old-web-device-token")
    assert wc.get("/v1/items").status_code == 401


# ---- 插件设备授权流程（docs/05 §4.5） ----

def test_device_flow_full_path(wc, central):
    me = _login(wc, central)
    csrf = wc.cookies.get("kb_csrf")

    start = wc.post("/v1/auth/device/start", json={"device_name": "我的 Obsidian"})
    assert start.status_code == 200
    body = start.json()
    assert body["poll_secret"] and body["request_id"]
    assert body["browser_url"].startswith("http://testserver/authorize?request_id=")

    # 只有 request_id、没有 poll_secret 不能领取 Token
    info = wc.get("/v1/auth/device/info", params={"request_id": body["request_id"]}).json()
    assert info["device_name"] == "我的 Obsidian"
    assert "poll_secret" not in json.dumps(info)

    poll_bad = wc.post("/v1/auth/device/poll", json={
        "request_id": body["request_id"], "poll_secret": "wrong-secret-wrong-secret"})
    assert poll_bad.status_code == 403
    poll_before = wc.post("/v1/auth/device/poll", json={
        "request_id": body["request_id"], "poll_secret": body["poll_secret"]})
    assert poll_before.json()["status"] == "pending"

    # 浏览器批准（CSRF + Origin 校验的 POST）
    approve = wc.post("/v1/auth/device/approve", json={"request_id": body["request_id"]},
                      headers={"X-CSRF-Token": csrf, "Origin": "http://testserver"})
    assert approve.status_code == 200

    # 重复批准拒绝（不能换账号/二次批准）
    approve2 = wc.post("/v1/auth/device/approve", json={"request_id": body["request_id"]},
                       headers={"X-CSRF-Token": csrf, "Origin": "http://testserver"})
    assert approve2.status_code == 409

    poll = wc.post("/v1/auth/device/poll", json={
        "request_id": body["request_id"], "poll_secret": body["poll_secret"]})
    assert poll.status_code == 200
    tok = poll.json()
    assert tok["status"] == "ok" and tok["token"] and tok["user_id"] == me.json()["user_id"]

    # 已消费的请求不能再领
    poll2 = wc.post("/v1/auth/device/poll", json={
        "request_id": body["request_id"], "poll_secret": body["poll_secret"]})
    assert poll2.status_code == 410

    # 新 Token 可用（desktop scopes），可同步
    items = wc.get("/v1/items", headers=auth(tok["token"]))
    assert items.status_code == 200

    # 断开设备：撤销后 Token 失效
    disc = wc.post(f"/v1/devices/{tok['device_id']}/disconnect", headers=auth(tok["token"]))
    assert disc.status_code == 200
    assert wc.get("/v1/items", headers=auth(tok["token"])).status_code == 401


def test_device_approve_requires_central_session(wc, central, user_a):
    start = wc.post("/v1/auth/device/start", json={"device_name": "x"})
    request_id = start.json()["request_id"]
    # 设备 Bearer 通道不能批准设备授权
    r = wc.post("/v1/auth/device/approve", json={"request_id": request_id},
                headers=auth(user_a["desktop"]["token"]))
    assert r.status_code == 403


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
    from kbserver.extractors import bilibili as bili, fetch_base

    monkeypatch.setattr(fetch_base, "safe_fetch", net)
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
