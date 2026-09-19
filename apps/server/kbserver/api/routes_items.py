"""收件箱与条目接口（docs/02 §10.1；docs/08 §8.4）。

- GET /v1/items：状态过滤、稳定分页，不返回凭据。
- GET /v1/items/{id}：来源、状态、缺失材料、阅读所需的元数据与到期时间；
  已删除返回 410。
- GET /v1/items/{id}/reading：原始资料与云端提炼的结构化阅读视图；
  复用 Bundle 内的 normalized.md / analysis.json，不新生成 AI 结果。
- POST /v1/items/{id}/supplements：补充文字/截图/字幕，expected_source_revision 冲突 409，新增不可变来源版本。
- POST /v1/items/{id}/source-text：编辑原文，一行一块；编辑后文本成为新不可变来源版本，
  旧版本保留，按用户「AI 自动加工」开关决定是否重新提炼。
- DELETE /v1/items/{id}：标记 tombstone，取消后续发布。
- POST /v1/items/{id}/reprocess：基于已有材料重新排队，不默认重新抓站点。
- POST /v1/items/{id}/source-download：登记用户在网页下载了当前版本原文。
- POST /v1/items/{id}/refetch：显式重新提取来源；限频，保留旧版本，内容无变化不新增版本。
"""
from __future__ import annotations

import json
import re
import time

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..db import get_db
from ..domain import pipeline
from ..domain.errors import ApiError
from ..api.deps import require_scope
from ..api.rate_limit import SlidingWindowLimiter
from ..domain import workflow_view
from ..extractors import paragraphs as parafmt
from ..extractors import subtitles as subfmt
from ..models import AsrRun, AudioAsset, BundleRevision, Capture, Item, SourceRevision, Job, StoredFile, new_id, utcnow
from ..repositories import core as repo
from ..storage.objects import ObjectStore
from ..workers.publish import auto_enrich_enabled

router = APIRouter(prefix="/v1/items", tags=["items"])


class ItemOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: str
    pipeline_state: str
    state_detail: str
    source_revision: int
    bundle_revision: int
    platform: str
    media_kind: str
    source_type: str
    source_label: str
    icon_key: str
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
    # 音频原件（上传录音）保留状态与下载入口；远程临时音频为 False
    audio_original_retained: bool
    audio_original_download: str | None
    # 当前来源版本已归档的正文图片数（0 = 未提取图片，可点「提取图片」重新提取）
    images_archived: int
    # 面向 Web 的四阶段状态视图（docs/17 §10.2）：前端只渲染它，
    # 不再解释 pipeline_state / state_detail（两字段仅为旧客户端兼容保留）
    workflow: dict | None = None


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


class SourceTextInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_source_revision: int
    text: str


class ReprocessInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = None


class RefetchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # 网页/公众号：本次重新提取时下载正文图片（默认维持现状不提取）
    include_images: bool = False


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


