"""统一账号 API（docs/05 §4.1、§4.2、§4.5）。

- GET  /login：302 到中心登录页（return_to 指回 KB 自身路径）。
- GET  /v1/auth/me：当前账号（页面初始化与管理员入口依据）。
- POST /v1/auth/logout：撤销中心会话并清理 Cookie（CSRF/Origin 校验后代理）。
- POST /v1/auth/device/start|poll：插件端发起/轮询。
- GET  /v1/auth/device/info、POST /v1/auth/device/approve|cancel：浏览器授权页用。

设备授权：数据库只存 poll_secret 哈希；browser_url 只含 request_id；
仅知道 request_id 不能领取 Token；一条请求只能批准一次且不能换账号。
"""
from __future__ import annotations

import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..api.deps import (
    CSRF_COOKIE,
    current_principal,
    ensure_local_user,
    require_device,
)
from ..domain.errors import ApiError
from ..models import Device, DeviceAuthRequest, Token, utcnow
from ..security import central_auth
from ..security.tokens import DESKTOP_SCOPES, hash_token, issue_token, new_service_token

router = APIRouter(tags=["auth"])

# start/poll 限流：每 IP 每 10 分钟最多 30 次
_attempts: dict[str, list] = {}
_RATE_LIMIT = 30
_RATE_WINDOW = timedelta(minutes=10)


def _check_rate(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    now = utcnow()
    window = [t for t in _attempts.get(ip, []) if now - t < _RATE_WINDOW]
    if len(window) >= _RATE_LIMIT:
        raise ApiError("RATE_LIMITED", "尝试过于频繁，请稍后再试", status_code=429)
    window.append(now)
    _attempts[ip] = window


def _admin_url(settings) -> str | None:
    """由中心登录地址推导管理员邀请码页地址（同一认证站点）。"""
    if not settings.auth_login_url:
        return None
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(settings.auth_login_url)
    return urlunsplit((parts.scheme, parts.netloc, "/admin/invitations", "", ""))


@router.get("/login", include_in_schema=False)
def login_redirect(request: Request):
    """跳转中心登录：return_to 只允许指回 KB 自身路径，不接受外部地址。"""
    settings = get_settings()
    if not settings.auth_login_url:
        raise ApiError("AUTH_UNAVAILABLE", "未配置中心登录地址（AUTH_LOGIN_URL）", status_code=503)
    next_path = request.query_params.get("next") or "/inbox"
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = "/inbox"
    return_to = f"{settings.public_base_url}{next_path}"
    from urllib.parse import quote

    sep = "&" if "?" in settings.auth_login_url else "?"
    return RedirectResponse(
        f"{settings.auth_login_url}{sep}return_to={quote(return_to, safe='')}",
        status_code=307,
    )


@router.get("/v1/auth/me")
def auth_me(request: Request, principal=Depends(current_principal), db: Session = Depends(get_db)):
    settings = get_settings()
    return {
        "user_id": principal.user.id,
        "display_name": principal.user.name,
        "auth_method": principal.auth_method,
        "scopes": principal.scopes,
        "central_username": principal.central_username,
        "is_admin": principal.is_admin,
        "has_device": principal.has_device,
        "auth_login_url": settings.auth_login_url or None,
        "admin_url": _admin_url(settings),
    }


class LogoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


@router.post("/v1/auth/logout")
def auth_logout(request: Request, response: Response, principal=Depends(current_principal)):
    """共享退出：撤销当前中心会话并清理 Cookie（中心会话通道）。

    设备 Token 通道不受浏览器退出影响（docs/05 §4.2：区分「退出网站」与「断开设备」）。
    """
    settings = get_settings()
    cookie = request.cookies.get(settings.auth_cookie_name)
    if principal.auth_method == "central_session" and cookie:
        try:
            central_auth.central_logout(cookie)
        except central_auth.CentralAuthUnavailable:
            # 中心不可达也清理本地可见 Cookie；中心会话仍在时由中心自身过期兜底
            pass
    response.delete_cookie(settings.auth_cookie_name, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"logged_out": True}


# ---- 插件设备授权流程 ----

class DeviceStartInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_name: str = Field(min_length=1, max_length=120)


class DeviceStartResult(BaseModel):
    request_id: str
    poll_secret: str
    browser_url: str
    expires_at: str
    interval_seconds: int


class DevicePollInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=8, max_length=64)
    poll_secret: str = Field(min_length=16, max_length=128)


class DeviceApproveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=8, max_length=64)


def _pending_request(db: Session, request_id: str) -> DeviceAuthRequest:
    req = db.get(DeviceAuthRequest, request_id)
    if req is None or len(request_id) != 32:
        raise ApiError("NOT_FOUND", "授权请求不存在", status_code=404)
    if req.expires_at <= utcnow():
        req.state = "expired"
        db.commit()
        raise ApiError("AUTH_EXPIRED", "授权请求已过期，请在插件中重新发起", status_code=410)
    return req


