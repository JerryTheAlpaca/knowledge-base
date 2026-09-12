"""管理员代配用户凭据（docs/16）。

- 仅管理员可写；secret 不出现在任何响应。
- LLM：写入目标用户后 configured；标记 local_export=denied，本机绑定被拒。
- 用户自行 PATCH secret 后解除本地下发限制。
- B 站：PUT/DELETE/test 只影响目标用户；local-binding 对 bilibili 仍 422。
"""
from __future__ import annotations

from tests.conftest import auth

from kbserver.models import Credential, Device, ProviderProfile
from kbserver.security.tokens import BIND_LOCAL_SCOPE, DESKTOP_SCOPES, issue_token

AUTH_COOKIE = "test_session"
LLM_ENDPOINT = "https://api.deepseek.com/v1"
LLM_SECRET = "sk-admin-provision-1234567890"
# 合法 SESSDATA 形态：长度与字符集由 _extract_sessdata 校验
SESSDATA = "abc123def456ghi789jkl012mno345pqr678"


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


def _device_with_scopes(db, user_id, name, scopes):
    device = Device(user_id=user_id, kind="desktop", name=name)
    db.add(device)
    db.flush()
    raw, token = issue_token(user_id, device.id, scopes)
    db.add(token)
    db.commit()
    return raw


def test_admin_provision_llm_and_block_local_export(engine, monkeypatch, session_factory, db, user_a):
    _central(monkeypatch)
    wc = _wc(engine)
    assert _login(wc).json()["is_admin"] is True

    r = wc.put(
        f"/v1/admin/users/{user_a['user_id']}/llm-credential",
        json={"endpoint": LLM_ENDPOINT, "model": "deepseek-chat", "secret": LLM_SECRET},
        headers=_csrf_headers(wc),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["configured"] is True
    assert body["local_export"] == "denied"
    assert "secret" not in body and LLM_SECRET not in r.text

    # 目标用户侧可见已配置，但读接口仍无 secret
    listed = wc.get("/v1/provider-profiles", headers=auth(user_a["desktop"]["token"]))
    assert listed.status_code == 200
    profiles = listed.json()
    assert profiles and profiles[0]["configured"] is True
    assert LLM_SECRET not in listed.text

    profile_id = profiles[0]["id"]
    bindable = _device_with_scopes(db, user_a["user_id"], "a-bind", DESKTOP_SCOPES + [BIND_LOCAL_SCOPE])
    bound = wc.post(f"/v1/provider-profiles/{profile_id}/local-binding", headers=auth(bindable))
    assert bound.status_code == 403, bound.text
    assert "管理员代配" in bound.json()["error"]["message"]
    assert LLM_SECRET not in bound.text

    # 用户自行更换 Key 后允许绑定
    patched = wc.patch(
        f"/v1/provider-profiles/{profile_id}",
        json={"secret": "sk-user-own-key-0987654321"},
        headers=auth(user_a["desktop"]["token"]),
    )
    assert patched.status_code == 200
    bound2 = wc.post(f"/v1/provider-profiles/{profile_id}/local-binding", headers=auth(bindable))
    assert bound2.status_code == 200
    assert "sk-user-own-key-0987654321" in bound2.text


def test_admin_credentials_require_admin_and_isolation(engine, monkeypatch, session_factory, db, user_a, user_b):
    state = _central(monkeypatch, role="user")
    wc = _wc(engine)
    _login(wc)
    denied = wc.put(
        f"/v1/admin/users/{user_a['user_id']}/llm-credential",
        json={"endpoint": LLM_ENDPOINT, "model": "deepseek-chat", "secret": LLM_SECRET},
        headers=_csrf_headers(wc),
    )
    assert denied.status_code == 403

    state["user"]["role"] = "admin"
    wc2 = _wc(engine)
    _login(wc2)
    ok = wc2.put(
        f"/v1/admin/users/{user_a['user_id']}/llm-credential",
        json={"endpoint": LLM_ENDPOINT, "model": "deepseek-chat", "secret": LLM_SECRET},
        headers=_csrf_headers(wc2),
    )
    assert ok.status_code == 200

    users = wc2.get("/v1/admin/users").json()["users"]
    by_id = {u["user_id"]: u for u in users}
    assert by_id[user_a["user_id"]]["llm"]["configured"] is True
    assert by_id[user_b["user_id"]]["llm"]["configured"] is False
    assert LLM_SECRET not in wc2.get("/v1/admin/users").text

    # 不存在的用户
    assert wc2.put(
        "/v1/admin/users/no-such-user/llm-credential",
        json={"endpoint": LLM_ENDPOINT, "model": "m", "secret": LLM_SECRET},
        headers=_csrf_headers(wc2),
    ).status_code == 404


def test_admin_provision_bilibili_and_revoke(engine, monkeypatch, session_factory, db, user_a):
    _central(monkeypatch)
    wc = _wc(engine)
    _login(wc)

    r = wc.put(
        f"/v1/admin/users/{user_a['user_id']}/bilibili-session",
        json={"secret": SESSDATA},
        headers=_csrf_headers(wc),
    )
    assert r.status_code == 200, r.text
    assert r.json()["configured"] is True
    assert SESSDATA not in r.text

    # 用户侧状态可见，仍无明文
    mine = wc.get("/v1/bilibili-session", headers=auth(user_a["desktop"]["token"]))
    assert mine.status_code == 200 and mine.json()["configured"] is True
    assert SESSDATA not in mine.text

    # B 站配置不能本地下发
    profile = (
        db.query(ProviderProfile)
        .filter(ProviderProfile.user_id == user_a["user_id"],
                ProviderProfile.kind == "bilibili_session")
        .one()
    )
    bindable = _device_with_scopes(db, user_a["user_id"], "a-bili-bind",
                                   DESKTOP_SCOPES + [BIND_LOCAL_SCOPE])
    bound = wc.post(f"/v1/provider-profiles/{profile.id}/local-binding", headers=auth(bindable))
    assert bound.status_code == 422

    rev = wc.delete(f"/v1/admin/users/{user_a['user_id']}/bilibili-session",
                    headers=_csrf_headers(wc))
    assert rev.status_code == 200 and rev.json()["revoked"] is True
    creds = db.query(Credential).filter(
        Credential.profile_id == profile.id, Credential.revoked_at.is_(None)
    ).count()
    assert creds == 0


def test_admin_llm_revoke(engine, monkeypatch, session_factory, db, user_a):
    _central(monkeypatch)
    wc = _wc(engine)
    _login(wc)
    wc.put(
        f"/v1/admin/users/{user_a['user_id']}/llm-credential",
        json={"endpoint": LLM_ENDPOINT, "model": "deepseek-chat", "secret": LLM_SECRET},
        headers=_csrf_headers(wc),
    )
    rev = wc.delete(f"/v1/admin/users/{user_a['user_id']}/llm-credential",
                    headers=_csrf_headers(wc))
    assert rev.status_code == 200 and rev.json()["revoked"] is True
    listed = wc.get("/v1/provider-profiles", headers=auth(user_a["desktop"]["token"])).json()
    assert listed[0]["configured"] is False
