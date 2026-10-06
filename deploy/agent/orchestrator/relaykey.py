"""中继凭据的验签与签发（容器侧独立一份，不依赖 kbserver 包）。

载荷形态必须与 A 机 `kbserver/security/agent_relay.py` 完全一致：同样的字段、
同样的 `sort_keys=True` 紧凑 JSON、同样的 base64url 去填充。两边不共享代码是
刻意的 —— 容器里不该出现服务端业务包，但**测试会逐字节比对两边签出的同一载荷**，
漂移了就会红。

容器拿到的 secret 是 `purpose_key(master_key, "agent-relay-v1")` 这 32 字节，
不是 master_key：它能签/验中继凭据，解不开任何模型凭据信封。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

RELAY_PURPOSE = "agent-relay-v1"
SUB_USER = "user"
SUB_INGEST = "ingest"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def decode_key(text: str) -> bytes:
    raw = text.strip().encode("ascii")
    key = _unb64(raw.decode("ascii"))
    if len(key) != 32:
        raise ValueError("agent 中继密钥必须是 32 字节")
    return key


def encode_key(key: bytes) -> str:
    return _b64(key)


def canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


def issue(signing_key: bytes, *, site: str, user_id: str, subject: str,
          ttl_seconds: int) -> tuple[str, int]:
    expires_at = int(time.time()) + int(ttl_seconds)
    payload = {
        "purpose": RELAY_PURPOSE, "sub": subject, "site": site, "user_id": user_id,
        "exp": expires_at, "nonce": secrets.token_hex(8),
    }
    body = _b64(canonical(payload))
    sig = hmac.new(signing_key, body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64(sig)}", expires_at


def read(signing_key: bytes, token: str, *, expect_subject: str) -> dict[str, Any] | None:
    try:
        body, sig = token.split(".", 1)
    except ValueError:
        return None
    expected = hmac.new(signing_key, body.encode("ascii"), hashlib.sha256).digest()
    try:
        if not hmac.compare_digest(expected, _unb64(sig)):
            return None
        payload = json.loads(_unb64(body).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("purpose") != RELAY_PURPOSE:
        return None
    if payload.get("sub") != expect_subject:
        return None
    if int(payload.get("exp") or 0) < int(time.time()):
        return None
    if not payload.get("site") or not payload.get("user_id"):
        return None
    return payload


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
