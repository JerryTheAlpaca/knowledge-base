"""首次初始化（docs/17 §8、§10.6）。

- GET /v1/onboarding：三步引导的完成状态。completed 由服务端验证
  （默认模型凭据可用 + 存在未撤销桌面设备），不依赖前端 flag；
  平台连接是可选项，不阻塞完成。
- PATCH /v1/onboarding：只记录用户主动跳过/完成/重开引导的偏好。

已有模型和有效桌面设备的存量用户自然 completed=true，不强制进向导。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from ..db import get_db
from ..api.deps import require_scope
from ..models import Credential, Device, ProviderProfile, User, utcnow
from ..repositories import core as repo

router = APIRouter(prefix="/v1/onboarding", tags=["onboarding"])


def _model_step(db: Session, user_id: str) -> dict:
    """整理模型：存在带有效凭据的整理（digest）配置即完成（与 enrich 取配置同口径）。"""
    row = (
        db.query(ProviderProfile, Credential)
        .join(Credential, Credential.profile_id == ProviderProfile.id)
        .filter(
            ProviderProfile.user_id == user_id,
            ProviderProfile.kind == "llm",
            ProviderProfile.adapter == "openai-compatible",
            ProviderProfile.role.is_(None) | (ProviderProfile.role == "digest"),
            Credential.revoked_at.is_(None),
        )
        .order_by(Credential.created_at.desc())
        .first()
    )
    if row is None:
        return {"completed": False, "profile_id": None, "label": None}
    profile, _ = row
    return {"completed": True, "profile_id": profile.id, "label": profile.model}


def _active_desktop(db: Session, user_id: str) -> Device | None:
    """主要写入设备：consumer_epoch 最大的未撤销桌面设备。"""
    devices = db.query(Device).filter(
        Device.user_id == user_id, Device.kind == "desktop", Device.revoked_at.is_(None)
    ).all()
    return max(devices, key=lambda d: (d.consumer_epoch, d.created_at), default=None)


def _obsidian_step(db: Session, user_id: str) -> dict:
    device = _active_desktop(db, user_id)
    if device is None:
        return {"completed": False, "active_device": None}
    return {
        "completed": True,
        "active_device": {
            "device_id": device.id,
            "name": device.name,
            "last_seen_at": device.last_seen_at.isoformat() if device.last_seen_at else None,
        },
    }


def _platforms_step(db: Session, user_id: str) -> dict:
    """内容平台：当前展示 B 站；可选，不影响完成。"""
    row = (
        db.query(ProviderProfile, Credential)
        .join(Credential, Credential.profile_id == ProviderProfile.id)
        .filter(
            ProviderProfile.user_id == user_id,
            ProviderProfile.kind == "bilibili_session",
            Credential.revoked_at.is_(None),
        )
        .first()
    )
    return {"completed": bool(row), "optional": True, "bilibili_connected": bool(row)}


def _prefs(user: User) -> dict:
    ob = (user.settings_json or {}).get("onboarding")
    return ob if isinstance(ob, dict) else {}


@router.get("")
def get_onboarding(principal=Depends(require_scope("profiles:manage")),
                   db: Session = Depends(get_db)) -> dict:
    user = principal.user
    model = _model_step(db, user.id)
    obsidian = _obsidian_step(db, user.id)
    platforms = _platforms_step(db, user.id)
    prefs = _prefs(user)
    return {
        "completed": model["completed"] and obsidian["completed"],
        "dismissed": bool(prefs.get("dismissed")),
        "model": model,
        "obsidian": obsidian,
        "platforms": platforms,
    }


class OnboardingPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # dismiss=true：用户主动关闭引导（可选平台跳过或暂不设置）
    dismiss: bool | None = None
    # reopen=true：用户从设置选择「重新运行初始化」
    reopen: bool | None = None


@router.patch("")
def patch_onboarding(body: OnboardingPatch, principal=Depends(require_scope("profiles:manage")),
                     db: Session = Depends(get_db)) -> dict:
    user = principal.user
    settings_json = dict(user.settings_json or {})
    prefs = dict(settings_json.get("onboarding") or {})
    if body.dismiss is True:
        prefs["dismissed"] = True
        prefs["dismissed_at"] = utcnow().isoformat()
    if body.reopen is True:
        prefs["dismissed"] = False
        prefs["reopened_at"] = utcnow().isoformat()
    settings_json["onboarding"] = prefs
    user.settings_json = settings_json
    db.commit()
    db.refresh(user)
    return {"dismissed": bool(_prefs(user).get("dismissed"))}