def _item_out(item: Item, source: SourceRevision, db: Session | None = None,
              analysis_bundle: BundleRevision | None = None) -> ItemOut:
    from ..domain.source_labels import resolve_platform, source_fields

    meta = source.metadata_json
    if analysis_bundle is None and db is not None:
        analysis_bundle = _analysis_bundle(db, item)
    expires_at = analysis_bundle.expires_at if analysis_bundle else None
    now = utcnow()
    # 旧客户端把采集渠道（web_inbox）写进了 platform：展示时按 URL 回退到真实来源，
    # 不把渠道当平台名显示（docs/13 §5.2）
    platform = resolve_platform(meta.get("platform"), meta.get("original_url"))
    fields = source_fields(platform, meta.get("media_kind"))
    audio_retained = bool(meta.get("original_media_retained")) and fields["source_type"] == "audio_upload"
    return ItemOut(
        item_id=item.id,
        pipeline_state=item.pipeline_state,
        state_detail=item.state_detail,
        source_revision=item.source_revision,
        bundle_revision=item.bundle_revision,
        platform=fields["platform"],
        media_kind=fields["media_kind"],
        source_type=fields["source_type"],
        source_label=fields["source_label"],
        icon_key=fields["icon_key"],
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
        captured_at=meta.get("captured_at") or item.created_at.isoformat(),
        created_at=item.created_at.isoformat(),
        expires_at=expires_at.isoformat() if expires_at else None,
        expired=bool(expires_at and expires_at <= now),
        analysis_source_revision=analysis_bundle.source_revision if analysis_bundle else None,
        analysis_bundle_revision=analysis_bundle.revision if analysis_bundle else None,
        analysis_created_at=analysis_bundle.created_at.isoformat() if analysis_bundle else None,
        audio_original_retained=audio_retained,
        audio_original_download=(f"/v1/items/{item.id}/audio-original" if audio_retained else None),
        images_archived=int(meta.get("images_archived") or 0),
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


def _with_workflow(out: ItemOut, item: Item, wf_map: dict) -> ItemOut:
    wf = wf_map.get(item.id)
    if wf is None:
        return out
    return out.model_copy(update={"workflow": wf})


def _analysis_bundles_map(db: Session, user_id: str, ids: list[str]) -> dict[str, BundleRevision]:
    """批量取每条目最新提炼 Bundle（审查 C-07：避免逐行查询）。"""
    bundle_map: dict[str, BundleRevision] = {}
    if ids:
        for b in db.query(BundleRevision).filter(
            BundleRevision.user_id == user_id,
            BundleRevision.item_id.in_(ids),
            BundleRevision.processing_state.in_(["ready", "failed"]),
        ).order_by(BundleRevision.revision.asc()).all():
            bundle_map[b.item_id] = b  # 升序遍历后覆盖：留下最大 revision
    return bundle_map


@router.get("", response_model=ItemList)
def list_items(
    state: str | None = None,
    view: str | None = None,
    search: str | None = None,
    source_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
    principal=Depends(require_scope("items:read")),
    db: Session = Depends(get_db),
) -> ItemList:
    """条目列表（docs/17 §10.5）：

    - 无 view/search：稳定分页，行为与旧客户端一致。
    - view=attention|working|published：首页三分组；SQL 先按候选状态收敛，
      Python 按推导后的 overall_state 精筛（发布只认当前 Bundle 回执）。
    - search：服务端全收件箱搜索（标题/URL/来源标签/用户备注），不只搜已加载页。
    """
    user = principal.user
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    filtered_mode = bool(view or search or source_type)

    if not filtered_mode:
        items, total = repo.list_items(db, user.id, state=state, limit=limit, offset=offset)
    else:
        # 过滤模式：拉取候选集（个人收件箱规模一次 200 条足够），Python 精筛后手动分页
        candidate_states = workflow_view.VIEW_CANDIDATE_STATES.get(view or "")
        if candidate_states is not None:
            # view 优先于旧 state 参数：候选集合覆盖它
            items, _ = _list_items_by_states(db, user.id, candidate_states, limit=200)
        else:
            items, _ = repo.list_items(db, user.id, state=state, limit=200, offset=0)

    ids = [it.id for it in items]
    src_map: dict[tuple[str, int], SourceRevision] = {}
    if ids:
        revs = sorted({it.source_revision for it in items})
        for r in db.query(SourceRevision).filter(
            SourceRevision.item_id.in_(ids), SourceRevision.revision.in_(revs)
        ).all():
            src_map[(r.item_id, r.revision)] = r
    bundle_map = _analysis_bundles_map(db, user.id, ids)

    wf_inputs = workflow_view.collect_workflow_inputs(db, user.id, items)
    wf_map = workflow_view.build_workflow_map(db, user.id, items, wf_inputs)

    outs = []
    for it in items:
        src = src_map.get((it.id, it.source_revision))
        if src is None:
            continue
        out = _item_out(it, src, db, analysis_bundle=bundle_map.get(it.id))
        if filtered_mode:
            overall = (wf_map.get(it.id) or {}).get("overall_state")
            if view and overall not in workflow_view.VIEW_OVERALL.get(view, set()):
                continue
            if search:
                meta = src.metadata_json or {}
                hay = " ".join(filter(None, [
                    meta.get("title"), meta.get("original_url"),
                    meta.get("source_label"), meta.get("user_note"),
                ])).lower()
                if search.strip().lower() not in hay:
                    continue
            if source_type:
                from ..domain.source_labels import resolve_platform, source_fields
                meta = src.metadata_json or {}
                fields = source_fields(resolve_platform(meta.get("platform"), meta.get("original_url")),
                                       meta.get("media_kind"))
                if fields["source_type"] != source_type:
                    continue
        outs.append(_with_workflow(out, it, wf_map))

    if filtered_mode:
        total = len(outs)
        outs = outs[offset:offset + limit]
    return ItemList(items=outs, total=total, limit=limit, offset=offset)


def _list_items_by_states(db: Session, user_id: str, states: tuple[str, ...],
                          limit: int = 200) -> tuple[list[Item], int]:
    """按多个 pipeline_state 取候选（view 过滤第一步）；时间倒序。"""
    from sqlalchemy import func, select

    q = select(Item).where(Item.user_id == user_id, Item.deleted_at.is_(None),
                           Item.pipeline_state.in_(states))
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(Item.created_at.desc(), Item.id).limit(limit)).all()
    return list(rows), int(total)


