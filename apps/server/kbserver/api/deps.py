"""鉴权依赖：Bearer 服务 Token -> (user, device, token)。Scope 不足/无效一律 401/403。

Web 收件箱（docs/02 §9.1）：无 Bearer 头时回退读会话 Cookie（HttpOnly，仅 kind=web
设备签发）。Cookie 通道的写操作必须带 X-CSRF-Token（双提交 Cookie）并通过 Origin 校验，
防止跨站请求伪造；Bearer 通道不受 CSRF 约束（不存在浏览器自动携带问题）。
"""
from __future__ import annotations

from urllib.parse import urlparse

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..domain.errors import ApiError
from ..models import Device, Token, User
from ..repositories import core as repo
from ..security.tokens import constant_time_eq, has_scope, hash_token, token_valid

SESSION_COOKIE = "kb_session"
CSRF_COOKIE = "kb_csrf"
CSRF_HEADER = "X-CSRF-Token"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _extract_bearer(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if not auth:
        return None
    if not auth.startswith("Bearer "):
        raise ApiError("AUTH_EXPIRED", "缺少 Bearer Token", status_code=401)
    return auth[7:].strip()


def _origin_allowed(request: Request) -> bool:
    origin = request.headers.get("Origin")
    if not origin:
        return True  # 非浏览器客户端没有 Origin；CSRF 由双提交 Token 保证
    host = (urlparse(origin).hostname or "").lower()
    request_host = (urlparse(str(request.base_url)).hostname or "").lower()
    base_host = (urlparse(get_settings().public_base_url).hostname or "").lower()
    return host in {request_host, base_host}


def _check_csrf(request: Request) -> None:
    if request.method.upper() not in UNSAFE_METHODS:
        return
    cookie = request.cookies.get(CSRF_COOKIE) or ""
    header = request.headers.get(CSRF_HEADER) or ""
    if not cookie or not header or not constant_time_eq(cookie, header):
        raise ApiError("FORBIDDEN", "缺少或错误的 CSRF Token", status_code=403)
    if not _origin_allowed(request):
        raise ApiError("FORBIDDEN", "Origin 不被允许", status_code=403)


def _load_principal(db: Session, raw: str) -> tuple[User, Device, Token]:
    if len(raw) < 40 or len(raw) > 128:
        raise ApiError("AUTH_EXPIRED", "Token 无效", status_code=401)
    token = db.query(Token).filter(Token.token_hash == hash_token(raw)).one_or_none()
    if token is None or not token_valid(token):
        raise ApiError("AUTH_EXPIRED", "Token 无效或已过期", status_code=401)
    user = repo.get_user(db, token.user_id)
    if user is None or user.status != "active":
        raise ApiError("AUTH_EXPIRED", "用户不可用", status_code=401)
    device = repo.get_device(db, token.user_id, token.device_id)
    if device is None or device.revoked_at is not None:
        raise ApiError("AUTH_EXPIRED", "设备已撤销", status_code=401)
    return user, device, token


def current_principal(request: Request, db: Session = Depends(get_db)) -> tuple[User, Device, Token]:
    raw = _extract_bearer(request)
    if raw:
        return _load_principal(db, raw)

    # Cookie 会话通道：仅限 Web 收件箱设备
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        raise ApiError("AUTH_EXPIRED", "缺少 Bearer Token", status_code=401)
    user, device, token = _load_principal(db, cookie)
    if device.kind != "web":
        raise ApiError("AUTH_EXPIRED", "会话无效", status_code=401)
    _check_csrf(request)
    return user, device, token


def require_scope(scope: str):
    def dep(principal=Depends(current_principal)):
        user, device, token = principal
        if not has_scope(token.scopes_json or [], scope):
            raise ApiError("FORBIDDEN", f"缺少权限：{scope}", status_code=403)
        return principal
    return dep
