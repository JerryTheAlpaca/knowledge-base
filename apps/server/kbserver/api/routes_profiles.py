"""模型配置与凭据托管（docs/02 §9.2、§10.1；docs/05 §5 去计费）。

- 凭据只进不出：任何读取接口不返回明文、掩码或可还原形式，只返回 configured 状态。
- PATCH 凭据产生新版本并撤销旧版本；更新后 waiting_key 条目自动重新排队。
- 连接测试限频（每配置 60 秒一次）；仅返回连通结果，无金额/用量。
- 不统计模型 API 用量、价格和估算费用；供应商账单由用户在供应商平台查看。
"""
from __future__ import annotations

import time
from urllib.parse import urlparse

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..api.deps import require_scope
from ..domain import pipeline, provider_ops
from ..domain.errors import ApiError
from ..models import Credential, Item, ProviderOperation, ProviderProfile, User, utcnow
from ..providers.llm import (
    GenerateRequest,
    OpenAICompatibleProvider,
    ProviderError,
    ProviderOutcomeUnknown,
)
from ..repositories import core as repo
from ..security import credentials as cred_crypto

router = APIRouter(tags=["profiles"])

ALLOWED_KINDS = {"llm", "vision_ocr"}
ALLOWED_ADAPTERS = {"openai-compatible"}
ALLOWED_CAPABILITY_KEYS = {
    "context_tokens": int,
    "max_output_tokens": int,
    "timeout_seconds": int,
    "temperature": bool,
    "json_mode": bool,
    "vision": bool,
}

_TEST_LAST_AT: dict[tuple[str, str], float] = {}
TEST_MIN_INTERVAL_SECONDS = 60


def _validate_endpoint(endpoint: str) -> str:
    endpoint = endpoint.strip()
    if not endpoint.startswith("https://"):
        raise ApiError("SCHEMA_INVALID", "模型 endpoint 必须是 HTTPS 地址")
    if len(endpoint) > 512:
        raise ApiError("SCHEMA_INVALID", "endpoint 过长")
    host = (urlparse(endpoint).hostname or "").lower()
    if not host:
        raise ApiError("SCHEMA_INVALID", "endpoint 缺少主机名")
    settings = get_settings()
    allowed = settings.provider_allowed_origins
    if "*" not in allowed and host not in allowed:
        raise ApiError(
            "SCHEMA_INVALID",
            f"endpoint 主机不在允许列表：{host}；管理员可通过 PROVIDER_ALLOWED_ORIGINS 扩展",
        )
    return endpoint


def _validate_capabilities(caps: dict | None) -> dict:
    if caps is None:
        return {}
    if not isinstance(caps, dict):
        raise ApiError("SCHEMA_INVALID", "capabilities 必须是对象")
    out: dict = {}
    for key, value in caps.items():
        if key not in ALLOWED_CAPABILITY_KEYS:
            raise ApiError("SCHEMA_INVALID", f"未知能力字段：{key}")
        expected = ALLOWED_CAPABILITY_KEYS[key]
        if expected is int:
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ApiError("SCHEMA_INVALID", f"能力 {key} 必须是正整数")
        else:
            if not isinstance(value, bool):
                raise ApiError("SCHEMA_INVALID", f"能力 {key} 必须是布尔值")
        out[key] = value
    return out


# ---- 模型配置 ----

class ProfileCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = "llm"
    adapter: str = "openai-compatible"
    endpoint: str
    model: str = Field(min_length=1, max_length=120)
    capabilities: dict | None = None
    secret: str = Field(min_length=8, max_length=4096)


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint: str | None = None
    model: str | None = Field(default=None, min_length=1, max_length=120)
    capabilities: dict | None = None
    secret: str | None = Field(default=None, min_length=8, max_length=4096)
    expected_version: int | None = None


class ProfileOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    kind: str
    adapter: str
    endpoint: str
    model: str
    capabilities: dict
    version: int
    configured: bool
    credential_version: int | None
    created_at: str


def _profile_out(db: Session, profile: ProviderProfile) -> ProfileOut:
    cred = (
        db.query(Credential)
        .filter(Credential.profile_id == profile.id, Credential.revoked_at.is_(None))
        .order_by(Credential.created_at.desc())
        .first()
    )
    return ProfileOut(
        id=profile.id,
        kind=profile.kind,
        adapter=profile.adapter,
        endpoint=profile.endpoint,
        model=profile.model,
        capabilities=profile.capabilities_json or {},
        version=profile.version,
        configured=cred is not None,
        credential_version=cred.version if cred else None,
        created_at=profile.created_at.isoformat(),
    )


