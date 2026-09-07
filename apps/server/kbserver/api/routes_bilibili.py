"""B 站登录态托管（docs/04 §5，2026-09-07 实测决策：按用户托管最小凭据 SESSDATA）。

- 复用模型 Key 的凭据架构：ProviderProfile(kind=bilibili_session) + Credential
  信封加密（AAD 绑定 user/profile/version），明文只进不出。
- 最小凭据：只保存 SESSDATA 值（实测仅 SESSDATA 即可取得 AI 字幕轨，
  整串 Cookie 反而拿不到可下载 URL）。允许粘贴 "SESSDATA=…" 或完整 Cookie，
  服务端只提取 SESSDATA 值，其余字段丢弃、不落任何存储。
- 更新/新增后，needs_input 条目自动重新提取（此前因无登录态降级的条目自动续跑）。
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..api.deps import require_scope
from ..domain import pipeline
from ..domain.errors import ApiError
from ..models import Credential, Item, ProviderProfile, new_id, utcnow
from ..security import credentials as cred_crypto

router = APIRouter(tags=["bilibili-session"])

SESSION_KIND = "bilibili_session"
SESSION_ADAPTER = "bilibili-web"
SESSION_ENDPOINT = "https://api.bilibili.com"
SESSION_MODEL = "sessdata"


def _extract_sessdata(raw: str) -> str:
    """接受裸 SESSDATA 值、`SESSDATA=…` 或完整 Cookie 串，只提取 SESSDATA 值。"""
    value = (raw or "").strip()
    if not value:
        raise ApiError("SCHEMA_INVALID", "SESSDATA 不能为空")
    if "=" in value:
        for pair in value.split(";"):
            pair = pair.strip()
            if pair.lower().startswith("sessdata="):
                value = pair.split("=", 1)[1].strip()
                break
    value = value.strip().strip('"')
    if not value or "=" in value or ";" in value or " " in value:
        raise ApiError("SCHEMA_INVALID", "未能从输入中提取出合法的 SESSDATA 值；请只提交 SESSDATA 的值")
    if len(value) < 20 or len(value) > 512:
        raise ApiError("SCHEMA_INVALID", "SESSDATA 长度异常（应为几十到一百多字符）")
    if not re.match(r"^[A-Za-z0-9%,*_\-./+]+$", value):
        raise ApiError("SCHEMA_INVALID", "SESSDATA 含非法字符")
    return value


def _session_profile(db: Session, user_id: str) -> ProviderProfile | None:
    return (
        db.query(ProviderProfile)
        .filter(ProviderProfile.user_id == user_id, ProviderProfile.kind == SESSION_KIND)
        .order_by(ProviderProfile.created_at)
        .first()
    )


def _active_credential(db: Session, profile: ProviderProfile) -> Credential | None:
    return (
        db.query(Credential)
        .filter(Credential.profile_id == profile.id, Credential.revoked_at.is_(None))
        .order_by(Credential.created_at.desc())
        .first()
    )


def _out(db: Session, profile: ProviderProfile | None) -> dict:
    if profile is None:
        return {
            "configured": False,
            "credential_version": None,
            "updated_at": None,
            "note": "未托管 B 站登录态：需要登录才能取得字幕的视频将进入补充材料。",
        }
    cred = _active_credential(db, profile)
    return {
        "configured": cred is not None,
        "credential_version": cred.version if cred else None,
        "updated_at": cred.created_at.isoformat() if cred else None,
        "note": "只保存 SESSDATA 值并加密存储；任何接口不返回明文。失效时更新即可。",
    }


def _requeue_needs_input(db: Session, user_id: str) -> int:
    """登录态变化：此前因缺登录态降级的条目重新提取（A13 语义由新来源版本保证）。"""
    items = (
        db.query(Item)
        .filter(Item.user_id == user_id, Item.pipeline_state == "needs_input", Item.deleted_at.is_(None))
        .all()
    )
    for it in items:
        pipeline.enqueue_stage(
            db, user_id=user_id, item_id=it.id, source_revision=it.source_revision,
            stage="extract", reset_attempt=True,
        )
    return len(items)


class SessionSecret(BaseModel):
    model_config = ConfigDict(extra="forbid")
    secret: str = Field(min_length=16, max_length=8192)


@router.get("/v1/bilibili-session")
def get_session(principal=Depends(require_scope("profiles:manage")), db: Session = Depends(get_db)):
    user, _device, _token = principal
    return _out(db, _session_profile(db, user.id))


@router.put("/v1/bilibili-session")
def put_session(body: SessionSecret, principal=Depends(require_scope("profiles:manage")), db: Session = Depends(get_db)):
    user, _device, _token = principal
    sessdata = _extract_sessdata(body.secret)

    profile = _session_profile(db, user.id)
    if profile is None:
        profile = ProviderProfile(
            user_id=user.id, kind=SESSION_KIND, adapter=SESSION_ADAPTER,
            endpoint=SESSION_ENDPOINT, model=SESSION_MODEL,
            capabilities_json={}, prices_json={}, version=1,
        )
        db.add(profile)
        db.flush()

    current = _active_credential(db, profile)
    next_version = (current.version if current else 0) + 1
    if current:
        current.revoked_at = utcnow()
    encrypted = cred_crypto.encrypt_secret(
        sessdata, get_settings().load_master_key(),
        user_id=user.id, profile_id=profile.id, credential_version=next_version,
    )
    db.add(Credential(user_id=user.id, profile_id=profile.id, version=next_version,
                      master_key_version=get_settings().master_key_version, **encrypted))

    requeued = _requeue_needs_input(db, user.id)
    db.commit()
    db.refresh(profile)
    out = _out(db, profile)
    out["requeued_items"] = requeued
    pipeline.emit_event(db, user.id, item_id=None, bundle_revision=None,
                        event_type="bilibili_session_updated",
                        payload={"credential_version": next_version, "requeued": requeued})
    db.commit()
    return out


@router.delete("/v1/bilibili-session")
def delete_session(principal=Depends(require_scope("profiles:manage")), db: Session = Depends(get_db)):
    user, _device, _token = principal
    profile = _session_profile(db, user.id)
    if profile is None:
        return {"revoked": True, "note": "本来就没有托管登录态。"}
    now = utcnow()
    revoked = 0
    for cred in db.query(Credential).filter(
        Credential.profile_id == profile.id, Credential.revoked_at.is_(None)
    ).all():
        cred.revoked_at = now
        revoked += 1
    db.commit()
    return {"revoked": True, "revoked_versions": revoked,
            "note": "后续提取回到匿名路径；需要登录的视频将进入补充材料。"}