@router.post("/v1/auth/device/start", response_model=DeviceStartResult)
def device_start(body: DeviceStartInput, request: Request, db: Session = Depends(get_db)):
    _check_rate(request)
    settings = get_settings()
    poll_secret = new_service_token()
    now = utcnow()
    req = DeviceAuthRequest(
        device_name=body.device_name[:120],
        poll_secret_hash=hash_token(poll_secret),
        expires_at=now + timedelta(seconds=settings.device_auth_ttl_seconds),
    )
    db.add(req)
    db.commit()
    return DeviceStartResult(
        request_id=req.id,
        poll_secret=poll_secret,
        browser_url=f"{settings.public_base_url}/authorize?request_id={req.id}",
        expires_at=req.expires_at.isoformat(),
        interval_seconds=settings.device_auth_poll_interval_seconds,
    )


@router.get("/v1/auth/device/info")
def device_info(request_id: str, db: Session = Depends(get_db)):
    """授权页展示用：只暴露设备名/状态等非敏感信息；不含有能领取 Token 的数据。"""
    req = _pending_request(db, request_id)
    return {
        "device_name": req.device_name,
        "state": req.state,
        "central_username": req.central_username,
        "expires_at": req.expires_at.isoformat(),
    }


@router.post("/v1/auth/device/approve")
def device_approve(body: DeviceApproveInput, request: Request,
                   principal=Depends(current_principal), db: Session = Depends(get_db)):
    """浏览器批准：绑定中心账号与本地用户；一条请求只能批准一次、不能换账号。"""
    if principal.auth_method != "central_session":
        raise ApiError("FORBIDDEN", "请先在浏览器中登录后再授权设备", status_code=403)
    req = _pending_request(db, body.request_id)
    if req.state != "pending":
        raise ApiError("SCHEMA_INVALID", f"授权请求当前状态为 {req.state}，不能批准", status_code=409)
    local_user = ensure_local_user(db, principal.central_user_id, principal.central_username)
    device = Device(user_id=local_user.id, kind="desktop", name=req.device_name[:120])
    db.add(device)
    db.flush()
    req.state = "approved"
    req.auth_subject = principal.central_user_id
    req.central_username = principal.central_username
    req.local_user_id = local_user.id
    req.device_id = device.id
    req.approved_at = utcnow()
    db.commit()
    return {"approved": True, "device_name": req.device_name}


@router.post("/v1/auth/device/cancel")
def device_cancel(body: DeviceApproveInput, request: Request,
                  principal=Depends(current_principal), db: Session = Depends(get_db)):
    req = _pending_request(db, body.request_id)
    if req.state == "pending":
        req.state = "cancelled"
        db.commit()
    return {"cancelled": True}


@router.post("/v1/auth/device/poll")
def device_poll(body: DevicePollInput, request: Request, db: Session = Depends(get_db)):
    """插件轮询：pending 返回 pending；approved 原子消费并签发设备 Token。"""
    _check_rate(request)
    req = db.get(DeviceAuthRequest, body.request_id)
    if req is None or len(body.request_id) != 32:
        raise ApiError("NOT_FOUND", "授权请求不存在", status_code=404)
    if not secrets.compare_digest(req.poll_secret_hash, hash_token(body.poll_secret)):
        raise ApiError("FORBIDDEN", "poll_secret 不匹配", status_code=403)
    if req.expires_at <= utcnow():
        if req.state in ("pending", "approved"):
            req.state = "expired"
            db.commit()
        raise ApiError("AUTH_EXPIRED", "授权请求已过期，请重新发起", status_code=410)

    if req.state == "pending":
        return {"status": "pending", "expires_at": req.expires_at.isoformat()}
    if req.state == "expired" or req.state == "cancelled":
        raise ApiError("AUTH_EXPIRED", f"授权请求已{('过期' if req.state == 'expired' else '取消')}，请重新发起",
                       status_code=410)
    if req.state == "consumed":
        raise ApiError("AUTH_EXPIRED", "授权请求已被使用，请重新发起", status_code=410)

    # approved：原子消费并签发 Token（状态条件更新保证只消费一次）
    updated = (
        db.query(DeviceAuthRequest)
        .filter(DeviceAuthRequest.id == req.id, DeviceAuthRequest.state == "approved")
        .update({"state": "consumed", "consumed_at": utcnow()})
    )
    if not updated:
        raise ApiError("AUTH_EXPIRED", "授权请求已被使用，请重新发起", status_code=410)
    device = db.get(Device, req.device_id)
    if device is None or device.revoked_at is not None:
        raise ApiError("AUTH_EXPIRED", "批准会话已失效，请重新发起", status_code=410)
    raw, token = issue_token(req.local_user_id, device.id, list(DESKTOP_SCOPES))
    db.add(token)
    db.commit()
    return {
        "status": "ok",
        "token": raw,
        "device_id": device.id,
        "user_id": req.local_user_id,
        "scopes": list(DESKTOP_SCOPES),
        "expires_at": token.expires_at.isoformat(),
    }


@router.post("/v1/devices/{device_id}/disconnect")
def disconnect_device(device_id: str, principal=Depends(require_device), db: Session = Depends(get_db)):
    """插件「断开设备」：撤销当前设备 Token，清理由调用方在本地完成。"""
    if principal.device is None or principal.device.id != device_id:
        raise ApiError("FORBIDDEN", "只能断开当前设备", status_code=403)
    now = utcnow()
    principal.device.revoked_at = now
    db.query(Token).filter(Token.device_id == device_id, Token.revoked_at.is_(None)).update(
        {"revoked_at": now}, synchronize_session=False
    )
    db.commit()
    return {"device_id": device_id, "disconnected": True}
