"""管理员用户列表与删除账户。

- 用户列表：带条目统计（item_count / last_item_at）。
- 删除账户：先删中心账号（404 视为已删），再硬删本地全部数据；
  不能删当前登录账号。
- 邀请码删除：以 DELETE 方法代理中心端点。
"""
from __future__ import annotations

from tests.conftest import auth

from kbserver.models import Capture, Item

AUTH_COOKIE = "test_session"


def _central(monkeypatch, role="admin"):
    monkeypatch.setenv("AUTH_COOKIE_NAME", AUTH_COOKIE)
    monkeypatch.setenv("AUTH_SESSION_URL", "https://auth.example.com/api/auth/session")
    monkeypatch.setenv("AUTH_LOGIN_URL", "https://auth.example.com/login")
    monkeypatch.setenv("AUTH_LOGOUT_URL", "https://auth.example.com/api/auth/logout")
    monkeypatch.setenv("AUTH_COOKIE_DOMAIN", "")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://testserver")

    state = {"valid": True, "user": {"id": "central-admin-1", "username": "管理员", "role": role}}

    def fake_validate(cookie_value):
        if not state["valid"] or cookie_value != "fake-central-cookie":
            from kbserver.security import central_auth

            raise central_auth.CentralAuthRejected("中心会话无效或已过期")
        return {"user": dict(state["user"]), "expiresAt": "2026-09-09T00:00:00Z"}, None

    monkeypatch.setattr("kbserver.security.central_auth.validate_central_session", fake_validate)
    return state


def _wc(engine):
    from fastapi.testclient import TestClient

    from kbserver.app import create_app

    return TestClient(create_app())


def _login(wc):
    wc.cookies.set(AUTH_COOKIE, "fake-central-cookie")
    r = wc.get("/v1/auth/me")
    assert r.status_code == 200
    return r


def _csrf_headers(wc):
    csrf = wc.cookies.get("kb_csrf")
    assert csrf
    return {"X-CSRF-Token": csrf, "Origin": "http://testserver"}


def _add_item(db, user_id: str, client_capture_id: str) -> Item:
    cap = Capture(user_id=user_id, client_capture_id=client_capture_id,
                  request_hash=f"hash-{client_capture_id}", input_json={})
    db.add(cap)
    db.flush()
    item = Item(user_id=user_id, capture_id=cap.id)
    db.add(item)
    db.commit()
    return item


def test_admin_users_list_item_stats(engine, monkeypatch, db, user_a, user_b):
    _central(monkeypatch)
    wc = _wc(engine)
    _login(wc)
    _add_item(db, user_a["user_id"], "cap-a1")

    r = wc.get("/v1/admin/users")
    assert r.status_code == 200, r.text
    users = {u["name"]: u for u in r.json()["users"]}
    assert users["用户A"]["item_count"] == 1
    assert users["用户A"]["last_item_at"]
    assert users["用户B"]["item_count"] == 0
    assert users["用户B"]["last_item_at"] is None


def test_admin_delete_user_purges_local_and_central(engine, monkeypatch, session_factory, db, user_a):
    _central(monkeypatch)
    wc = _wc(engine)
    _login(wc)

    calls = []

    def fake_proxy(request, method, path, json_body=None):
        calls.append((method, path))
        return {"deleted": True}

    monkeypatch.setattr("kbserver.api.routes_admin._proxy_central", fake_proxy)

    _add_item(db, user_a["user_id"], "cap-del")
    from kbserver.models import User

    user = db.get(User, user_a["user_id"])
    user.auth_subject = "central-user-a"
    db.commit()

    r = wc.delete(f"/v1/admin/users/{user_a['user_id']}", headers=_csrf_headers(wc))
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] is True
    # 中心账号以 DELETE 方法按 auth_subject 删除
    assert calls == [("DELETE", "/api/admin/users/central-user-a")]

    # 本地全部行清空：用户、设备、Token、条目
    with session_factory() as check:
        assert check.get(User, user_a["user_id"]) is None
        assert check.query(Capture).filter(Capture.user_id == user_a["user_id"]).count() == 0
        assert check.query(Item).filter(Item.user_id == user_a["user_id"]).count() == 0

    # 重复删除：本地已不存在 → 404
    r2 = wc.delete(f"/v1/admin/users/{user_a['user_id']}", headers=_csrf_headers(wc))
    assert r2.status_code == 404


def test_admin_delete_user_cannot_delete_self(engine, monkeypatch, db):
    _central(monkeypatch)
    wc = _wc(engine)
    _login(wc)
    from kbserver.models import User

    me = db.query(User).filter(User.auth_subject == "central-admin-1").one()
    r = wc.delete(f"/v1/admin/users/{me.id}", headers=_csrf_headers(wc))
    assert r.status_code == 403


def test_admin_delete_user_purges_legacy_usage_ledger(engine, monkeypatch, session_factory, db, user_b):
    """生产库仍有去计费前的 usage_ledger 遗留表（FK 引用 provider_operations），
    purge 必须先清它，否则删除账户时 FK 约束失败。"""
    from sqlalchemy import text

    _central(monkeypatch)
    wc = _wc(engine)
    _login(wc)

    # 模拟生产遗留表（测试库默认没有）
    db.execute(text(
        "CREATE TABLE IF NOT EXISTS usage_ledger ("
        " seq INTEGER PRIMARY KEY AUTOINCREMENT,"
        " user_id VARCHAR(36) NOT NULL REFERENCES users(id),"
        " operation_id VARCHAR(36) REFERENCES provider_operations(id),"
        " kind VARCHAR(40) NOT NULL, quantity INTEGER NOT NULL)"
    ))
    uid = user_b["user_id"]
    db.execute(text(
        "INSERT INTO usage_ledger (user_id, operation_id, kind, quantity)"
        " VALUES (:uid, NULL, 'llm_call', 3)"), {"uid": uid})
    db.commit()

    r = wc.delete(f"/v1/admin/users/{uid}", headers=_csrf_headers(wc))
    assert r.status_code == 200, r.text
    with session_factory() as check:
        assert check.execute(
            text("SELECT COUNT(*) FROM usage_ledger WHERE user_id = :uid"), {"uid": uid}
        ).fetchone()[0] == 0


def test_admin_delete_invitation_proxies_delete(engine, monkeypatch, db):
    _central(monkeypatch)
    wc = _wc(engine)
    _login(wc)

    calls = []

    def fake_proxy(request, method, path, json_body=None):
        calls.append((method, path))
        return {"deleted": True}

    monkeypatch.setattr("kbserver.api.routes_admin._proxy_central", fake_proxy)
    r = wc.delete("/v1/admin/invitations/inv-123", headers=_csrf_headers(wc))
    assert r.status_code == 200, r.text
    assert calls == [("DELETE", "/api/invitations/inv-123")]


def test_admin_endpoints_require_admin(engine, monkeypatch, db, user_a):
    state = _central(monkeypatch, role="user")
    wc = _wc(engine)
    _login(wc)
    assert wc.get("/v1/admin/users").status_code == 403
    assert wc.delete(
        f"/v1/admin/users/{user_a['user_id']}", headers=_csrf_headers(wc)
    ).status_code == 403
    assert wc.delete(
        "/v1/admin/invitations/inv-x", headers=_csrf_headers(wc)
    ).status_code == 403
    state["user"]["role"] = "admin"
