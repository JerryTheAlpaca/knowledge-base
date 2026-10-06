"""模型配置选择与 Key 解密（docs/27 §Phase 2）。

网页对话（`workers/share.py`）与 LLM 代理必须用**同一套**选择规则，否则同一个
用户在两处会拿到不同的模型配置，表现为「agent 用的不是我设的默认 Key」。规则从
`workers/share.py` 原样抽出，不在这里改语义：

1. 请求显式带 `profile_id` 时优先用它（调用方已校验归属）；
2. 否则用 `users.settings_json.default_profile_id`；
3. 再否则用最新一份未撤销凭据。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import Credential, ProviderProfile, User
from ..providers.llm import ProviderAuthFailed
from ..security import credentials as cred_crypto


def pick_profile(db: Session, user_id: str, profile_id: str | None):
    """按用户现有云端提炼配置选模型：显式指定优先，其次默认配置，再否则最近一份。"""
    rows = list(db.query(ProviderProfile, Credential).join(
        Credential, Credential.profile_id == ProviderProfile.id
    ).filter(
        ProviderProfile.user_id == user_id,
        ProviderProfile.kind == "llm",
        ProviderProfile.adapter == "openai-compatible",
        Credential.revoked_at.is_(None),
    ).all())
    if profile_id:
        return next(((p, c) for p, c in rows if p.id == profile_id), None)
    if not rows:
        return None
    user = db.get(User, user_id)
    default_id = ((user.settings_json or {}).get("default_profile_id")) if user else None
    return next(((p, c) for p, c in rows if p.id == default_id), None) or max(rows, key=lambda pc: pc[1].created_at)


def reveal_key(db: Session, *, user_id: str, profile_id: str, credential: Credential,
               master_key: bytes) -> str:
    """解出一份凭据的明文 Key。明文只交给单次请求对象，不进日志、不进异常栈。

    解密失败一律按凭据失效处理，绝不回退到别人的 Key（docs/02 §9.1）。
    """
    try:
        return cred_crypto.decrypt_secret(
            credential.encrypted_secret, credential.encrypted_dek, credential.nonces_json,
            master_key,
            user_id=user_id, profile_id=profile_id, credential_version=credential.version,
        )
    except Exception as exc:  # noqa: BLE001 —— 边界：任何解密失败都只报「凭据不可用」
        raise ProviderAuthFailed(f"凭据解密失败：{type(exc).__name__}") from exc
