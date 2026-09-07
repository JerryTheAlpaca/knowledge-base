"""鉴权依赖：Bearer 服务 Token -> (user, device, token)。Scope 不足/无效一律 401/403。"""
from __future__ import annotations

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..domain.errors import ApiError
from ..models import Device, Token, User
from ..repositories import core as repo
from ..security.tokens import has_scope, hash_token, token_valid


def _extract_bearer(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise ApiError("AUTH_EXPIRED", "缺少 Bearer Token", status_code=401)
    return auth[7:].strip()


def current_principal(request: Request, db: Session = Depends(get_db)) -> tuple[User, Device, Token]:
    raw = _extract_bearer(request)
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


def require_scope(scope: str):
    def dep(principal=Depends(current_principal)):
        user, device, token = principal
        if not has_scope(token.scopes_json or [], scope):
            raise ApiError("FORBIDDEN", f"缺少权限：{scope}", status_code=403)
        return principal
    return dep
