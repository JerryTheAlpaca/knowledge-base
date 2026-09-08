"""鉴权依赖（docs/05 §4.2）：两条明确通道 -> Principal。

- Bearer 服务 Token（插件/已授权采集设备）：本地 Device/Token，携带 device。
- Web 中心会话 Cookie：服务端只提取指定中心 Cookie 发给固定校验接口，
  读取真实用户 ID 后映射本地用户；不伪造数据库 Token，device=None。
  写操作必须带 X-CSRF-Token（双提交 Cookie）并通过 Origin 校验。

显式传入无效 Bearer 时直接拒绝，不回退浏览器 Cookie 换身份。
Scope 不足/无效一律 401/403；同步回执等设备专属操作要求 device 通道。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from fastapi import Depends, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..domain.errors import ApiError
from ..models import Device, Token, User, utcnow
from ..repositories import core as repo
from ..security import central_auth
from ..security.tokens import (
    WEB_SCOPES,
    constant_time_eq,
    has_scope,
    hash_token,
    token_valid,
)

SESSION_COOKIE = "kb_session"  # 旧 Web 配对会话 Cookie：不再用于认证（docs/05 §4.5）
CSRF_COOKIE = "kb_csrf"
CSRF_HEADER = "X-CSRF-Token"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@dataclass
class Principal:
    """小型身份载体：本地用户、业务 scopes、认证方式、可选设备。"""

    user: User
    scopes: list[str] = field(default_factory=list)
    auth_method: str = "device_token"  # device_token | central_session
    device: Device | None = None
    token: Token | None = None
    # 中心会话通道附带的账号信息（脱敏展示与 /v1/auth/me 用）
    central_user_id: str | None = None
    central_username: str | None = None
    central_role: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.central_role == "admin"

    @property
    def has_device(self) -> bool:
        return self.device is not None


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


def _load_device_principal(db: Session, raw: str) -> Principal:
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
    return Principal(
        user=user, scopes=list(token.scopes_json or []),
        auth_method="device_token", device=device, token=token,
    )


def ensure_local_user(db: Session, central_user_id: str, username: str) -> User:
    """按中心不可变 user.id 幂等映射/创建本地用户（docs/05 §4.3）。

    并发首次访问靠 auth_subject 唯一约束收敛到一行；绑定只增加身份映射，
    保留知识库原 user_id，不动已有材料与凭据。
    """
    user = db.query(User).filter(User.auth_subject == central_user_id).one_or_none()
    if user is not None:
        return user
    user = User(name=username or central_user_id, auth_subject=central_user_id)
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        user = db.query(User).filter(User.auth_subject == central_user_id).one()
    return user


def _load_central_principal(db: Session, request: Request) -> Principal:
    """中心会话 Cookie 通道：校验 -> 映射本地用户 -> 固定 Web scopes。"""
    settings = get_settings()
    cookie = request.cookies.get(settings.auth_cookie_name)
    if not cookie:
        raise ApiError("AUTH_EXPIRED", "未登录", status_code=401)
    try:
        data, renewal = central_auth.validate_central_session(cookie)
    except central_auth.CentralAuthRejected as exc:
        raise ApiError("AUTH_EXPIRED", str(exc), status_code=401) from exc
    except central_auth.CentralAuthUnavailable as exc:
        raise ApiError("AUTH_UNAVAILABLE", f"认证服务暂不可用：{exc}", status_code=503) from exc
    cuser = data.get("user") or {}
    # 续期 Cookie：暂存到 request.state，由 app 中间件统一转发给浏览器
    if renewal:
        request.state.central_renewal = renewal
    # 会话有效但浏览器还没有 CSRF Cookie：补发一个（双提交用）
    if not request.cookies.get(CSRF_COOKIE):
        from ..security.tokens import new_service_token

        request.state.kb_csrf_issue = new_service_token()
    user = ensure_local_user(db, str(cuser["id"]), str(cuser.get("username") or ""))
    if user.status != "active":
        raise ApiError("AUTH_EXPIRED", "用户不可用", status_code=401)
    return Principal(
        user=user, scopes=list(WEB_SCOPES), auth_method="central_session",
        central_user_id=str(cuser["id"]),
        central_username=str(cuser.get("username") or ""),
        central_role=str(cuser.get("role") or "user"),
    )


def current_principal(request: Request, db: Session = Depends(get_db)) -> Principal:
    raw = _extract_bearer(request)
    if raw:
        # 显式 Bearer：无效直接拒绝，不回退浏览器 Cookie
        return _load_device_principal(db, raw)

    principal = _load_central_principal(db, request)
    _check_csrf(request)
    return principal


def require_scope(scope: str):
    def dep(principal=Depends(current_principal)):
        if not has_scope(principal.scopes, scope):
            raise ApiError("FORBIDDEN", f"缺少权限：{scope}", status_code=403)
        return principal
    return dep


def require_device(principal=Depends(current_principal)) -> Principal:
    """要求合法设备通道（同步回执等，docs/05 §4.2）。"""
    if not principal.has_device:
        raise ApiError("FORBIDDEN", "该操作需要已授权设备", status_code=403)
    return principal
