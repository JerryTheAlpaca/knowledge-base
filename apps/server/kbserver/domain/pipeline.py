"""领域流水线：Capture 接收、来源版本、Bundle 发布（docs/02 §6、§7.4、§8.1）。

关键顺序（§7.4）：文件先写对象存储并校验摘要，再在数据库短事务中写引用；
发布事件与更新 Item 当前版本在同一事务内。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import timedelta

from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    AudioAsset,
    BundleRevision,
    Capture,
    Event,
    Item,
    Job,
    SourceRevision,
    StoredFile,
    Upload,
    new_id,
    utcnow,
)
from ..storage.objects import ObjectStore

RECIPE_VERSION = "source-light-v1"
SCHEMA_VERSION = "1.0"


def canonical_json(data) -> bytes:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_id(prefix: str, item_id: str, path: str, sha: str) -> str:
    # 内容感知：同路径不同内容（如补充材料后重提取）得到不同 file_id，不再撞唯一约束
    return f"{prefix}-{hashlib.sha256(f'{item_id}|{path}'.encode()).hexdigest()[:8]}-{sha[:12]}"


# ---- 事件 ----

def emit_event(db: Session, user_id: str, *, item_id: str | None, bundle_revision: int | None,
               event_type: str, payload: dict | None = None) -> None:
    db.add(
        Event(
            user_id=user_id,
            item_id=item_id,
            bundle_revision=bundle_revision,
            event_type=event_type,
            payload_json=payload or {},
        )
    )


# ---- Manifest ----

def build_manifest(
    *,
    item: Item,
    source: SourceRevision,
    bundle_revision: int,
    files: list[StoredFile],
    processing_state: str,
    warnings: list[str] | None = None,
    result_file_id: str | None = None,
    processing_extra: dict | None = None,
) -> dict:
    meta = source.metadata_json
    return {
        "schema_version": SCHEMA_VERSION,
        "item_id": item.id,
        "source_revision": source.revision,
        "bundle_revision": bundle_revision,
        "created_at": utcnow().isoformat(),
        "source": {
            "platform": meta.get("platform", "unknown"),
            "media_kind": meta.get("media_kind", "text"),
            "source_type": meta.get("source_type"),
            "source_label": meta.get("source_label"),
            "icon_key": meta.get("icon_key"),
            "title": meta.get("title"),
            "author": meta.get("author"),
            "original_url": meta.get("original_url"),
            "canonical_url": meta.get("canonical_url"),
            "published_at": meta.get("published_at"),
            "captured_at": meta.get("captured_at"),
            "source_locator": meta.get("source_locator", {}),
            "coverage": meta.get("coverage", "metadata_only"),
            "content_scope": meta.get("content_scope", "unknown"),
            "original_media_retained": bool(meta.get("original_media_retained", False)),
        },
        "processing": {
            "state": processing_state,
            "recipe_version": RECIPE_VERSION,
            "result_file_id": result_file_id if result_file_id is not None else meta.get("result_file_id"),
            "source_revision": source.revision,
            # v3 附加字段（format_version/completeness/content_file_id）：由发布方给出，
            # 旧写入路径不传就保持原样（docs/24 §5）
            **(processing_extra or {}),
        },
        "files": [
            {
                "file_id": f.file_id,
                "relative_path": f.relative_path,
                "role": f.role,
                "mime": f.mime,
                "bytes": f.bytes,
                "sha256": f.sha256,
            }
            for f in files
        ],
        "missing_materials": meta.get("missing_materials", []),
        "warnings": warnings or [],
        "expires_at": (utcnow() + timedelta(days=get_settings().unacked_bundle_retention_days)).isoformat(),
    }


def register_file(db: Session, store: ObjectStore, *, user_id: str, item_id: str,
                  data: bytes, relative_path: str, role: str, mime: str,
                  sha256: str | None = None) -> StoredFile:
    """把一个文件写入对象存储并在库中登记；file_id 由内容+路径派生，重试幂等。

    按 (user_id, file_id) 唯一约束查询已有登记（file_id 不是主键，不能按主键 get）。
    """
    from sqlalchemy import select

    sha, key, size = store.put_bytes(data)
    sha = sha256 or sha
    file_id = _file_id(role, item_id, relative_path, sha)
    existing = db.scalar(
        select(StoredFile).where(StoredFile.user_id == user_id, StoredFile.file_id == file_id)
    )
    if existing is not None and existing.sha256 == sha:
        return existing
    f = StoredFile(
        file_id=file_id,
        user_id=user_id,
        item_id=item_id,
        role=role,
        relative_path=relative_path,
        mime=mime,
        bytes=size,
        sha256=sha,
        storage_key=key,
    )
    db.add(f)
    db.flush()
    return f


def upload_to_file(upload: Upload, *, user_id: str, item_id: str) -> StoredFile:
    """已上传文件引用为 Bundle 文件；复用 upload 的对象。"""
    return StoredFile(
        file_id=f"upload-{upload.id[:12]}-{item_id[:8]}",
        user_id=user_id,
        item_id=item_id,
        role="original_submission",
        relative_path=f"uploads/{upload.id}/{upload.filename or 'attachment.bin'}",
        mime=upload.mime,
        bytes=upload.bytes,
        sha256=upload.sha256,
        storage_key=upload.storage_key,
    )


def ensure_upload_file(db: Session, upload: Upload, *, user_id: str, item_id: str) -> StoredFile:
    """把上传文件登记为条目文件；同条目同上传幂等复用已有登记。"""
    from sqlalchemy import select

    existing = db.scalar(
        select(StoredFile).where(
            StoredFile.user_id == user_id,
            StoredFile.file_id == f"upload-{upload.id[:12]}-{item_id[:8]}",
        )
    )
    if existing is not None:
        return existing
    f = upload_to_file(upload, user_id=user_id, item_id=item_id)
    db.add(f)
    db.flush()
    return f


def latest_files_per_path(rows: list[StoredFile]) -> list[StoredFile]:
    """同 relative_path 多版本（内容寻址 file_id）时只保留最新登记。"""
    latest: dict[str, StoredFile] = {}
    for f in sorted(rows, key=lambda r: (r.created_at, r.id)):
        latest[f.relative_path] = f
    return list(latest.values())


def manifest_files_by_path(manifest: dict) -> dict[str, dict]:
    """清单条目按路径归并：同路径重复登记时以最后一条（最新登记）为准。

    写侧已统一按路径覆盖，但线上仍存有旧格式清单——同路径两份、旧版在前，
    命中靠前的话转写正文会被上一次提取的旧版顶掉，读侧一律按这里的最新登记。
    """
    return {f["relative_path"]: f for f in manifest.get("files", [])
            if isinstance(f, dict) and f.get("relative_path")}


def publish_bundle(
    db: Session,
    store: ObjectStore,
    *,
    item: Item,
    source: SourceRevision,
    files: list[StoredFile],
    processing_state: str,
    warnings: list[str] | None = None,
    pipeline_state: str | None = None,
    result_file_id: str | None = None,
    processing_extra: dict | None = None,
    recipe_version: str | None = None,
) -> BundleRevision:
    """发布一个不可变 Bundle：manifest 先写对象存储，再在库中登记引用并推进 Item 当前版本。"""
    item.bundle_revision = getattr(item, "bundle_revision", 0) or 0
    revision = (item.bundle_revision or 0) + 1
    manifest = build_manifest(
        item=item, source=source, bundle_revision=revision, files=files,
        processing_state=processing_state, warnings=warnings,
        result_file_id=result_file_id, processing_extra=processing_extra,
    )
    if recipe_version:
        # 历史迁移等新链路产物按自己的规则版本登记，不借用提取阶段的常量
        manifest["processing"]["recipe_version"] = recipe_version
    manifest_bytes = canonical_json(manifest)
    sha, key, _ = store.put_bytes(manifest_bytes)

    bundle = BundleRevision(
        item_id=item.id,
        user_id=item.user_id,
        revision=revision,
        source_revision=source.revision,
        manifest_key=key,
        manifest_sha256=sha,
        processing_state=processing_state,
        expires_at=utcnow() + timedelta(days=get_settings().unacked_bundle_retention_days),
    )
    db.add(bundle)

    item.bundle_revision = revision
    item.pipeline_state = pipeline_state or processing_state

    emit_event(
        db, item.user_id,
        item_id=item.id, bundle_revision=revision,
        event_type="bundle_published",
        payload={"bundle_revision": revision, "processing_state": processing_state,
                 "source_revision": source.revision},
    )
    return bundle


def enqueue_stage(db: Session, *, user_id: str, item_id: str, source_revision: int, stage: str,
                  reset_attempt: bool = False, digest_requested: bool = False) -> Job:
    """幂等入队：jobs 有 UNIQUE(user,item,revision,stage,recipe_hash)，
    重复入队（重新加工、凭据更新后重排队）复位已有行而不是插入新行。

    digest_requested=用户点名要整理（手动「开始整理」）。同一行会被自动入队和
    手动点击复用，所以只能往上置真、不清除。
    """
    recipe_hash = hashlib.sha256(RECIPE_VERSION.encode()).hexdigest()[:16]
    job = db.query(Job).filter(
        Job.user_id == user_id,
        Job.item_id == item_id,
        Job.source_revision == source_revision,
        Job.stage == stage,
        Job.recipe_hash == recipe_hash,
    ).one_or_none()
    if job is not None:
        job.digest_requested = job.digest_requested or digest_requested
        if job.state != "running":
            job.state = "queued"
            job.not_before = utcnow()
            job.lease_token = None
            job.lease_until = None
            if reset_attempt:
                job.attempt = 0
        return job
    job = Job(
        user_id=user_id, item_id=item_id, source_revision=source_revision,
        stage=stage, recipe_hash=recipe_hash, state="queued",
        digest_requested=digest_requested,
    )
    db.add(job)
    db.flush()
    return job


# ---- Capture 接收 ----

ALLOWED_INPUT_KINDS = {"url", "text", "share", "images", "audio", "conversation", "workflow", "file"}
ALLOWED_ARCHIVE_POLICIES = {"source_materials", "minimal"}
ALLOWED_PROCESSING_INTENTS = {"default", "transcribe_audio"}


def validate_capture_payload(payload: dict, uploads_index: dict[str, Upload]) -> None:
    """写请求严格校验（docs/02 §10.2）。拒绝未知字段由 Pydantic 层负责，这里校验语义。"""
    from .errors import ApiError

    if payload.get("schema_version") != "1.0":
        raise ApiError("VERSION_UNSUPPORTED", "schema_version 仅支持 1.0")
    kind = payload.get("input_kind")
    if kind not in ALLOWED_INPUT_KINDS:
        raise ApiError("SCHEMA_INVALID", f"input_kind 非法：{kind}")
    intent = payload.get("processing_intent") or "default"
    if intent not in ALLOWED_PROCESSING_INTENTS:
        raise ApiError("SCHEMA_INVALID", f"processing_intent 非法：{intent}")
    settings = get_settings()

    url = payload.get("original_url")
    text = payload.get("text") or ""
    share = payload.get("share_text") or ""
    upload_ids = payload.get("upload_ids") or []
    primary_audio = payload.get("primary_audio_upload_id")

    if url:
        if len(url) > 8192:
            raise ApiError("SCHEMA_INVALID", "original_url 超过 8192 字符")
        if not re.match(r"^https?://", url):
            raise ApiError("SCHEMA_INVALID", "original_url 必须是 HTTP(S) 链接")
    if len(text) + len(share) > 1024 * 1024:
        raise ApiError("SCHEMA_INVALID", "text/share_text 合计超过 1MiB，请使用文件上传")
    note = payload.get("user_note") or ""
    if len(note) > 10000:
        raise ApiError("SCHEMA_INVALID", "user_note 超过 10000 字符")
    if not (url or text or share or upload_ids or primary_audio):
        raise ApiError("SCHEMA_INVALID", "至少需要 URL、文字或已上传文件之一")
    if len(upload_ids) > settings.max_attachments_per_capture:
        raise ApiError("PAYLOAD_TOO_LARGE", f"每条采集最多 {settings.max_attachments_per_capture} 个附件")

    for uid in upload_ids:
        up = uploads_index.get(uid)
        if up is None:
            raise ApiError("SCHEMA_INVALID", f"upload_id 不存在或未完成：{uid}")

    if primary_audio:
        up = uploads_index.get(primary_audio)
        if up is None:
            raise ApiError("SCHEMA_INVALID",
                           f"primary_audio_upload_id 不存在或未完成：{primary_audio}")
        if up.bytes > settings.max_audio_upload_bytes:
            raise ApiError("PAYLOAD_TOO_LARGE",
                           f"音频主体上限 {settings.max_audio_upload_bytes} 字节", status_code=413)

    if payload.get("archive_policy") not in ALLOWED_ARCHIVE_POLICIES:
        raise ApiError("SCHEMA_INVALID", "archive_policy 非法")


def _capture_platform(payload: dict) -> tuple[str, str]:
    """确定来源平台与 media_kind：只按真实输入判定，不信任客户端 hint（渠道不是平台）。

    - 音频转写意图 + 音频主体上传 → audio_upload/audio；
    - 音频转写意图 + 链接（B 站 → bilibili/video，公众号/网页 → 该平台/audio）；
    - 其余按 URL 推断平台（公众号仍是 wechat_mp，只有普通网页才是 web）。

    `source_hint`/`capture_channel` 只记采集渠道，不参与平台判定（docs/13 §5.2）。
    """
    from .platforms import guess_platform
    from .source_labels import default_media_kind, normalize_platform

    intent = payload.get("processing_intent") or "default"
    url = (payload.get("original_url") or "").strip()
    guessed = normalize_platform(guess_platform(url)) if url else "unknown"
    if intent == "transcribe_audio":
        if url:
            return ("bilibili", "video") if guessed == "bilibili" else (guessed, "audio")
        if payload.get("primary_audio_upload_id") or payload.get("input_kind") == "audio":
            return "audio_upload", "audio"
    if payload.get("input_kind") == "audio":
        if payload.get("primary_audio_upload_id") or not url:
            return "audio_upload", "audio"
        return guessed, "audio"
    return guessed, default_media_kind(guessed)


def create_capture(db: Session, store: ObjectStore, *, user_id: str, payload: dict,
                   uploads: dict[str, Upload]) -> tuple[Capture, Item]:
    """接收一条采集：原始输入 + 初始来源版本 + 原始材料 Bundle + 首个任务，同一提交边界。"""
    settings = get_settings()

    primary_audio_id = payload.get("primary_audio_upload_id")
    # 音频主体按独立额度；普通附件仍按普通额度计总和（docs/13 §7.1）
    total_bytes = sum(u.bytes for uid, u in uploads.items() if uid != primary_audio_id)
    if total_bytes > settings.max_capture_total_bytes:
        from .errors import ApiError
        raise ApiError("PAYLOAD_TOO_LARGE", "附件总计超过单条采集上限")

    now = utcnow()
    capture = Capture(
        user_id=user_id,
        client_capture_id=payload["client_capture_id"],
        request_hash=hashlib.sha256(canonical_json(payload)).hexdigest(),
        input_json=payload,
        received_at=now,
    )
    db.add(capture)
    db.flush()

    item = Item(user_id=user_id, capture_id=capture.id, pipeline_state="queued")
    db.add(item)
    db.flush()

    from .source_labels import source_fields

    platform, media_kind = _capture_platform(payload)
    fields = source_fields(platform, media_kind)
    meta = {
        "platform": fields["platform"],
        "media_kind": fields["media_kind"],
        "source_type": fields["source_type"],
        "source_label": fields["source_label"],
        "icon_key": fields["icon_key"],
        "title": None,
        "author": None,
        "original_url": payload.get("original_url"),
        "canonical_url": None,
        "published_at": None,
        "captured_at": payload.get("captured_at") or now.isoformat(),
        "source_locator": {},
        "coverage": "full_text" if (payload.get("text") or payload.get("share_text")) else ("metadata_only" if payload.get("original_url") else "metadata_only"),
        "content_scope": payload.get("content_scope") or "unknown",
        "original_media_retained": primary_audio_id is not None,
        "missing_materials": _initial_missing(payload),
        "user_note": payload.get("user_note"),
        "capture_channel": payload.get("capture_channel") or payload.get("source_hint") or None,
        "processing_intent": payload.get("processing_intent") or "default",
        "result_file_id": None,
    }
    source = SourceRevision(
        item_id=item.id, user_id=user_id, revision=1,
        content_hash=hashlib.sha256(canonical_json(payload)).hexdigest(),
        metadata_json=meta, artifacts_json={},
    )
    db.add(source)
    db.flush()

    files: list[StoredFile] = []
    capture_bytes = canonical_json({"capture": payload, "received_at": now.isoformat()})
    files.append(
        register_file(
            db, store, user_id=user_id, item_id=item.id,
            data=capture_bytes, relative_path="capture.json",
            role="original_submission", mime="application/json",
        )
    )
    for uid in payload.get("upload_ids") or []:
        up = uploads.get(uid)
        if up:
            files.append(ensure_upload_file(db, up, user_id=user_id, item_id=item.id))
    # 音频原件引用：登记 AudioAsset，让原件不再被当未引用上传清理（docs/13 §6.3）
    primary_upload = uploads.get(primary_audio_id) if primary_audio_id else None
    if primary_upload is not None:
        audio_file = ensure_upload_file(db, primary_upload, user_id=user_id, item_id=item.id)
        files.append(audio_file)
        db.add(AudioAsset(
            user_id=user_id, item_id=item.id, source_revision=1,
            upload_id=primary_upload.id, stored_file_id=audio_file.file_id,
            sha256=primary_upload.sha256, bytes=primary_upload.bytes,
            filename=primary_upload.filename or "audio.bin", mime=primary_upload.mime,
            role="original_audio", retention_state="retained",
        ))
        # 原件已登记引用：不再按未引用上传 24h 过期
        primary_upload.expires_at = None

    db.flush()
    publish_bundle(
        db, store, item=item, source=source, files=files,
        processing_state="original_only", pipeline_state="queued",
        warnings=["已保存原始材料；AI 加工尚未开始。"],
    )

    # 用户主动提交音频（上传录音或选择网页音频）即为本次转写请求，
    # 不再要求打开 B 站自动转写开关（docs/13 §6.1）；部署关闭时照常保留
    # 原件并由 prepare 显示「等待启用」。
    audio_request = (payload.get("processing_intent") == "transcribe_audio"
                     and bool(primary_audio_id or payload.get("original_url")))
    if audio_request:
        from ..workers import asr as asr_stage

        asr_stage.start_asr(db, item=item, source=source,
                            model_alias=settings.asr_model, requested_by="manual")
    else:
        enqueue_stage(db, user_id=user_id, item_id=item.id, source_revision=1, stage="extract")

    emit_event(db, user_id, item_id=item.id, bundle_revision=1, event_type="capture_received",
               payload={"item_id": item.id})
    return capture, item


def _initial_missing(payload: dict) -> list[str]:
    missing: list[str] = []
    kind = payload.get("input_kind")
    if kind == "url" and not (payload.get("text") or payload.get("share_text")):
        missing.append("main_content")
    if kind == "images":
        missing.append("ocr_text")
    if kind == "audio" or payload.get("processing_intent") == "transcribe_audio":
        missing.append("transcript")
    return missing
