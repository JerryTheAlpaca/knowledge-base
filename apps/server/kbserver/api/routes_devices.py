"""设备管理（docs/02 §10.1）：查看已配对设备、切换主要写入设备、撤销设备。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..domain.errors import ApiError
from ..api.deps import require_scope
from ..models import Device, Token, utcnow
from ..repositories import core as repo

router = APIRouter(prefix="/v1/devices", tags=["devices"])


class DeviceOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_id: str
    kind: str
    name: str
    consumer_epoch: int
    revoked: bool
    created_at: str
    last_seen_at: str | None


@router.get("", response_model=list[DeviceOut])
def list_devices(principal=Depends(require_scope("devices:manage")), db: Session = Depends(get_db)) -> list[DeviceOut]:
    user = principal.user
    devices = repo.list_devices(db, user.id)
    return [
        DeviceOut(
            device_id=d.id, kind=d.kind, name=d.name, consumer_epoch=d.consumer_epoch,
            revoked=d.revoked_at is not None, created_at=d.created_at.isoformat(),
            last_seen_at=d.last_seen_at.isoformat() if d.last_seen_at else None,
        )
        for d in devices
    ]


@router.post("/{device_id}/activate-consumer", response_model=DeviceOut)
def activate_consumer(
    device_id: str,
    principal=Depends(require_scope("devices:manage")),
    db: Session = Depends(get_db),
) -> DeviceOut:
    """切换主要写入设备：递增 consumer_epoch，旧设备停止新回执（docs/02 §9.1）。"""
    user = principal.user
    target = repo.get_device(db, user.id, device_id)
    if target is None or target.revoked_at is not None:
        raise ApiError("NOT_FOUND", "设备不存在", status_code=404)
    if target.kind != "desktop":
        raise ApiError("SCHEMA_INVALID", "只有桌面设备可以成为主要写入设备", status_code=422)
    current = repo.active_consumer_epoch(db, user.id)
    target.consumer_epoch = current + 1
    db.commit()
    db.refresh(target)
    return DeviceOut(
        device_id=target.id, kind=target.kind, name=target.name,
        consumer_epoch=target.consumer_epoch, revoked=False,
        created_at=target.created_at.isoformat(),
        last_seen_at=target.last_seen_at.isoformat() if target.last_seen_at else None,
    )


@router.delete("/{device_id}")
def revoke_device(device_id: str, principal=Depends(require_scope("devices:manage")), db: Session = Depends(get_db)) -> dict:
    """撤销设备：服务 Token 随之失效；模型 Key 不受影响。"""
    user = principal.user
    target = repo.get_device(db, user.id, device_id)
    if target is None:
        raise ApiError("NOT_FOUND", "设备不存在", status_code=404)
    if target.revoked_at is None:
        target.revoked_at = utcnow()
        db.query(Token).filter(Token.device_id == target.id, Token.revoked_at.is_(None)).update(
            {"revoked_at": utcnow()}, synchronize_session=False
        )
        db.commit()
    return {"device_id": target.id, "revoked": True}
