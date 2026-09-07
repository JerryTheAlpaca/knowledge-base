"""统一 Repository 层：所有查询都带 user_id（docs/02 §7.2）。

SQLite 首版无行级安全，租户隔离由本层落实；未知对象与他人对象统一 404。
"""
from __future__ import annotations

import hashlib
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    BundleRevision,
    Device,
    IdempotencyRecord,
    Item,
    Receipt,
    StoredFile,
    Upload,
    User,
    utcnow,
)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---- 用户与设备 ----

def get_user(db: Session, user_id: str) -> User | None:
    return db.get(User, user_id)


def get_device(db: Session, user_id: str, device_id: str) -> Device | None:
    return db.scalar(select(Device).where(Device.id == device_id, Device.user_id == user_id))


def list_devices(db: Session, user_id: str) -> list[Device]:
    return list(db.scalars(select(Device).where(Device.user_id == user_id).order_by(Device.created_at)))


def active_consumer_epoch(db: Session, user_id: str) -> int:
    """当前主要写入设备的 epoch；无 active 桌面设备时为 0。"""
    now = utcnow()
    epochs = db.scalars(
        select(Device.consumer_epoch).where(
            Device.user_id == user_id,
            Device.kind == "desktop",
            Device.revoked_at.is_(None),
        )
    ).all()
    return max(epochs, default=0)


# ---- 条目 ----

def get_item(db: Session, user_id: str, item_id: str) -> Item | None:
    item = db.scalar(select(Item).where(Item.id == item_id, Item.user_id == user_id))
    return item


def list_items(db: Session, user_id: str, *, state: str | None = None, limit: int = 50, offset: int = 0) -> tuple[list[Item], int]:
    q = select(Item).where(Item.user_id == user_id, Item.deleted_at.is_(None))
    if state:
        q = q.where(Item.pipeline_state == state)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(Item.created_at.desc(), Item.id).limit(limit).offset(offset)).all()
    return list(rows), int(total)


# ---- Bundle 与文件 ----

def get_bundle(db: Session, user_id: str, item_id: str, revision: int) -> BundleRevision | None:
    return db.scalar(
        select(BundleRevision).where(
            BundleRevision.user_id == user_id,
            BundleRevision.item_id == item_id,
            BundleRevision.revision == revision,
        )
    )


def list_bundles(db: Session, user_id: str, item_id: str) -> list[BundleRevision]:
    return list(
        db.scalars(
            select(BundleRevision)
            .where(BundleRevision.user_id == user_id, BundleRevision.item_id == item_id)
            .order_by(BundleRevision.revision)
        )
    )


def get_file(db: Session, user_id: str, file_id: str, item_id: str | None = None) -> StoredFile | None:
    q = select(StoredFile).where(StoredFile.user_id == user_id, StoredFile.file_id == file_id)
    if item_id:
        q = q.where(StoredFile.item_id == item_id)
    return db.scalar(q)


def get_upload(db: Session, user_id: str, upload_id: str) -> Upload | None:
    return db.scalar(select(Upload).where(Upload.id == upload_id, Upload.user_id == user_id))


# ---- 回执 ----

def get_receipt(db: Session, user_id: str, item_id: str, bundle_revision: int, device_id: str) -> Receipt | None:
    return db.scalar(
        select(Receipt).where(
            Receipt.user_id == user_id,
            Receipt.item_id == item_id,
            Receipt.bundle_revision == bundle_revision,
            Receipt.device_id == device_id,
        )
    )


# ---- 幂等 ----

def idempotency_lookup(db: Session, user_id: str, endpoint: str, key: str) -> IdempotencyRecord | None:
    key_hash = sha256_hex(key.encode("utf-8"))
    rec = db.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.user_id == user_id,
            IdempotencyRecord.endpoint == endpoint,
            IdempotencyRecord.key_hash == key_hash,
        )
    )
    if rec and rec.expires_at <= utcnow():
        return None
    return rec


def idempotency_save(
    db: Session,
    user_id: str,
    endpoint: str,
    key: str,
    request_hash: str,
    response_json: dict,
    status_code: int,
    ttl_days: int = 90,
) -> None:
    from ..models import IdempotencyRecord as R

    db.add(
        R(
            user_id=user_id,
            endpoint=endpoint,
            key_hash=sha256_hex(key.encode("utf-8")),
            request_hash=request_hash,
            response_json=response_json,
            status_code=status_code,
            expires_at=utcnow() + timedelta(days=ttl_days),
        )
    )
