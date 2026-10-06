"""服务 Token 与配对码（docs/02 §9.1；docs/08 §8.3）。

- Token 至少 32 字节密码学随机；数据库只存 SHA-256 摘要。
- Token 不出现在 URL、日志或错误消息里。
- 手机 scopes：captures:create, uploads:create；桌面 scopes 另发。
- `profiles:bind-local` 是专用权限：只有用户在设备授权时明确选择，才允许把
  某个线上配置的 Key 下发到该设备用于本地直连模型；不随 profiles:manage 自动获得。
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
# 设备授权时可额外申请的权限（docs/08 §8.3）：不能仅凭旧设备的普通
# profiles:manage 自动获得导出能力，必须由用户在授权页显式勾选。
BIND_LOCAL_SCOPE = "profiles:bind-local"
OPTIONAL_DEVICE_SCOPES = [BIND_LOCAL_SCOPE]
# Web 收件箱：桌面同级权限 + uploads:create（收件箱要上传补充材料，docs/02 §2.1）
# shares:* 只在 Web 通道开放：本地插件首版不需要，也不自动扩大旧设备 Token 权限（docs/20 §12）
WEB_SCOPES = [
    "items:read",
    "receipts:write",
    "captures:create",
    "uploads:create",
    "items:edit",
    "profiles:manage",
    "devices:manage",
    "shares:read",
    "shares:write",
]

# ---- Agent Token（docs/27 §agent token 生命周期）----
#
# 单独一类，不复用设备 Token 语义：撤销设备 Token 会连带掐掉 Obsidian 同步回执通道，
# 而 agent Token 只服务 MCP 与对话入口，一键停用不能影响同步。
# 权限按 MCP 工具面切：mcp:read 只读，mcp:write 才能往收件箱投东西。
MCP_READ_SCOPE = "mcp:read"
MCP_WRITE_SCOPE = "mcp:write"
AGENT_SCOPES = [MCP_READ_SCOPE, MCP_WRITE_SCOPE]
# Device.kind 的新取值：与 phone|desktop|web 并列，设备列表与同步链路都不会误认它是同步端
AGENT_DEVICE_KIND = "agent"


def hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_service_token() -> str:
    # 32 字节随机 -> 43 字符 urlsafe base64，前缀便于识别与扫描
    return "kbi_" + secrets.token_urlsafe(32)


def new_pairing_code() -> str:
    return "KBP-" + secrets.token_hex(4).upper() + "-" + secrets.token_hex(4).upper()


def constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def issue_token(user_id: str, device_id: str, scopes: list[str],
                ttl_days: int | None = None) -> tuple[str, Token]:
    settings = get_settings()
    raw = new_service_token()
    token = Token(
        user_id=user_id,
        device_id=device_id,
        token_hash=hash_token(raw),
        scopes_json=scopes,
        expires_at=utcnow() + timedelta(days=settings.token_ttl_days if ttl_days is None else ttl_days),
    )
    return raw, token


def issue_agent_token(user_id: str, device_id: str, scopes: list[str]) -> tuple[str, Token]:
    """Agent Token 走同一张 tokens 表与同一条校验路径，只有有效期不同（docs/27）。"""
    return issue_token(user_id, device_id, scopes, ttl_days=get_settings().agent_token_ttl_days)


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
