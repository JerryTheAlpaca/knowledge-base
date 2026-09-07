"""模型凭据 AES-256-GCM 信封加密（docs/02 §9.2）。

- 每条凭据独立随机 DEK；DEK 加密凭据；主密钥 KEK 加密 DEK。
- 每次加密新的 96 位 nonce；绝不复用密钥/nonce 组合。
- AAD 绑定 user_id、profile_id、credential_version、用途，防止换库误解密。
- 明文只在单次调用的请求中使用；接口不提供读回明文。
"""
from __future__ import annotations

import base64

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

AAD_PURPOSE = "model-credential"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data.encode("ascii"))


def new_dek() -> bytes:
    return AESGCM.generate_key(bit_length=256)


def _aad(user_id: str, profile_id: str, credential_version: int) -> bytes:
    return f"{AAD_PURPOSE}|{user_id}|{profile_id}|{credential_version}".encode("utf-8")


def encrypt_secret(
    plaintext: str,
    master_key: bytes,
    *,
    user_id: str,
    profile_id: str,
    credential_version: int,
) -> dict:
    """返回可入库字段：encrypted_secret / encrypted_dek / nonces_json。"""
    import os

    dek = new_dek()
    secret_nonce = os.urandom(12)
    dek_nonce = os.urandom(12)

    aes = AESGCM(dek)
    encrypted_secret = aes.encrypt(secret_nonce, plaintext.encode("utf-8"), _aad(user_id, profile_id, credential_version))

    kek = AESGCM(master_key)
    encrypted_dek = kek.encrypt(dek_nonce, dek, _aad(user_id, profile_id, credential_version))

    return {
        "encrypted_secret": _b64(encrypted_secret),
        "encrypted_dek": _b64(encrypted_dek),
        "nonces_json": {"secret_nonce": _b64(secret_nonce), "dek_nonce": _b64(dek_nonce), "alg": "AES-256-GCM"},
    }


def decrypt_secret(
    encrypted_secret: str,
    encrypted_dek: str,
    nonces: dict,
    master_key: bytes,
    *,
    user_id: str,
    profile_id: str,
    credential_version: int,
) -> str:
    kek = AESGCM(master_key)
    dek = kek.decrypt(
        _unb64(nonces["dek_nonce"]), _unb64(encrypted_dek), _aad(user_id, profile_id, credential_version)
    )
    aes = AESGCM(dek)
    plaintext = aes.decrypt(
        _unb64(nonces["secret_nonce"]), _unb64(encrypted_secret), _aad(user_id, profile_id, credential_version)
    )
    return plaintext.decode("utf-8")


def wrap_dek_for_rotation(encrypted_dek: str, nonces: dict, old_master_key: bytes, new_master_key: bytes, *,
                          user_id: str, profile_id: str, credential_version: int) -> tuple[str, dict]:
    """主密钥轮换：先重新包装 DEK 并验证可解密（docs/02 §9.2 轮换）。"""
    kek_old = AESGCM(old_master_key)
    dek = kek_old.decrypt(_unb64(nonces["dek_nonce"]), _unb64(encrypted_dek), _aad(user_id, profile_id, credential_version))
    import os

    new_dek_nonce = os.urandom(12)
    kek_new = AESGCM(new_master_key)
    new_encrypted_dek = kek_new.encrypt(new_dek_nonce, dek, _aad(user_id, profile_id, credential_version))
    return _b64(new_encrypted_dek), {**nonces, "dek_nonce": _b64(new_dek_nonce)}
