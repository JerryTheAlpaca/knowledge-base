"""增量事件、Bundle 清单/文件下载与投递回执（docs/02 §6.3、§10.1、§10.3、§13.1）。"""
from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..domain.errors import ApiError
from ..api.deps import require_scope
from ..models import Event, Item, StoredFile, utcnow
from ..repositories import core as repo
from ..storage.objects import ObjectStore

router = APIRouter(prefix="/v1", tags=["sync"])


# ---- 增量事件 ----

@router.get("/events")
def list_events(
    after: int = 0,
    limit: int = 100,
    principal=Depends(require_scope("items:read")),
    db: Session = Depends(get_db),
) -> dict:
    user, _device, _token = principal
    limit = max(1, min(limit, 500))
    rows = db.scalars(
        select(Event)
        .where(Event.user_id == user.id, Event.seq > after)
        .order_by(Event.seq)
        .limit(limit + 1)
    ).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "events": [
            {
                "seq": e.seq,
                "event_type": e.event_type,
                "item_id": e.item_id,
                "bundle_revision": e.bundle_revision,
                "payload": e.payload_json,
                "created_at": e.created_at.isoformat(),
            }
            for e in rows
        ],
        "next_cursor": rows[-1].seq if rows else after,
        "has_more": has_more,
    }


# ---- Bundle 清单与文件 ----

def _require_item_any_state(db: Session, user_id: str, item_id: str) -> Item:
    item = repo.get_item(db, user_id, item_id)
    if item is None:
        raise ApiError("NOT_FOUND", "条目不存在", status_code=404)
    if item.deleted_at is not None:
        # tombstone：禁止后续文件访问（docs/02 §14.3）
        raise ApiError("GONE", "条目已删除", status_code=410)
    return item


@router.get("/items/{item_id}/bundles/{revision}/manifest")
def get_manifest(
    item_id: str,
    revision: int,
    principal=Depends(require_scope("items:read")),
    db: Session = Depends(get_db),
) -> Response:
    user, _device, _token = principal
    item = _require_item_any_state(db, user.id, item_id)
    bundle = repo.get_bundle(db, user.id, item_id, revision)
    if bundle is None:
        raise ApiError("NOT_FOUND", "Bundle 版本不存在", status_code=404)
    store = ObjectStore()
    data = store.read_object(bundle.manifest_key)
    # 响应摘要校验：X-Manifest-SHA256（docs/02 §6.3）
    return Response(
        content=data,
        media_type="application/json",
        headers={"X-Manifest-SHA256": bundle.manifest_sha256, "ETag": f'"{bundle.manifest_sha256}"'},
    )


@router.get("/items/{item_id}/bundles/{revision}/files/{file_id}")
def get_file(
    item_id: str,
    revision: int,
    file_id: str,
    principal=Depends(require_scope("items:read")),
    db: Session = Depends(get_db),
) -> Response:
    user, _device, _token = principal
    item = _require_item_any_state(db, user.id, item_id)
    bundle = repo.get_bundle(db, user.id, item_id, revision)
    if bundle is None:
        raise ApiError("NOT_FOUND", "Bundle 版本不存在", status_code=404)
    # 对象权限与版本联合校验：文件必须属于该条目
    f = repo.get_file(db, user.id, file_id, item_id=item_id)
    if f is None:
        raise ApiError("NOT_FOUND", "文件不存在", status_code=404)
    store = ObjectStore()
    if not store.object_exists(f.storage_key):
        raise ApiError("NOT_FOUND", "文件对象缺失", status_code=404)
    data = store.read_object(f.storage_key)
    if hashlib.sha256(data).hexdigest() != f.sha256:
        raise ApiError("NOT_FOUND", "文件校验失败", status_code=500)
    return Response(content=data, media_type=f.mime, headers={"ETag": f'"{f.sha256}"'})


# ---- 回执 ----

class ReceiptInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: str
    bundle_revision: int
    manifest_sha256: str
    local_commit_id: str


class ReceiptResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: str
    bundle_revision: int
    manifest_sha256: str
    device_id: str
    consumer_epoch: int
    local_commit_id: str
    status: str


@router.post("/receipts", response_model=ReceiptResult)
def post_receipt(
    body: ReceiptInput,
    principal=Depends(require_scope("receipts:write")),
    db: Session = Depends(get_db),
) -> ReceiptResult:
    user, device, _token = principal
    item = _require_item_any_state(db, user.id, body.item_id)
    bundle = repo.get_bundle(db, user.id, body.item_id, body.bundle_revision)
    if bundle is None:
        raise ApiError("NOT_FOUND", "Bundle 版本不存在", status_code=404)

    epoch = repo.active_consumer_epoch(db, user.id)
    if epoch and device.consumer_epoch != epoch:
        raise ApiError("REVISION_CONFLICT", "设备已不是主要写入设备（consumer_epoch 过期）", status_code=409)

    if bundle.manifest_sha256 != body.manifest_sha256:
        raise ApiError("REVISION_CONFLICT", "清单摘要与本版本不一致", status_code=409)

    existing = repo.get_receipt(db, user.id, body.item_id, body.bundle_revision, device.id)
    if existing is None:
        from ..models import Receipt
        db.add(Receipt(
            user_id=user.id, item_id=body.item_id, bundle_revision=body.bundle_revision,
            device_id=device.id, manifest_sha256=body.manifest_sha256,
            local_commit_id=body.local_commit_id[:120],
        ))
        pipeline_emit_received(db, user.id, body.item_id, body.bundle_revision)
        db.commit()
    # 重复同版本回执幂等返回成功（docs/02 §10.3）
    return ReceiptResult(
        item_id=body.item_id,
        bundle_revision=body.bundle_revision,
        manifest_sha256=bundle.manifest_sha256,
        device_id=device.id,
        consumer_epoch=device.consumer_epoch,
        local_commit_id=body.local_commit_id,
        status="stored",
    )


def pipeline_emit_received(db: Session, user_id: str, item_id: str, revision: int) -> None:
    from ..domain.pipeline import emit_event
    emit_event(db, user_id, item_id=item_id, bundle_revision=revision, event_type="item_acked")
