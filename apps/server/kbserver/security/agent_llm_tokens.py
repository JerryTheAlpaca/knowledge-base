"""Agent 运行期凭据：LLM 代理会话 token 与 MCP 能力 token（docs/27 §Phase 2、§身份打通）。

形态照 `security/share_tokens.py` 的私有预览凭据：HMAC 签名的自包含载荷，不落库、
不能撤销，靠短 TTL 收口。三种用途各自一把派生密钥（凭据主密钥、分享令牌、这两种），
互相都解不开。

载荷只放归属，**不放模型 Key，也不放原文**。

为什么网页对话还需要一枚 MCP 能力 token：用户在网页里点「对话」时用的是中心会话
Cookie，而容器里的 dsh 要以 Bearer 连 `/mcp`。给它用户那枚 180 天的 agent token
既不现实（面板里不该有长期凭据流转）也不安全；所以由 A 机在容器每次要凭据时现签
一枚 15 分钟的、只带 `mcp:read/mcp:write` 与 `user_id` 的能力 token。
它不扩权：能做的事与「这个已登录用户自己在面板里操作」完全一样。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import timedelta

from ..config import get_settings
from ..models import utcnow
from .share_tokens import purpose_key

LLM_PROXY_PURPOSE = "llm-proxy-v1"
MCP_CAPABILITY_PURPOSE = "mcp-capability-v1"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _issue(master_key: bytes, purpose: str, fields: dict, ttl_seconds: int) -> tuple[str, int]:
    expires_at = int((utcnow() + timedelta(seconds=ttl_seconds)).timestamp())
    payload = dict(fields)
    payload.update(purpose=purpose, exp=expires_at, nonce=secrets.token_hex(8))
    body = _b64(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    sig = hmac.new(purpose_key(master_key, purpose), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64(sig)}", expires_at


def _read(master_key: bytes, purpose: str, token: str, required: tuple[str, ...]) -> dict | None:
    try:
        body, sig = token.split(".", 1)
    except ValueError:
        return None
    expected = hmac.new(purpose_key(master_key, purpose), body.encode("ascii"),
                        hashlib.sha256).digest()
    try:
        if not hmac.compare_digest(expected, _unb64(sig)):
            return None
        payload = json.loads(_unb64(body).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("purpose") != purpose:
        return None
    if int(payload.get("exp") or 0) < int(utcnow().timestamp()):
        return None
    for key in required:
        if not payload.get(key):
            return None
    return payload


def issue_llm_token(master_key: bytes, *, site: str, user_id: str, profile_id: str,
                    ttl_seconds: int | None = None) -> tuple[str, int]:
    """签发一次会话的 LLM 代理凭据；返回 (token, 过期 Unix 时间戳)。"""
    ttl = get_settings().agent_llm_session_ttl_seconds if ttl_seconds is None else ttl_seconds
    return _issue(master_key, LLM_PROXY_PURPOSE,
                  {"site": site, "user_id": user_id, "profile_id": profile_id}, ttl)


def read_llm_token(master_key: bytes, token: str) -> dict | None:
    """验签与到期。调用方仍要核对 profile 归属与预算，不靠 token 本身授权写操作。"""
    return _read(master_key, LLM_PROXY_PURPOSE, token, ("site", "user_id", "profile_id"))


def issue_mcp_capability(master_key: bytes, *, site: str, user_id: str,
                         scopes: list[str], ttl_seconds: int | None = None) -> tuple[str, int]:
    ttl = get_settings().agent_llm_session_ttl_seconds if ttl_seconds is None else ttl_seconds
    return _issue(master_key, MCP_CAPABILITY_PURPOSE,
                  {"site": site, "user_id": user_id, "scopes": list(scopes)}, ttl)


def read_mcp_capability(master_key: bytes, token: str) -> dict | None:
    """能力 token 与 LLM 会话 token 是两把用途密钥：互相顶替在这里过不了验签。"""
    payload = _read(master_key, MCP_CAPABILITY_PURPOSE, token, ("site", "user_id", "scopes"))
    if payload is None or not isinstance(payload["scopes"], list) or not payload["scopes"]:
        return None
    return payload