def _require_profile(db: Session, user_id: str, profile_id: str) -> ProviderProfile:
    profile = db.query(ProviderProfile).filter(
        ProviderProfile.id == profile_id, ProviderProfile.user_id == user_id
    ).one_or_none()
    if profile is None:
        # 未知对象与他人对象统一 404（docs/02 §10.2）
        raise ApiError("NOT_FOUND", "配置不存在", status_code=404)
    return profile


def _requeue_waiting(db: Session, user_id: str, state: str, stage: str) -> int:
    items = (
        db.query(Item)
        .filter(Item.user_id == user_id, Item.pipeline_state == state, Item.deleted_at.is_(None))
        .all()
    )
    for it in items:
        pipeline.enqueue_stage(
            db, user_id=user_id, item_id=it.id, source_revision=it.source_revision,
            stage=stage, reset_attempt=True,
        )
    return len(items)


@router.get("/v1/provider-profiles", response_model=list[ProfileOut])
def list_profiles(principal=Depends(require_scope("profiles:manage")), db: Session = Depends(get_db)):
    user = principal.user
    rows = db.query(ProviderProfile).filter(ProviderProfile.user_id == user.id).order_by(ProviderProfile.created_at).all()
    return [_profile_out(db, p) for p in rows]


@router.post("/v1/provider-profiles", response_model=ProfileOut, status_code=201)
def create_profile(body: ProfileCreate, principal=Depends(require_scope("profiles:manage")), db: Session = Depends(get_db)):
    user = principal.user
    if body.kind not in ALLOWED_KINDS:
        raise ApiError("SCHEMA_INVALID", f"kind 仅支持 {sorted(ALLOWED_KINDS)}")
    if body.adapter not in ALLOWED_ADAPTERS:
        raise ApiError("SCHEMA_INVALID", f"adapter 仅支持 {sorted(ALLOWED_ADAPTERS)}")
    endpoint = _validate_endpoint(body.endpoint)
    caps = _validate_capabilities(body.capabilities)

    profile = ProviderProfile(
        user_id=user.id,
        kind=body.kind,
        adapter=body.adapter,
        endpoint=endpoint,
        model=body.model,
        capabilities_json=caps,
        version=1,
    )
    db.add(profile)
    db.flush()
    encrypted = cred_crypto.encrypt_secret(
        body.secret, get_settings().load_master_key(),
        user_id=user.id, profile_id=profile.id, credential_version=1,
    )
    db.add(Credential(user_id=user.id, profile_id=profile.id, version=1, master_key_version=get_settings().master_key_version, **encrypted))
    _requeue_waiting(db, user.id, "waiting_key", "enrich")
    db.commit()
    db.refresh(profile)
    return _profile_out(db, profile)


@router.patch("/v1/provider-profiles/{profile_id}", response_model=ProfileOut)
def update_profile(profile_id: str, body: ProfileUpdate, principal=Depends(require_scope("profiles:manage")), db: Session = Depends(get_db)):
    user = principal.user
    profile = _require_profile(db, user.id, profile_id)
    if body.expected_version is not None and body.expected_version != profile.version:
        raise ApiError("REVISION_CONFLICT", "配置版本已变化，请刷新后重试", status_code=409)

    changed = False
    if body.endpoint is not None and body.endpoint != profile.endpoint:
        profile.endpoint = _validate_endpoint(body.endpoint)
        changed = True
    if body.model is not None and body.model != profile.model:
        profile.model = body.model
        changed = True
    if body.capabilities is not None:
        profile.capabilities_json = _validate_capabilities(body.capabilities)
        changed = True
    if changed:
        profile.version += 1

    requeued = 0
    if body.secret is not None:
        current = (
            db.query(Credential)
            .filter(Credential.profile_id == profile.id, Credential.revoked_at.is_(None))
            .order_by(Credential.created_at.desc())
            .first()
        )
        next_version = (current.version if current else 0) + 1
        if current:
            current.revoked_at = utcnow()
        encrypted = cred_crypto.encrypt_secret(
            body.secret, get_settings().load_master_key(),
            user_id=user.id, profile_id=profile.id, credential_version=next_version,
        )
        db.add(Credential(user_id=user.id, profile_id=profile.id, version=next_version,
                          master_key_version=get_settings().master_key_version, **encrypted))
        # 新凭据生效：等待 Key 的条目自动继续（docs/02 §8.1 waiting_key -> queued）
        requeued = _requeue_waiting(db, user.id, "waiting_key", "enrich")
    db.commit()
    db.refresh(profile)
    out = _profile_out(db, profile)
    if requeued:
        pipeline.emit_event(db, user.id, item_id=None, bundle_revision=None,
                            event_type="credentials_updated", payload={"profile_id": profile.id, "requeued": requeued})
        db.commit()
    return out


