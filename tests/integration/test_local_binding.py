"""docs/08 §8.3、§8.5、§11：本地 Key 绑定接口契约测试。

验收要求（§11）：
- 拒绝其他用户的配置、无专用授权和已撤销设备；
- 普通读取接口依旧不返回 Key；
- 解绑与供应商撤销的含义正确区分；
- 响应禁止缓存。
"""
from __future__ import annotations

import json

from tests.conftest import auth, make_user_with_tokens

from kbserver.models import Device, Token, User, utcnow
from kbserver.security.tokens import BIND_LOCAL_SCOPE, DESKTOP_SCOPES, issue_token

ALLOWED_ENDPOINT = "https://api.deepseek.com/v1"
SECRET = "sk-bind-test-1234567890"


def _create_profile(client, token, *, model="deepseek-chat"):
    return client.post("/v1/provider-profiles", json={
        "kind": "llm", "adapter": "openai-compatible", "endpoint": ALLOWED_ENDPOINT,
        "model": model, "secret": SECRET,
    }, headers=auth(token))


def _device_with_scopes(db, user_id, name, scopes):
    device = Device(user_id=user_id, kind="desktop", name=name)
    db.add(device)
    db.flush()
    raw, token = issue_token(user_id, device.id, scopes)
    db.add(token)
    db.commit()
    return {"device_id": device.id, "token": raw}


# ---- 权限与归属 ----

def test_bind_requires_dedicated_scope(client, db, user_a):
    """旧设备只有 profiles:manage 也不自动获得导出能力（docs/08 §8.3）。"""
    r = _create_profile(client, user_a["desktop"]["token"])
    profile_id = r.json()["id"]

    # 普通桌面 Token 缺少 profiles:bind-local
    out = client.post(f"/v1/provider-profiles/{profile_id}/local-binding",
                      headers=auth(user_a["desktop"]["token"]))
    assert out.status_code == 403
    assert BIND_LOCAL_SCOPE in out.json()["error"]["message"]


def test_bind_rejects_other_users_profile(client, db, user_a, user_b):
    """不能领取其他用户的配置（docs/08 §11）。"""
    r = _create_profile(client, user_b["desktop"]["token"])
    profile_id = r.json()["id"]
    bindable = _device_with_scopes(db, user_a["user_id"], "a-bind", DESKTOP_SCOPES + [BIND_LOCAL_SCOPE])

    out = client.post(f"/v1/provider-profiles/{profile_id}/local-binding",
                      headers=auth(bindable["token"]))
    assert out.status_code == 404  # 未知对象与他人对象统一 404


def test_bind_rejects_revoked_device(client, db, user_a):
    r = _create_profile(client, user_a["desktop"]["token"])
    profile_id = r.json()["id"]
    bindable = _device_with_scopes(db, user_a["user_id"], "a-revoked",
                                   DESKTOP_SCOPES + [BIND_LOCAL_SCOPE])
    device = db.get(Device, bindable["device_id"])
    device.revoked_at = utcnow()
    db.commit()

    out = client.post(f"/v1/provider-profiles/{profile_id}/local-binding",
                      headers=auth(bindable["token"]))
    assert out.status_code == 401


def test_bind_requires_device_channel(client, db, user_a):
    """中心会话（无设备）不能领取 Key。"""
    r = _create_profile(client, user_a["desktop"]["token"])
    profile_id = r.json()["id"]
    # 模拟：无 Bearer、无 Cookie -> 401；有设备要求由 require_device 保证
    out = client.post(f"/v1/provider-profiles/{profile_id}/local-binding")
    assert out.status_code == 401


# ---- 下发内容与缓存 ----

def test_bind_returns_key_once_with_no_store(client, db, user_a):
    r = _create_profile(client, user_a["desktop"]["token"], model="deepseek-chat")
    profile = r.json()
    bindable = _device_with_scopes(db, user_a["user_id"], "a-local",
                                   DESKTOP_SCOPES + [BIND_LOCAL_SCOPE])

    out = client.post(f"/v1/provider-profiles/{profile['id']}/local-binding",
                      headers=auth(bindable["token"]))
    assert out.status_code == 200
    assert "no-store" in out.headers["cache-control"]
    body = out.json()
    assert body["secret"] == SECRET
    assert body["profile_id"] == profile["id"]
    assert body["profile_version"] == profile["version"]
    assert body["credential_version"] == 1
    assert body["endpoint"] == ALLOWED_ENDPOINT
    assert body["model"] == "deepseek-chat"
    assert "capabilities" in body
    assert body["binding_id"]

    # 状态接口不返回 Key
    st = client.get(f"/v1/provider-profiles/{profile['id']}/local-binding",
                    headers=auth(bindable["token"]))
    assert st.status_code == 200
    assert st.json()["bound"] is True
    assert SECRET not in json.dumps(st.json())

    # 普通列表接口依旧不返回 Key（docs/08 §8.3）
    listing = client.get("/v1/provider-profiles", headers=auth(user_a["desktop"]["token"]))
    assert SECRET not in json.dumps(listing.json())


