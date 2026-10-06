"""A 机 ↔ agent 编排容器的服务间凭据（docs/27 §身份打通）。

仓库里原本没有服务间 token（`new_service_token()` 只用于 CSRF 与设备轮询密文），
这里补的就是这一格。**不新增一套 Bearer 体系**：仍然是 HMAC 自包含载荷，
与分享预览凭据同一形态，只是用途密钥独立。

为什么容器里放的是「派生后的用途密钥」而不是 master_key：
agent 容器的边界要求它拿不到 `master_key`（拿到就能解所有人的凭据），而它又必须
能自己签出「回传事件」的凭据。于是部署时把 `purpose_key(master_key, "agent-relay-v1")`
这 32 字节单独作为 secret 挂进容器——它能签/验中继凭据，解不开任何凭据信封。

`site` 由 A 机的中继路由硬编码（知识库的中继只签 "kb"），编排服务不接受请求体
传 site。这是站点隔离在身份层的落点。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import timedelta

from ..models import utcnow
from .share_tokens import purpose_key

RELAY_PURPOSE = "agent-relay-v1"
# 两种用途共用一把派生密钥，靠 sub 区分：A 代表用户去调容器 / 容器把事件回传入库
SUB_USER = "user"
SUB_INGEST = "ingest"


def relay_signing_key(master_key: bytes) -> bytes:
    """部署时写入 agent 容器 secret 的那 32 字节（`scripts/agent-relay-key` 导出）。"""
    return purpose_key(master_key, RELAY_PURPOSE)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def issue_relay_token(signing_key: bytes, *, site: str, user_id: str, subject: str,
                      ttl_seconds: int) -> tuple[str, int]:
    expires_at = int((utcnow() + timedelta(seconds=ttl_seconds)).timestamp())
    payload = {
        "purpose": RELAY_PURPOSE, "sub": subject, "site": site, "user_id": user_id,
        "exp": expires_at, "nonce": secrets.token_hex(8),
    }
    body = _b64(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    sig = hmac.new(signing_key, body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64(sig)}", expires_at


def read_relay_token(signing_key: bytes, token: str, *, expect_subject: str) -> dict | None:
    """验签、到期与用途隔离：回传凭据不能当用户凭据用，反之也不行。"""
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
    if int(payload.get("exp") or 0) < int(utcnow().timestamp()):
        return None
    if not payload.get("site") or not payload.get("user_id"):
        return None
    return payload


def encode_key(signing_key: bytes) -> str:
    return base64.urlsafe_b64encode(signing_key).decode("ascii")


def decode_key(text: str) -> bytes:
    raw = text.strip().encode("ascii")
    key = base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))
    if len(key) != 32:
        raise ValueError("agent 中继密钥必须是 32 字节")
    return key