@router.get("/{item_id}", response_model=ItemOut)
def get_item(item_id: str, principal=Depends(require_scope("items:read")), db: Session = Depends(get_db)) -> ItemOut:
    user = principal.user
    item = _require_item(db, user.id, item_id)
    out = _item_out(item, _latest_source(db, item), db)
    wf_map = workflow_view.build_workflow_map(db, user.id, [item])
    return _with_workflow(out, item, wf_map)


# ---- 阅读视图：原始资料与云端提炼（docs/08 §8.4） ----

class SourceMaterialOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_revision: int
    bundle_revision: int
    normalized_md: str | None
    # 阅读层正文：segments 合并后的自然段落（块 ID ^p0001）；没有则回退 normalized
    readable_md: str | None
    # segment_id → paragraph_id：证据引用定位到所属段落
    segment_paragraph: dict[str, str]
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


def _segment_paragraph_map(db: Session, item: Item, revision: int) -> dict[str, str]:
    """segment_id → paragraph_id：证据引用跳转到段落正文用（阅读层）。"""
    raw = _read_bundle_text(db, item, revision, "segments.json")
    if not raw:
        return {}
    try:
        doc = json.loads(raw)
    except ValueError:
        return {}
    mapping = {
        s["segment_id"]: s["paragraph_id"]
        for s in (doc.get("segments") or [])
        if isinstance(s, dict) and s.get("segment_id") and s.get("paragraph_id")
    }
    if mapping:
        return mapping
    # 旧版本没有逐片段标记：用段落清单展开
    return {
        sid: p["paragraph_id"]
        for p in (doc.get("paragraphs") or [])
        if isinstance(p, dict) and p.get("paragraph_id")
        for sid in (p.get("segment_ids") or [])
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
    readable = _read_bundle_text(db, item, revision, "readable.md")
    entry = next((f for f in manifest.get("files", []) if f.get("relative_path") == "normalized.md"), None)
    too_large = bool(entry and entry.get("bytes", 0) > MAX_INLINE_READ_BYTES)
    return SourceMaterialOut(
        source_revision=manifest.get("source_revision", item.source_revision),
        bundle_revision=revision,
        normalized_md=normalized,
        readable_md=readable,
        segment_paragraph=_segment_paragraph_map(db, item, revision),
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
        if item.pipeline_state == "extracted":
            state, detail = "pending", "AI 自动加工已关闭；可在设置中开启，或手动重新加工。"
        elif item.pipeline_state in {"waiting_key", "needs_input"}:
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
        # 用户语言契约（docs/17 §2.4）：不出现 r2/r3 一类修订号
        stale_note = "原始内容已经更新，这份整理基于更新前的内容；点击证据打开对应版本的原文。"
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
    wf_map = workflow_view.build_workflow_map(db, user.id, [item])
    out = _with_workflow(out, item, wf_map)
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


# ---- 处理记录（docs/17 §10.3）：用户主动打开时才请求的按需诊断 ----

@router.get("/{item_id}/diagnostics")
def get_diagnostics(item_id: str, principal=Depends(require_scope("items:read")),
                    db: Session = Depends(get_db)) -> dict:
    """白话摘要 → 用途解释 → 折叠的脱敏技术信息；不进入主页面与 Toast。"""
    user = principal.user
    item = _require_item(db, user.id, item_id)
    return workflow_view.build_diagnostics(db, user.id, item, _latest_source(db, item))


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


# ---- 原文编辑（用户编辑后的文本成为新的不可变来源版本） ----

# 与采集 text/share_text 上限一致（pipeline.validate_capture_payload）
_EDIT_MAX_BYTES = 1024 * 1024

# 行尾块 ID（^s0001/^p0001）：粘贴带标记的原文回来时容忍并剥离
_BLOCK_ID_TAIL = re.compile(r"\s+\^[sp]\d{4}\s*$")


def _parse_edited_blocks(text: str) -> list[tuple[str, str]]:
    """编辑文本 → (正文, kind) 块列表：一行一块；`#` 开头视为标题。

    编辑是人对正文的最终裁决：不套段落合并启发式，一行就是一段，
    保存后证据粒度与阅读粒度一致（每个片段对应一个段落）。
    """
    blocks: list[tuple[str, str]] = []
    for line in text.splitlines():
        line = _BLOCK_ID_TAIL.sub("", line).strip()
        if not line:
            continue
        if line.startswith("#"):
            line = line.lstrip("#").strip()
            if line:
                blocks.append((line, "heading"))
        else:
            blocks.append((line, "paragraph"))
    return blocks


@router.post("/{item_id}/source-text", response_model=ItemOut, status_code=202)
def edit_source_text(item_id: str, body: SourceTextInput,
                     principal=Depends(require_scope("items:edit")),
                     db: Session = Depends(get_db)) -> ItemOut:
    """编辑原文：以编辑后的文本新增不可变来源版本并重新发布 Bundle。

    - 一行一块（标题行以 # 开头）；旧版本与其提炼结果保留，不覆盖历史；
    - 编辑会清掉 missing_materials 中的 main_content（用户对正文负责）；
    - 按用户「AI 自动加工」开关决定是否重新提炼；
    - 内容与当前版本无变化时不新增版本（幂等）。
    """
    user = principal.user
    item = _require_item(db, user.id, item_id)
    source = _latest_source(db, item)
    if body.expected_source_revision != item.source_revision:
        raise ApiError("REVISION_CONFLICT", "来源版本已变化，请刷新后重试", status_code=409)
    if not item.bundle_revision:
        raise ApiError("SCHEMA_INVALID", "该条目还没有可编辑的原文", status_code=422)
    if len(body.text.encode("utf-8")) > _EDIT_MAX_BYTES:
        raise ApiError("PAYLOAD_TOO_LARGE", "编辑后的正文超过 1MiB 上限", status_code=413)

    blocks = _parse_edited_blocks(body.text)
    if not blocks:
        raise ApiError("SCHEMA_INVALID", "编辑后的正文为空", status_code=422)

    # 内容无变化则不新增版本：与当前原文按同样的块解析规则比较
    current_text = (_read_bundle_text(db, item, item.bundle_revision, "readable.md")
                    or _read_bundle_text(db, item, item.bundle_revision, "normalized.md")
                    or "")
    if blocks == _parse_edited_blocks(current_text):
        return _item_out(item, source, db)

    store = ObjectStore()
    segments = []
    paragraph_list = []
    for i, (t, kind) in enumerate(blocks, start=1):
        sid, pid = f"s{i:04d}", f"p{i:04d}"
        segments.append({
            "segment_id": sid, "text": t, "artifact_file_id": None,
            "locator": {"type": "paragraph", "index": i},
            "origin": "user_edit", "confidence": None,
            "kind": kind, "start_ms": None, "end_ms": None,
        })
        paragraph_list.append({
            "paragraph_id": pid, "segment_ids": [sid], "kind": kind,
            "start_ms": None, "end_ms": None, "char_count": len(t), "text": t,
        })
    normalized_md = subfmt.segments_to_normalized_md(segments)
    readable_md = parafmt.paragraphs_to_readable_md(paragraph_list)

    new_revision = item.source_revision + 1
    meta2 = dict(source.metadata_json)
    meta2["edited_by_user"] = True
    meta2["edited_at"] = utcnow().isoformat()
    meta2["missing_materials"] = [
        m for m in meta2.get("missing_materials", []) if m != "main_content"
    ]
    meta_updates = {"edited_by_user": True, "edited_at": meta2["edited_at"]}
    source2 = SourceRevision(
        item_id=item.id, user_id=user.id, revision=new_revision,
        content_hash=pipeline.sha256_hex(pipeline.canonical_json(
            {"segments": segments, "meta_updates": meta_updates})),
        metadata_json=meta2, artifacts_json={},
    )
    db.add(source2)
    db.flush()
    item.source_revision = new_revision

    # 先登记编辑版三件套，再按路径取最新登记组装 Bundle 文件清单
    # （否则旧登记与同路径新登记并存，读取方命中清单里靠前的旧文件）
    files_new = [
        pipeline.register_file(
            db, store, user_id=user.id, item_id=item.id,
            data=normalized_md.encode("utf-8"), relative_path="normalized.md",
            role="source_material", mime="text/markdown",
        ),
        pipeline.register_file(
            db, store, user_id=user.id, item_id=item.id,
            data=readable_md.encode("utf-8"), relative_path="readable.md",
            role="source_material", mime="text/markdown",
        ),
        pipeline.register_file(
            db, store, user_id=user.id, item_id=item.id,
            data=pipeline.canonical_json({
                "source_revision": new_revision,
                "segments": [dict(s, paragraph_id=p["paragraph_id"])
                             for s, p in zip(segments, paragraph_list)],
                "paragraphs": paragraph_list,
            }),
            relative_path="segments.json", role="source_material", mime="application/json",
        ),
    ]
    db.flush()
    latest = {f.relative_path: f for f in pipeline.latest_files_per_path(list(
        db.query(StoredFile).filter(StoredFile.item_id == item.id, StoredFile.user_id == user.id)
    ))}
    for f in files_new:
        latest[f.relative_path] = f
    files = list(latest.values())

    auto_enrich = auto_enrich_enabled(db, user.id)
    pipeline.publish_bundle(
        db, store, item=item, source=source2, files=files,
        processing_state="original_only",
        pipeline_state="enriching" if auto_enrich else "extracted",
        warnings=["用户编辑了原文；本次正文以编辑版本为准。"],
    )
    if auto_enrich:
        pipeline.enqueue_stage(db, user_id=user.id, item_id=item.id,
                               source_revision=new_revision, stage="enrich", reset_attempt=True)
    db.commit()
    db.refresh(item)
    return _item_out(item, source2, db)


@router.post("/{item_id}/source-download", response_model=ItemOut)
def mark_source_download(item_id: str,
                         principal=Depends(require_scope("items:edit")),
                         db: Session = Depends(get_db)) -> ItemOut:
    """登记「用户在网页下载了原文」：与插件回执一样进入终态（docs/17 §5.2）。

    记下当时的 Bundle 版本：原文随后更新时旧的下载不算新版本已完成。
    """
    user = principal.user
    item = _require_item(db, user.id, item_id)
    if not item.bundle_revision:
        raise ApiError("SCHEMA_INVALID", "该条目还没有可下载的原文", status_code=422)
    if item.original_download_bundle != item.bundle_revision:
        item.original_download_bundle = item.bundle_revision
        db.commit()
        db.refresh(item)
    return _item_out(item, _latest_source(db, item), db)


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
# check 在前、record 在 commit 之后：enqueue 失败不占用限频窗口（审查 C-25）
_REFETCH_WINDOW_SECONDS = 600
_refetch_limiter = SlidingWindowLimiter(1, _REFETCH_WINDOW_SECONDS)


@router.post("/{item_id}/refetch", response_model=ItemOut, status_code=202)
def refetch(item_id: str, body: RefetchInput | None = None,
            principal=Depends(require_scope("items:edit")), db: Session = Depends(get_db)) -> ItemOut:
    """显式重新提取来源（重新抓站点）：限频；旧来源版本保留，由 worker 比较内容变化。

    include_images=True 时把图片开关写进采集 payload（只开不关）：本次及之后的
    网页提取都会带上正文图片，直到来源版本自然更替。
    """
    user = principal.user
    item = _require_item(db, user.id, item_id)
    source = _latest_source(db, item)
    if not source.metadata_json.get("original_url"):
        raise ApiError("SCHEMA_INVALID", "该条目没有可重新提取的来源 URL", status_code=422)

    key = (user.id, item.id)
    _refetch_limiter.check(
        key, time.monotonic(), f"重新提取每 {_REFETCH_WINDOW_SECONDS // 60} 分钟限一次"
    )

    if body is not None and body.include_images and item.capture_id:
        capture = db.get(Capture, item.capture_id)
        if capture is not None:
            payload = dict(capture.input_json or {})
            payload["include_images"] = True
            capture.input_json = payload

    pipeline.enqueue_stage(
        db, user_id=user.id, item_id=item.id, source_revision=item.source_revision,
        stage="extract", reset_attempt=True,
    )
    item.pipeline_state = "queued"
    item.state_detail = ("用户请求重新提取来源（含正文图片）"
                         if body is not None and body.include_images else "用户请求重新提取来源")
    db.commit()
    # 限频额度在任务真正落盘后才记账（审查 C-25）：enqueue 失败时不占用限频窗口
    _refetch_limiter.record(key, time.monotonic())
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
        # 同步取消进行中的转写：否则 run 停留在 preparing 等活动状态，
        # 管理页 ASR 总览会一直显示「进行中」（docs/13 §6.3）
        db.query(AsrRun).filter(
            AsrRun.item_id == item.id,
            AsrRun.state.in_(["queued", "preparing", "transcribing", "paused"]),
        ).update(
            {"state": "cancelled", "pause_reason": "", "last_error": "条目已删除，任务作废",
             "updated_at": utcnow()},
            synchronize_session=False,
        )
        # 删除条目时解除音频原件引用（docs/13 §6.3）：在线对象随后由清理任务回收
        db.query(AudioAsset).filter(
            AudioAsset.user_id == user.id, AudioAsset.item_id == item.id
        ).update({"retention_state": "released"}, synchronize_session=False)
        pipeline.emit_event(db, user.id, item_id=item.id, bundle_revision=None, event_type="item_deleted")
        db.commit()
    return {"item_id": item.id, "deleted": True}
