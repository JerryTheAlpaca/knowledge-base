"""收件箱与条目接口（docs/02 §10.1；docs/08 §8.4）。

- GET /v1/items：状态过滤、稳定分页，不返回凭据。
- GET /v1/items/{id}：来源、状态、缺失材料、阅读所需的元数据与到期时间；
  已删除返回 410。
- GET /v1/items/{id}/reading：原始资料与云端提炼的结构化阅读视图；
  复用 Bundle 内的 normalized.md / analysis.json，不新生成 AI 结果。
- POST /v1/items/{id}/supplements：补充文字/截图/字幕，expected_source_revision 冲突 409，新增不可变来源版本。
- DELETE /v1/items/{id}：标记 tombstone，取消后续发布。
- POST /v1/items/{id}/reprocess：基于已有材料重新排队，不默认重新抓站点。
- POST /v1/items/{id}/refetch：显式重新提取来源；限频，保留旧版本，内容无变化不新增版本。
"""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..db import get_db
from ..domain import pipeline
from ..domain.errors import ApiError
from ..api.deps import require_scope
from ..models import BundleRevision, Item, SourceRevision, Job, new_id, utcnow
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
    title: str | None
    author: str | None
    original_url: str | None
    canonical_url: str | None
    published_at: str | None
    source_locator: dict
    coverage: str
    content_scope: str
    user_note: str | None
    missing_materials: list[str]
    captured_at: str | None
    created_at: str
    # 云端材料到期时间（docs/08 §8.4）：过期后如实提示，不从 Vault 回读
    expires_at: str | None
    expired: bool
    # 最新含提炼的 Bundle 对应来源版本；用于显式版本选择（docs/08 §8.4）
    analysis_source_revision: int | None
    analysis_bundle_revision: int | None
    analysis_created_at: str | None


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


def _analysis_bundle(db: Session, item: Item) -> BundleRevision | None:
    """最新一个带提炼产物的 Bundle（用于显式版本选择，docs/08 §8.4）。"""
    return (
        db.query(BundleRevision)
        .filter(
            BundleRevision.user_id == item.user_id,
            BundleRevision.item_id == item.id,
            BundleRevision.processing_state.in_(["ready", "failed"]),
        )
        .order_by(BundleRevision.revision.desc())
        .first()
    )


