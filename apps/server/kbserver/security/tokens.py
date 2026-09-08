"""服务 Token 与配对码（docs/02 §9.1）。

- Token 至少 32 字节密码学随机；数据库只存 SHA-256 摘要。
- Token 不出现在 URL、日志或错误消息里。
- 手机 scopes：captures:create, uploads:create；桌面 scopes 另发。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from ..config import get_settings
from ..models import PairingCode, Token, utcnow

PHONE_SCOPES = ["captures:create", "uploads:create"]
DESKTOP_SCOPES = [
    "items:read",
    "receipts:write",
    "captures:create",
    "items:edit",
    "profiles:manage",
    "devices:manage",
]
# Web 收件箱：桌面同级权限 + uploads:create（收件箱要上传补充材料，docs/02 §2.1）
WEB_SCOPES = [
    "items:read",
    "receipts:write",
    "captures:create",
    "uploads:create",
    "items:edit",
    "profiles:manage",
    "devices:manage",
]


def hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_service_token() -> str:
    # 32 字节随机 -> 43 字符 urlsafe base64，前缀便于识别与扫描
    return "kbi_" + secrets.token_urlsafe(32)


def new_pairing_code() -> str:
    return "KBP-" + secrets.token_hex(4).upper() + "-" + secrets.token_hex(4).upper()


def constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def issue_token(user_id: str, device_id: str, scopes: list[str]) -> tuple[str, Token]:
    settings = get_settings()
    raw = new_service_token()
    token = Token(
        user_id=user_id,
        device_id=device_id,
        token_hash=hash_token(raw),
        scopes_json=scopes,
        expires_at=utcnow() + timedelta(days=settings.token_ttl_days),
    )
    return raw, token


def issue_pairing_code(user_id: str, device_kind: str, scopes: list[str]) -> tuple[str, PairingCode]:
    settings = get_settings()
    raw = new_pairing_code()
    code = PairingCode(
        user_id=user_id,
        device_kind=device_kind,
        code_hash=hash_token(raw),
        scopes_json=scopes,
        expires_at=utcnow() + timedelta(minutes=settings.pairing_code_ttl_minutes),
    )
    return raw, code


def token_valid(token: Token, now: datetime | None = None) -> bool:
    now = now or utcnow()
    return token.revoked_at is None and token.expires_at > now


def pairing_code_valid(code: PairingCode, now: datetime | None = None) -> bool:
    now = now or utcnow()
    return code.used_at is None and code.expires_at > now


def has_scope(scopes: list[str], required: str) -> bool:
    return required in scopes
