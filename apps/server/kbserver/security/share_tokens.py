"""分享链接令牌与短时预览凭据（docs/20 §9.5）。

- 公开分享令牌：至少 32 字节密码学随机；库里存 SHA-256 摘要用于核验，另用按用户
  加密的密文保存原文，让作者能再次复制。它不是账号 Token，也不参与业务鉴权。
- 私有预览凭据：短时（默认 5 分钟）bearer capability，签名载荷
  `{work_id, revision_id, expires_at, nonce}`，不装用户身份或原文。
- 两种令牌都不得进入访问日志、诊断、模型提示与统计；用途密钥各用一份域分隔密钥，
  不与模型凭据的主密钥用途混用。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from datetime import timedelta

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..models import utcnow

PREVIEW_PURPOSE = "share-preview-v1"
TOKEN_PURPOSE = "share-token-v1"
SHARE_TOKEN_BYTES = 32


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def purpose_key(master_key: bytes, purpose: str) -> bytes:
    """从主密钥派生用途密钥：不同用途之间不互相可解。"""
    return hmac.new(master_key, purpose.encode("utf-8"), hashlib.sha256).digest()


def new_share_token() -> str:
    return _b64(secrets.token_bytes(SHARE_TOKEN_BYTES))


def hash_share_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _token_aad(user_id: str, work_id: str) -> bytes:
    return f"{TOKEN_PURPOSE}|{user_id}|{work_id}".encode("utf-8")


def encrypt_share_token(token: str, master_key: bytes, *, user_id: str, work_id: str) -> dict:
    """返回可入库字段：密文、包装后的 DEK 与 nonce（独立用途标识）。"""
    dek = AESGCM.generate_key(bit_length=256)
    secret_nonce = os.urandom(12)
    dek_nonce = os.urandom(12)
    aad = _token_aad(user_id, work_id)
    encrypted = AESGCM(dek).encrypt(secret_nonce, token.encode("utf-8"), aad)
    wrapped = AESGCM(purpose_key(master_key, TOKEN_PURPOSE)).encrypt(dek_nonce, dek, aad)
    return {
        "ciphertext": _b64(encrypted),
        "dek": _b64(wrapped),
        "nonces": {"secret_nonce": _b64(secret_nonce), "dek_nonce": _b64(dek_nonce),
                   "alg": "AES-256-GCM"},
    }


def decrypt_share_token(envelope_cipher: str, dek_cipher: str, nonces: dict, master_key: bytes,
                        *, user_id: str, work_id: str) -> str:
    aad = _token_aad(user_id, work_id)
    kek = AESGCM(purpose_key(master_key, TOKEN_PURPOSE))
    dek = kek.decrypt(_unb64(nonces["dek_nonce"]), _unb64(dek_cipher), aad)
    plain = AESGCM(dek).decrypt(_unb64(nonces["secret_nonce"]), _unb64(envelope_cipher), aad)
    return plain.decode("utf-8")


def issue_preview_token(master_key: bytes, *, work_id: str, revision_id: str,
                        ttl_seconds: int) -> tuple[str, int]:
    """签发短时预览凭据；返回 (token, 过期时间戳)。"""
    expires_at = int((utcnow() + timedelta(seconds=ttl_seconds)).timestamp())
    payload = {
        "work_id": work_id, "rev": revision_id, "exp": expires_at,
        "nonce": secrets.token_hex(8), "purpose": PREVIEW_PURPOSE,
    }
    body = _b64(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    sig = hmac.new(purpose_key(master_key, PREVIEW_PURPOSE), body.encode("ascii"),
                   hashlib.sha256).digest()
    return f"{body}.{_b64(sig)}", expires_at


def read_preview_token(master_key: bytes, token: str) -> dict | None:
    """验签与到期；调用方仍要核对作品未删除、版本仍可用。"""
    try:
        body, sig = token.split(".", 1)
    except ValueError:
        return None
    expected = hmac.new(purpose_key(master_key, PREVIEW_PURPOSE), body.encode("ascii"),
                        hashlib.sha256).digest()
    try:
        if not hmac.compare_digest(expected, _unb64(sig)):
            return None
        payload = json.loads(_unb64(body).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("purpose") != PREVIEW_PURPOSE:
        return None
    if int(payload.get("exp") or 0) < int(utcnow().timestamp()):
        return None
    return payload


def utcnow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
