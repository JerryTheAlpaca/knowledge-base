"""收件箱与条目接口（docs/02 §10.1）。

- GET /v1/items：状态过滤、稳定分页，不返回凭据。
- GET /v1/items/{id}：来源、状态、缺失材料；已删除返回 410。
- POST /v1/items/{id}/supplements：补充文字/截图/字幕，expected_source_revision 冲突 409，新增不可变来源版本。
- DELETE /v1/items/{id}：标记 tombstone，取消后续发布。
- POST /v1/items/{id}/reprocess：基于已有材料重新排队，不默认重新抓站点。
- POST /v1/items/{id}/refetch：显式重新提取来源；限频，保留旧版本，内容无变化不新增版本。
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..db import get_db
from ..domain import pipeline
from ..domain.errors import ApiError
from ..api.deps import require_scope
from ..models import Item, SourceRevision, Job, new_id, utcnow
from ..repositories import core as repo
from ..storage.objects import ObjectStore

router = APIRouter(prefix="/v1/items", tags=["items"])


class ItemOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: str
    pipeline_state: str
    state_detail: str
    source_revision: int
    bundle_revision: int
    platform: str
    original_url: str | None
    coverage: str
    missing_materials: list[str]
    captured_at: str | None
    created_at: str


class ItemList(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ItemOut]
    total: int
    limit: int
    offset: int


class SupplementInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_source_revision: int
    text: str | None = None
    note: str | None = None
    upload_ids: list[str] = Field(default_factory=list)


class ReprocessInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = None


def _item_out(item: Item, source: SourceRevision) -> ItemOut:
    meta = source.metadata_json
    return ItemOut(
        item_id=item.id,
        pipeline_state=item.pipeline_state,
        state_detail=item.state_detail,
        source_revision=item.source_revision,
        bundle_revision=item.bundle_revision,
        platform=meta.get("platform", "unknown"),
        original_url=meta.get("original_url"),
        coverage=meta.get("coverage", "metadata_only"),
        missing_materials=meta.get("missing_materials", []),
        captured_at=meta.get("captured_at"),
        created_at=item.created_at.isoformat(),
    )


def _require_item(db: Session, user_id: str, item_id: str) -> Item:
    item = repo.get_item(db, user_id, item_id)
    if item is None:
        raise ApiError("NOT_FOUND", "条目不存在", status_code=404)
    if item.deleted_at is not None:
        raise ApiError("GONE", "条目已删除", status_code=410)
    return item


def _latest_source(db: Session, item: Item) -> SourceRevision:
    source = db.query(SourceRevision).filter(
        SourceRevision.item_id == item.id,
        SourceRevision.user_id == item.user_id,
        SourceRevision.revision == item.source_revision,
    ).one_or_none()
    if source is None:
        raise ApiError("NOT_FOUND", "来源版本缺失", status_code=500)
    return source


@router.get("", response_model=ItemList)
def list_items(
    state: str | None = None,
    limit: int = 50,
    offset: int = 0,
    principal=Depends(require_scope("items:read")),
    db: Session = Depends(get_db),
) -> ItemList:
    user = principal.user
    limit = max(1, min(limit, 200))
    items, total = repo.list_items(db, user.id, state=state, limit=limit, offset=max(0, offset))
    outs = []
    for it in items:
        src = db.query(SourceRevision).filter(
            SourceRevision.item_id == it.id, SourceRevision.revision == it.source_revision
        ).one_or_none()
        if src:
            outs.append(_item_out(it, src))
    return ItemList(items=outs, total=total, limit=limit, offset=offset)


@router.get("/{item_id}", response_model=ItemOut)
def get_item(item_id: str, principal=Depends(require_scope("items:read")), db: Session = Depends(get_db)) -> ItemOut:
    user = principal.user
    item = _require_item(db, user.id, item_id)
    return _item_out(item, _latest_source(db, item))


@router.post("/{item_id}/supplements", response_model=ItemOut, status_code=202)
def supplement(
    item_id: str,
    body: SupplementInput,
    principal=Depends(require_scope("items:edit")),
    db: Session = Depends(get_db),
) -> ItemOut:
    """补充材料：新增不可变来源版本并重新排队（docs/02 §8.1 needs_input -> queued）。"""
    user = principal.user
    item = _require_item(db, user.id, item_id)
    source = _latest_source(db, item)
    if body.expected_source_revision != item.source_revision:
        raise ApiError("REVISION_CONFLICT", "来源版本已变化，请刷新后重试", status_code=409)

    uploads = {}
    for uid in body.upload_ids:
        up = repo.get_upload(db, user.id, uid)
        if up is None or up.state != "completed":
            raise ApiError("SCHEMA_INVALID", f"upload_id 不存在或未完成：{uid}")
        uploads[uid] = up

    if not body.text and not uploads:
        raise ApiError("SCHEMA_INVALID", "补充内容为空")

    store = ObjectStore()
    new_revision = item.source_revision + 1
    import copy
    meta = copy.deepcopy(source.metadata_json)
    if body.text:
        meta["supplement_text"] = body.text
    if body.note:
        meta["supplement_note"] = body.note
    meta.setdefault("missing_materials", [])
    if body.text:
        meta["missing_materials"] = [m for m in meta["missing_materials"] if m != "main_content"]
    if uploads:
        meta["missing_materials"] = [
            m for m in meta["missing_materials"]
            if m not in {"ocr_text", "subtitle_file", "transcript"}
        ]

    new_source = SourceRevision(
        item_id=item.id, user_id=user.id, revision=new_revision,
        content_hash=pipeline.sha256_hex(pipeline.canonical_json(meta)),
        metadata_json=meta,
        artifacts_json=source.artifacts_json,
    )
    db.add(new_source)
    item.source_revision = new_revision

    files = []
    if body.text:
        files.append(pipeline.register_file(
            db, store, user_id=user.id, item_id=item.id,
            data=body.text.encode("utf-8"), relative_path=f"supplements/r{new_revision}.md",
            role="source_material", mime="text/markdown",
        ))
    for uid, up in uploads.items():
        files.append(pipeline.ensure_upload_file(db, up, user_id=user.id, item_id=item.id))
    db.flush()

    pipeline.publish_bundle(
        db, store, item=item, source=new_source, files=files,
        processing_state="original_only", pipeline_state="queued",
        warnings=["用户补充了材料，等待重新处理。"],
    )
    pipeline.enqueue_stage(db, user_id=user.id, item_id=item.id, source_revision=new_revision, stage="extract")
    db.commit()
    db.refresh(item)
    return _item_out(item, new_source)


@router.post("/{item_id}/reprocess", response_model=ItemOut, status_code=202)
def reprocess(
    item_id: str,
    body: ReprocessInput,
    principal=Depends(require_scope("items:edit")),
    db: Session = Depends(get_db),
) -> ItemOut:
    """重新加工已有材料：不默认重新抓站点。"""
    user = principal.user
    item = _require_item(db, user.id, item_id)
    source = _latest_source(db, item)
    stage = "extract" if item.pipeline_state in {"needs_input", "failed"} else "enrich"
    pipeline.enqueue_stage(
        db, user_id=user.id, item_id=item.id, source_revision=item.source_revision,
        stage=stage, reset_attempt=True,
    )
    item.pipeline_state = "queued"
    item.state_detail = body.reason or "用户请求重新加工"
    db.commit()
    db.refresh(item)
    return _item_out(item, source)


# 重新提取限频：每条目 10 分钟一次（docs/02 §10.1 refetch 限频）
_REFETCH_LAST_AT: dict[tuple[str, str], float] = {}
_REFETCH_MIN_INTERVAL_SECONDS = 600


@router.post("/{item_id}/refetch", response_model=ItemOut, status_code=202)
def refetch(item_id: str, principal=Depends(require_scope("items:edit")), db: Session = Depends(get_db)) -> ItemOut:
    """显式重新提取来源（重新抓站点）：限频；旧来源版本保留，由 worker 比较内容变化。"""
    user = principal.user
    item = _require_item(db, user.id, item_id)
    source = _latest_source(db, item)
    if not source.metadata_json.get("original_url"):
        raise ApiError("SCHEMA_INVALID", "该条目没有可重新提取的来源 URL", status_code=422)

    key = (user.id, item.id)
    now = time.monotonic()
    last = _REFETCH_LAST_AT.get(key)
    if last is not None and now - last < _REFETCH_MIN_INTERVAL_SECONDS:
        raise ApiError("RATE_LIMITED", f"重新提取每 {_REFETCH_MIN_INTERVAL_SECONDS // 60} 分钟限一次", status_code=429)
    _REFETCH_LAST_AT[key] = now

    pipeline.enqueue_stage(
        db, user_id=user.id, item_id=item.id, source_revision=item.source_revision,
        stage="extract", reset_attempt=True,
    )
    item.pipeline_state = "queued"
    item.state_detail = "用户请求重新提取来源"
    db.commit()
    db.refresh(item)
    return _item_out(item, source)


@router.delete("/{item_id}", status_code=200)
def delete_item(item_id: str, principal=Depends(require_scope("items:edit")), db: Session = Depends(get_db)) -> dict:
    """先标记 tombstone 并取消后续发布；在线对象由清理任务在 24 小时内回收。"""
    user = principal.user
    item = repo.get_item(db, user.id, item_id)
    if item is None:
        raise ApiError("NOT_FOUND", "条目不存在", status_code=404)
    if item.deleted_at is None:
        item.deleted_at = utcnow()
        db.query(Job).filter(Job.item_id == item.id, Job.state.in_(["queued", "retry_wait"])).update(
            {"state": "cancelled"}, synchronize_session=False
        )
        pipeline.emit_event(db, user.id, item_id=item.id, bundle_revision=None, event_type="item_deleted")
        db.commit()
    return {"item_id": item.id, "deleted": True}