@router.delete("/v1/provider-profiles/{profile_id}/credential")
def revoke_credential(profile_id: str, principal=Depends(require_scope("profiles:manage")), db: Session = Depends(get_db)):
    user = principal.user
    profile = _require_profile(db, user.id, profile_id)
    now = utcnow()
    revoked = 0
    for cred in db.query(Credential).filter(
        Credential.profile_id == profile.id, Credential.revoked_at.is_(None)
    ).all():
        cred.revoked_at = now
        revoked += 1
    db.commit()
    return {"profile_id": profile.id, "revoked": True, "revoked_versions": revoked,
            "note": "后续加工任务将进入 waiting_key；已产生的调用记录不受影响。"}


# ---- 连接测试 ----

@router.post("/v1/provider-profiles/{profile_id}/test")
def test_profile(profile_id: str, principal=Depends(require_scope("profiles:manage")), db: Session = Depends(get_db)):
    """连通性测试：只验证凭据与模型可达，不返回金额或用量。"""
    user = principal.user
    profile = _require_profile(db, user.id, profile_id)
    key = (user.id, profile.id)
    now = time.monotonic()
    last = _TEST_LAST_AT.get(key)
    if last is not None and now - last < TEST_MIN_INTERVAL_SECONDS:
        raise ApiError("RATE_LIMITED", f"连接测试每 {TEST_MIN_INTERVAL_SECONDS} 秒限一次", status_code=429)
    _TEST_LAST_AT[key] = now

    cred = (
        db.query(Credential)
        .filter(Credential.profile_id == profile.id, Credential.revoked_at.is_(None))
        .order_by(Credential.created_at.desc())
        .first()
    )
    if cred is None:
        raise ApiError("SCHEMA_INVALID", "该配置还没有可用的凭据")

    settings = get_settings()
    try:
        api_key = cred_crypto.decrypt_secret(
            cred.encrypted_secret, cred.encrypted_dek, cred.nonces_json,
            settings.load_master_key(),
            user_id=user.id, profile_id=profile.id, credential_version=cred.version,
        )
    except Exception as exc:
        raise ApiError("PROVIDER_AUTH_FAILED", f"凭据解密失败：{type(exc).__name__}", status_code=422) from exc

    op = provider_ops.create_operation(
        db, user_id=user.id, job_id=None, profile_id=profile.id,
        request_fingerprint=pipeline.sha256_hex(f"test|{profile.id}".encode())[:32],
    )
    provider_ops.mark_sent(op)
    db.commit()

    provider = OpenAICompatibleProvider(
        endpoint=profile.endpoint, api_key=api_key, model=profile.model,
        capabilities=profile.capabilities_json or {},
        timeout_seconds=int((profile.capabilities_json or {}).get("timeout_seconds") or 30),
    )
    try:
        result = provider.generate(GenerateRequest(
            system="你是连接测试探针。", user="连接测试：请只回复 OK。",
            max_output_tokens=16, temperature=0.0, json_mode=False,
        ))
    except ProviderOutcomeUnknown as exc:
        provider_ops.mark_unknown(op, "连接测试结果未知")
        db.commit()
        return {"ok": False, "code": "PROVIDER_OUTCOME_UNKNOWN", "message": str(exc)}
    except ProviderError as exc:
        provider_ops.finish_operation(op, "failed", f"连接测试失败：{type(exc).__name__}")
        db.commit()
        return {"ok": False, "code": type(exc).__name__, "message": str(exc)}

    provider_ops.finish_operation(op, "succeeded", "连接测试通过")
    db.commit()
    return {"ok": True, "request_id": result.provider_request_id}


# ---- 设置 ----

class SettingsOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default_profile_id: str | None
    note: str = ""


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default_profile_id: str | None = None


def _settings_out(user: User) -> SettingsOut:
    s = user.settings_json or {}
    return SettingsOut(
        default_profile_id=s.get("default_profile_id"),
        note="模型账单请在供应商平台查看；本系统不统计用量与费用。",
    )


@router.get("/v1/settings", response_model=SettingsOut)
def get_settings_route(principal=Depends(require_scope("profiles:manage")), db: Session = Depends(get_db)):
    user = principal.user
    return _settings_out(user)


@router.patch("/v1/settings", response_model=SettingsOut)
def update_settings(body: SettingsUpdate, principal=Depends(require_scope("profiles:manage")), db: Session = Depends(get_db)):
    user = principal.user
    s = dict(user.settings_json or {})
    if body.default_profile_id is not None:
        if body.default_profile_id:
            _require_profile(db, user.id, body.default_profile_id)
        s["default_profile_id"] = body.default_profile_id or None
    user.settings_json = s
    db.commit()
    return _settings_out(user)


# ProviderOperation 仍被引用，避免误删导入（reconcile/管理查询使用）
_ = ProviderOperation