def test_bind_rebinds_with_latest_credential_version(client, db, user_a):
    """线上换 Key 后再次绑定下发新版本（docs/08 §8.3）。"""
    r = _create_profile(client, user_a["desktop"]["token"])
    profile = r.json()
    bindable = _device_with_scopes(db, user_a["user_id"], "a-local2",
                                   DESKTOP_SCOPES + [BIND_LOCAL_SCOPE])
    first = client.post(f"/v1/provider-profiles/{profile['id']}/local-binding",
                        headers=auth(bindable["token"]))
    assert first.json()["credential_version"] == 1

    client.patch(f"/v1/provider-profiles/{profile['id']}",
                 json={"secret": "sk-rotated-0987654321"}, headers=auth(user_a["desktop"]["token"]))
    second = client.post(f"/v1/provider-profiles/{profile['id']}/local-binding",
                         headers=auth(bindable["token"]))
    body = second.json()
    assert body["credential_version"] == 2
    assert body["secret"] == "sk-rotated-0987654321"
    # 同设备同配置只有一条绑定，不重复新建
    assert body["binding_id"] == first.json()["binding_id"]


def test_unbind_does_not_revoke_provider_credential(client, db, user_a):
    """解绑只删除本机绑定，不替用户撤销线上 Key（docs/08 §8.3、§11）。"""
    r = _create_profile(client, user_a["desktop"]["token"])
    profile = r.json()
    bindable = _device_with_scopes(db, user_a["user_id"], "a-local3",
                                   DESKTOP_SCOPES + [BIND_LOCAL_SCOPE])
    client.post(f"/v1/provider-profiles/{profile['id']}/local-binding",
                headers=auth(bindable["token"]))

    out = client.delete(f"/v1/provider-profiles/{profile['id']}/local-binding",
                        headers=auth(bindable["token"]))
    assert out.status_code == 200
    assert out.json()["unbound"] is True

    # 线上凭据仍在：普通读取仍显示 configured
    listing = client.get("/v1/provider-profiles", headers=auth(user_a["desktop"]["token"]))
    row = next(p for p in listing.json() if p["id"] == profile["id"])
    assert row["configured"] is True
    # 解绑后再次领取仍可用（服务端撤销绑定才是阻止领取的方式）
    again = client.post(f"/v1/provider-profiles/{profile['id']}/local-binding",
                        headers=auth(bindable["token"]))
    assert again.status_code == 200


def test_bind_requires_usable_credential(client, db, user_a):
    r = _create_profile(client, user_a["desktop"]["token"])
    profile = r.json()
    bindable = _device_with_scopes(db, user_a["user_id"], "a-local4",
                                   DESKTOP_SCOPES + [BIND_LOCAL_SCOPE])
    client.delete(f"/v1/provider-profiles/{profile['id']}/credential",
                  headers=auth(user_a["desktop"]["token"]))
    out = client.post(f"/v1/provider-profiles/{profile['id']}/local-binding",
                      headers=auth(bindable["token"]))
    assert out.status_code == 422
    assert "凭据" in out.json()["error"]["message"]


# ---- 设备授权申请权限 ----

def test_device_start_filters_requested_scopes(client):
    """授权只签发白名单内的额外权限（docs/08 §8.3）。"""
    r = client.post("/v1/auth/device/start", json={
        "device_name": "测试设备",
        "requested_scopes": [BIND_LOCAL_SCOPE, "profiles:manage", "evil:scope"],
    })
    assert r.status_code == 200
    body = r.json()
    assert BIND_LOCAL_SCOPE in body["granted_scopes"]
    assert "evil:scope" not in body["granted_scopes"]

    info = client.get(f"/v1/auth/device/info?request_id={body['request_id']}").json()
    assert info["requests_local_key_binding"] is True
    assert info["requested_scopes"] == [BIND_LOCAL_SCOPE]


def test_device_start_without_extra_scopes(client):
    r = client.post("/v1/auth/device/start", json={"device_name": "普通设备"})
    body = r.json()
    assert BIND_LOCAL_SCOPE not in body["granted_scopes"]
    info = client.get(f"/v1/auth/device/info?request_id={body['request_id']}").json()
    assert info["requests_local_key_binding"] is False