def _item_out(item: Item, source: SourceRevision, db: Session | None = None) -> ItemOut:
    meta = source.metadata_json
    analysis_bundle = _analysis_bundle(db, item) if db is not None else None
    expires_at = analysis_bundle.expires_at if analysis_bundle else None
    now = utcnow()
    return ItemOut(
        item_id=item.id,
        pipeline_state=item.pipeline_state,
        state_detail=item.state_detail,
        source_revision=item.source_revision,
        bundle_revision=item.bundle_revision,
        platform=meta.get("platform", "unknown"),
        title=meta.get("title"),
        author=meta.get("author"),
        original_url=meta.get("original_url"),
        canonical_url=meta.get("canonical_url"),
        published_at=meta.get("published_at"),
        source_locator=meta.get("source_locator") or {},
        coverage=meta.get("coverage", "metadata_only"),
        content_scope=meta.get("content_scope", "unknown"),
        user_note=meta.get("user_note"),
        missing_materials=meta.get("missing_materials", []),
        captured_at=meta.get("captured_at"),
        created_at=item.created_at.isoformat(),
        expires_at=expires_at.isoformat() if expires_at else None,
        expired=bool(expires_at and expires_at <= now),
        analysis_source_revision=analysis_bundle.source_revision if analysis_bundle else None,
        analysis_bundle_revision=analysis_bundle.revision if analysis_bundle else None,
        analysis_created_at=analysis_bundle.created_at.isoformat() if analysis_bundle else None,
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
            outs.append(_item_out(it, src, db))
    return ItemList(items=outs, total=total, limit=limit, offset=offset)


@router.get("/{item_id}", response_model=ItemOut)
def get_item(item_id: str, principal=Depends(require_scope("items:read")), db: Session = Depends(get_db)) -> ItemOut:
    user = principal.user
    item = _require_item(db, user.id, item_id)
    return _item_out(item, _latest_source(db, item), db)


# ---- 阅读视图：原始资料与云端提炼（docs/08 §8.4） ----

class SourceMaterialOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_revision: int
    bundle_revision: int
    normalized_md: str | None
    normalized_available: bool
    truncated: bool
    files: list[dict]
    # 有轨但取不到 / 部分取得等真实覆盖说明来自 manifest
    coverage: str
    missing_materials: list[str]
    warnings: list[str]
    # 原件下载入口（已鉴权文件路由）
    download_base: str


class CloudDigestOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: str  # ready|pending|failed|expired|missing
    state_detail: str
    source_revision: int | None
    bundle_revision: int | None
    created_at: str | None
    schema_version: str | None
    summary: str | None
    key_points: list[dict]
    excerpts: list[dict]
    methods: list[dict]
    insights: list[dict]
    limitations: list[str]
    workflow: dict | None
    evidence_map: dict
    # 结构化结果对应的原文片段（用于定位）；键为 segment_id
    segments: dict[str, str]
    stale_note: str | None


class ReadingOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item: ItemOut
    source_material: SourceMaterialOut | None
    cloud_digest: CloudDigestOut
    expires_at: str | None
    expired: bool
    note: str = ""


MAX_INLINE_READ_BYTES = 2 * 1024 * 1024  # 单次内联阅读上限；超出只给文件入口


def _bundle_manifest(db: Session, item: Item, revision: int) -> dict | None:
    bundle = repo.get_bundle(db, item.user_id, item.id, revision)
    if bundle is None:
        return None
    try:
        return json.loads(ObjectStore().read_object(bundle.manifest_key).decode("utf-8"))
    except Exception:
        return None


def _read_bundle_text(db: Session, item: Item, revision: int, relative_path: str) -> str | None:
    """按清单读取 Bundle 内某个已登记文件的文本；只接受清单里存在的路径。"""
    manifest = _bundle_manifest(db, item, revision)
    if manifest is None:
        return None
    entry = next((f for f in manifest.get("files", []) if f.get("relative_path") == relative_path), None)
    if entry is None:
        return None
    f = repo.get_file(db, item.user_id, entry["file_id"], item_id=item.id)
    if f is None or f.bytes > MAX_INLINE_READ_BYTES:
        return None
    store = ObjectStore()
    if not store.object_exists(f.storage_key):
        return None
    try:
        return store.read_object(f.storage_key).decode("utf-8", errors="replace")
    except Exception:
        return None


def _segments_texts(db: Session, item: Item, revision: int) -> dict[str, str]:
    raw = _read_bundle_text(db, item, revision, "segments.json")
    if not raw:
        return {}
    try:
        doc = json.loads(raw)
    except ValueError:
        return {}
    return {
        s["segment_id"]: s.get("text") or ""
        for s in (doc.get("segments") or [])
        if isinstance(s, dict) and s.get("segment_id")
    }


def _source_material(db: Session, item: Item) -> SourceMaterialOut | None:
    """当前来源版本的原文与原件；版本显式，不把截断预览标成全文（docs/08 §8.4）。"""
    revision = item.bundle_revision
    if not revision:
        return None
    manifest = _bundle_manifest(db, item, revision)
    if manifest is None:
        return None
    normalized = _read_bundle_text(db, item, revision, "normalized.md")
    entry = next((f for f in manifest.get("files", []) if f.get("relative_path") == "normalized.md"), None)
    too_large = bool(entry and entry.get("bytes", 0) > MAX_INLINE_READ_BYTES)
    return SourceMaterialOut(
        source_revision=manifest.get("source_revision", item.source_revision),
        bundle_revision=revision,
        normalized_md=normalized,
        normalized_available=normalized is not None,
        truncated=too_large,
        files=manifest.get("files", []),
        coverage=(manifest.get("source") or {}).get("coverage", "metadata_only"),
        missing_materials=manifest.get("missing_materials", []),
        warnings=manifest.get("warnings", []),
        download_base=f"/v1/items/{item.id}/bundles/{revision}/files",
    )


def _cloud_digest(db: Session, item: Item) -> CloudDigestOut:
    """云端提炼阅读：优先从结构化 analysis.json 渲染；旧 preview.md 仅作兼容输入。"""
    bundle = _analysis_bundle(db, item)
    if bundle is None:
        if item.pipeline_state in {"waiting_key", "needs_input"}:
            state, detail = "pending", "尚无云端提炼；原始资料仍可阅读。"
        elif item.pipeline_state == "failed":
            state, detail = "failed", item.state_detail or "云端提炼失败；可重新加工。"
        else:
            state, detail = "pending", "云端提炼尚未生成；原始资料仍可阅读。"
        return CloudDigestOut(
            state=state, state_detail=detail, source_revision=None, bundle_revision=None,
            created_at=None, schema_version=None, summary=None, key_points=[], excerpts=[],
            methods=[], insights=[], limitations=[], workflow=None, evidence_map={},
            segments={}, stale_note=None,
        )

    now = utcnow()
    expired = bool(bundle.expires_at and bundle.expires_at <= now)
    doc = None
    raw = _read_bundle_text(db, item, bundle.revision, "analysis.json")
    if raw:
        try:
            doc = json.loads(raw)
        except ValueError:
            doc = None
    if doc is None:
        # 旧版产物只有 preview.md：作为历史兼容输入
        legacy_md = _read_bundle_text(db, item, bundle.revision, "preview.md")
        return CloudDigestOut(
            state="expired" if expired else ("failed" if bundle.processing_state == "failed" else "pending"),
            state_detail=("云端材料已过期，本地已下载材料仍可查看。" if expired
                          else "该版本只有旧格式预览，无法结构化定位。"),
            source_revision=bundle.source_revision, bundle_revision=bundle.revision,
            created_at=bundle.created_at.isoformat(), schema_version="1.0",
            summary=(legacy_md or "")[:500] or None, key_points=[], excerpts=[],
            methods=[], insights=[], limitations=[], workflow=None, evidence_map={},
            segments={}, stale_note=None,
        )

    # 证据定位必须与提炼所用来源版本一致：不同版本不混用片段（docs/08 §8.4）
    segments = _segments_texts(db, item, bundle.revision)
    stale_note = None
    if bundle.source_revision != item.source_revision:
        stale_note = (f"现有提炼基于 r{bundle.source_revision}，"
                      f"当前来源为 r{item.source_revision}；点击证据打开 r{bundle.source_revision} 的原文。")
    state = "ready"
    detail = ""
    if expired:
        state, detail = "expired", "云端材料已过期，本地已下载材料仍可查看。"
    elif bundle.processing_state == "failed":
        state, detail = "failed", "该版本提炼未通过校验；可重新加工。"

    return CloudDigestOut(
        state=state,
        state_detail=detail,
        source_revision=bundle.source_revision,
        bundle_revision=bundle.revision,
        created_at=bundle.created_at.isoformat(),
        schema_version=doc.get("schema_version"),
        summary=doc.get("summary"),
        key_points=doc.get("key_points") or [],
        excerpts=doc.get("excerpts") or [],
        methods=doc.get("methods") or [],
        insights=doc.get("insights") or [],
        limitations=doc.get("limitations") or [],
        workflow=doc.get("workflow"),
        evidence_map=doc.get("evidence_map") or {},
        segments=segments,
        stale_note=stale_note,
    )


@router.get("/{item_id}/reading", response_model=ReadingOut)
def get_reading(item_id: str, principal=Depends(require_scope("items:read")),
                db: Session = Depends(get_db)) -> ReadingOut:
    """条目详情阅读页签数据：原始资料 + 云端提炼（docs/08 §8.4）。

    只读已登记文件；不执行原始 HTML，不渲染模型生成的脚本与命令。
    """
    user = principal.user
    item = _require_item(db, user.id, item_id)
    out = _item_out(item, _latest_source(db, item), db)
    digest = _cloud_digest(db, item)
    note = ""
    if out.expired:
        note = "云端材料已过期，本地已下载材料仍可查看。"
    return ReadingOut(
        item=out,
        source_material=_source_material(db, item),
        cloud_digest=digest,
        expires_at=out.expires_at,
        expired=out.expired,
        note=note,
    )


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
    return _item_out(item, new_source, db)


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
    return _item_out(item, source, db)


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
    return _item_out(item, source, db)


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
