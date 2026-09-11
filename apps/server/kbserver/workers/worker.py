"""独立 Worker：持久化任务调度（docs/02 §7.3、§8.1；docs/11 §6 ASR 接入）。

- BEGIN IMMEDIATE 领取到期任务；随机 lease_token、120s 租约；完成只允许当前租约提交。
- 领取按 stage 过滤：普通任务（extract/enrich）优先；ASR 片段仅在整机空闲时领取。
- 启动恢复：过期 running 租约回到 queued；主循环周期恢复过期租约。
- extract：字幕文件上传或 B 站链接走字幕适配器，普通网页/公众号链接走
  正文适配器（M4）；有正文 → 生成 normalized.md + segments.json 并发布
  新 Bundle，随后入 enrich；其余只有链接或附件 → needs_input，绝不伪造正文。
  B 站确认无字幕轨且开关开启 → 自动转入本地 ASR 路径（docs/11）。
- asr_prepare / asr_transcribe：workers/asr.py 执行（空闲准入、检查点、逐段转写）。
- enrich：由 workers/enrich.py 执行（预算预留、模型调用、校验、发布成品 Bundle）。
"""
from __future__ import annotations

import json
import re
import secrets
import time
from datetime import timedelta
from urllib.parse import urlparse

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import make_engine, make_session_factory
from ..domain import pipeline
from ..domain.platforms import guess_platform
from ..extractors import bilibili as bili
from ..extractors import paragraphs as parafmt
from ..extractors import subtitles as subfmt
from ..extractors import webpages as webpage
from ..models import (
    AsrRun,
    AudioAsset,
    AudioUploadSession,
    BundleRevision,
    Capture,
    Credential,
    DeviceAuthRequest,
    Event,
    IdempotencyRecord,
    Item,
    Job,
    ProviderProfile,
    Receipt,
    SourceRevision,
    StoredFile,
    Upload,
    utcnow,
)
from ..security import credentials as cred_crypto
from ..security.safe_fetch import SafeFetchError, safe_fetch
from ..storage.objects import ObjectStore
from . import asr as asr_stage
from . import enrich as enrich_stage
from . import idle as idle_mod
from .publish import auto_enrich_enabled
from .publish import bundle_files as _bundle_files
from .publish import publish_segments_revision as _publish_segments_revision

NORMAL_STAGES = ("extract", "enrich")
ASR_STAGES = ("asr_prepare", "asr_transcribe")


def claim_job(session_factory, stages: tuple[str, ...] | None = None) -> Job | None:
    """原子领取一个到期任务；按 stage 过滤（ASR 不得抢先普通任务导致饥饿）。

    SQLite RETURNING 保证单写者下不重复领取。
    """
    now = utcnow().replace(tzinfo=None)  # 原生 SQL 参数不经过 TypeDecorator，需去掉 tzinfo
    lease_token = secrets.token_hex(16)
    lease_until = now + timedelta(seconds=get_settings().job_lease_seconds)
    stage_filter = ""
    params = {"lt": lease_token, "lu": lease_until, "now": now}
    if stages is not None:
        names = tuple(stages)
        stage_filter = f" AND stage IN ({','.join(f':s{i}' for i in range(len(names)))})"
        for i, name in enumerate(names):
            params[f"s{i}"] = name
    with session_factory() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.execute(
            text(
                f"""
                UPDATE jobs
                SET state='running', lease_token=:lt, lease_until=:lu, attempt=attempt+1
                WHERE id = (
                    SELECT id FROM jobs
                    WHERE state IN ('queued','retry_wait') AND not_before <= :now{stage_filter}
                    ORDER BY not_before
                    LIMIT 1
                )
                RETURNING id
                """
            ),
            params,
        ).fetchone()
        db.commit()
        if row is None:
            return None
        job = db.get(Job, row[0])
        if job is None:
            return None
        job.lease_token = lease_token
        db.commit()
        return job


def submit_with_lease(db: Session, job_id: str, lease_token: str) -> Job | None:
    """只有持有当前 lease_token 的 Worker 才能提交结果；返回当前会话中的任务对象。"""
    fresh = db.get(Job, job_id)
    if fresh is None or fresh.lease_token != lease_token or fresh.state != "running":
        return None
    return fresh


def retry_or_fail(db: Session, job: Job, error: str) -> None:
    if job.attempt >= 5:
        job.state = "failed"
        job.last_error = error
        item = db.get(Item, job.item_id)
        if item:
            item.pipeline_state = "failed"
            item.state_detail = error[:200]
    else:
        backoff = min(300, 2 ** job.attempt * 5)
        job.state = "retry_wait"
        job.last_error = error
        job.not_before = utcnow() + timedelta(seconds=backoff)


def _latest_source(db: Session, item: Item) -> SourceRevision:
    return (
        db.query(SourceRevision)
        .filter(SourceRevision.item_id == item.id, SourceRevision.revision == item.source_revision)
        .one()
    )


def run_extract(db: Session, store: ObjectStore, job: Job, item: Item) -> None:
    capture = db.get(Capture, item.capture_id)
    payload = capture.input_json or {}
    source = _latest_source(db, item)
    meta = source.metadata_json
    _run_extract_dispatch(db, store, job, item, source, payload, meta)


def _run_extract_dispatch(db: Session, store: ObjectStore, job: Job, item: Item,
                          source: SourceRevision, payload: dict, meta: dict) -> None:
    user_text = (payload.get("text") or "").strip()
    share_text = (payload.get("share_text") or "").strip()
    supplement = (meta.get("supplement_text") or "").strip()

    # 1) 用户后续补充的正文优先（补充流程语义不变）
    if supplement:
        _extract_plain_text(db, store, job, item, source, body=supplement, origin="user_supplement")
        return
    # 2) 上传的字幕文件直接规范化进入加工，不重新访问来源网站（A19）
    if _extract_from_subtitle_uploads(db, store, job, item, source):
        return
    # 3) 用户正文优先（docs/02 §5.1）；B 站/网页分享文字只是标题+链接+摘要，
    #    不能当正文（docs/04 §5），交给对应适配器处理
    is_bili = _is_bilibili_capture(payload, meta)
    web_target = _webpage_target(payload)
    body = user_text or ("" if (is_bili or web_target) else share_text)
    # 用户把视频标题粘进了正文框：标题不是正文，交给字幕适配器取真正的正文
    title_note = ""
    if body and is_bili and len(body) <= 60:
        target = payload.get("original_url") or bili.extract_first_url(share_text)
        if target and bili.is_title_text(body, bili.fetch_video_title(target)):
            title_note = body
            body = ""
    if body:
        _extract_plain_text(
            db, store, job, item, source, body=body,
            origin="user_submission" if (user_text or share_text) else "user_supplement",
        )
        return
    # 4) 音频条目（上传录音/网页音频）：正文由机器转写产生，这里不伪造
    if payload.get("primary_audio_upload_id") or meta.get("media_kind") == "audio":
        _needs_input(db, job, item,
                     "音频条目：请在详情等待/触发机器转写，或补充字幕、正文。",
                     "audio_transcribe_pending")
        return
    # 5) B 站链接：字幕适配器（docs/04）
    if is_bili:
        _extract_bilibili(db, store, job, item, source, payload, title_note=title_note)
        return
    # 6) 普通网页/公众号链接：正文适配器（docs/02 §5.1）
    if web_target:
        _extract_webpage(db, store, job, item, source, payload)
        # 采集勾选「提取音轨」：网页/公众号条目提取后自动排队转写
        _maybe_start_requested_asr(db, item, payload)
        return
    # 7) 其余只有分享文字/图片/音频：M4 其他适配器提供前不做伪造提取
    if share_text:
        _extract_plain_text(
            db, store, job, item, source, body=share_text, origin="user_submission"
        )
        return
    item.pipeline_state = "needs_input"
    item.state_detail = "缺少正文：等待来源适配器（M4）或用户补充材料"
    job.state = "succeeded"
    pipeline.emit_event(db, item.user_id, item_id=item.id, bundle_revision=item.bundle_revision,
                        event_type="item_needs_input", payload={"reason": item.state_detail})


def _maybe_start_requested_asr(db: Session, item: Item, payload: dict) -> None:
    """采集勾选「提取音轨」（include_asr）：网页/公众号条目提取完成后自动排队转写。

    只在网页适配分支调用——上传录音始终自动转写（transcribe_audio 意图，
    不经这里），B 站沿用「无字幕自动转写」用户设置，都不受该开关影响。
    幂等：同版本已有排队/进行中/成功的 run 不重复建（重新提取内容无变化时
    不重跑转写）；部署开关关闭时不建任务。实际能否取得音频由 prepare 判定。
    """
    if not payload.get("include_asr"):
        return
    settings = get_settings()
    if not asr_stage.asr_enabled(settings):
        return
    source = _latest_source(db, item)
    existing = (
        db.query(AsrRun)
        .filter(AsrRun.user_id == item.user_id, AsrRun.item_id == item.id,
                AsrRun.source_revision == source.revision)
        .order_by(AsrRun.updated_at.desc(), AsrRun.created_at.desc())
        .first()
    )
    if existing is not None and existing.state not in ("failed", "cancelled"):
        return
    asr_stage.start_asr(db, item=item, source=source,
                        model_alias=settings.asr_model, requested_by="manual")


def _extract_plain_text(db: Session, store: ObjectStore, job: Job, item: Item,
                        source: SourceRevision, *, body: str, origin: str) -> None:
    # normalized.md：带块 ID 的规范文字稿（docs/02 §6.4、§12.1）
    paragraphs = [p.strip() for p in body.split("\n") if p.strip()]
    lines = []
    segments = []
    for i, p in enumerate(paragraphs, start=1):
        seg_id = f"s{i:04d}"
        lines.append(f"{p} ^{seg_id}\n")
        segments.append({
            "segment_id": seg_id,
            "text": p,
            "artifact_file_id": None,  # 发布后回填由清单定位
            "locator": {"type": "paragraph", "index": i},
            "origin": origin,
            "confidence": None,
        })
    normalized_md = "".join(lines)
    paragraph_list = parafmt.group_paragraphs(segments)
    segments_doc = {
        "source_revision": source.revision,
        "segments": segments,
        "paragraphs": paragraph_list,
    }

    files = _bundle_files(db, item)
    files.append(pipeline.register_file(
        db, store, user_id=item.user_id, item_id=item.id,
        data=normalized_md.encode("utf-8"), relative_path="normalized.md",
        role="source_material", mime="text/markdown",
    ))
    files.append(pipeline.register_file(
        db, store, user_id=item.user_id, item_id=item.id,
        data=parafmt.paragraphs_to_readable_md(paragraph_list).encode("utf-8"),
        relative_path="readable.md", role="source_material", mime="text/markdown",
    ))
    files.append(pipeline.register_file(
        db, store, user_id=item.user_id, item_id=item.id,
        data=pipeline.canonical_json(segments_doc), relative_path="segments.json",
        role="source_material", mime="application/json",
    ))
    db.flush()

    auto_enrich = auto_enrich_enabled(db, item.user_id)
    pipeline.publish_bundle(
        db, store, item=item, source=source, files=files,
        processing_state="original_only",
        pipeline_state="enriching" if auto_enrich else "extracted",
        warnings=["已生成规范文字稿；AI 加工待执行。" if auto_enrich
                  else "已生成规范文字稿；AI 自动加工已关闭，可手动重新加工。"],
    )
    job.state = "succeeded"

    if auto_enrich:
        pipeline.enqueue_stage(
            db, user_id=item.user_id, item_id=item.id, source_revision=source.revision, stage="enrich"
        )


def _is_bilibili_capture(payload: dict, meta: dict) -> bool:
    if meta.get("platform") == "bilibili":
        return True
    candidates = [payload.get("original_url"), bili.extract_first_url(payload.get("share_text"))]
    for url in candidates:
        if not url:
            continue
        try:
            host = (urlparse(url).hostname or "").lower()
        except ValueError:
            continue
        if host == "bilibili.com" or host.endswith(".bilibili.com") or host.endswith("b23.tv"):
            return True
    return False


def _needs_input(db: Session, job: Job, item: Item, detail: str, reason: str,
                 stage: str = "extract") -> None:
    """进入补充材料：state_detail 保留人话，事件 payload 保存机器可读的
    stage/discovery_status（docs/05 §3.3），供 UI 给出下一步操作。"""
    item.pipeline_state = "needs_input"
    item.state_detail = detail[:200]
    job.state = "succeeded"
    pipeline.emit_event(db, item.user_id, item_id=item.id, bundle_revision=item.bundle_revision,
                        event_type="item_needs_input",
                        payload={"reason": reason, "detail": detail[:200], "stage": stage})


def _extract_from_subtitle_uploads(db: Session, store: ObjectStore, job: Job,
                                   item: Item, source: SourceRevision) -> bool:
    """上传的 SRT/VTT/字幕 JSON 直接规范化进入加工；无平台访问也能完成（A19）。

    返回 True 表示本路径已处理（发布或 needs_input）；False 表示没有可用的
    字幕文件，交回其他路径处理。解析不了的 .json 附件不当字幕。
    """
    rows = (
        db.query(StoredFile)
        .filter(
            StoredFile.item_id == item.id,
            StoredFile.user_id == item.user_id,
            StoredFile.relative_path.like("uploads/%"),
        )
        .order_by(StoredFile.created_at.asc(), StoredFile.id.asc())
        .all()
    )
    store_reader = ObjectStore()
    segments: list[dict] = []
    warnings: list[str] = []
    subtitle_paths: list[str] = []
    for f in rows:
        name = f.relative_path.rsplit("/", 1)[-1].lower()
        if not name.endswith((".srt", ".vtt", ".json")):
            continue
        try:
            data = store_reader.read_object(f.storage_key)
            records, kind = subfmt.parse_any(data, filename=name, mime=f.mime or "")
        except Exception:
            continue  # 不是可解析的字幕文件，留给其他路径
        source_kind = "platform_subtitle" if kind == "platform_subtitle_json" else "tool_exported_srt"
        segs, warns = subfmt.normalize_records(records, source=source_kind)
        warnings.extend(warns)
        segments.extend(segs)
        subtitle_paths.append(f.relative_path)
    if not subtitle_paths:
        return False
    if not segments:
        _needs_input(db, job, item, "上传的字幕文件没有可用片段；请检查文件内容。", "empty_subtitle")
        return True

    for i, seg in enumerate(segments, start=1):
        seg["segment_id"] = f"s{i:04d}"
    warnings.append("已从上传字幕生成规范文字稿；AI 加工待执行。")
    _publish_segments_revision(
        db, store, job, item, source,
        segments=segments, warnings=warnings, extra_files=[],
        meta_updates={
            "coverage": "full_text",
            "original_media_retained": True,
            "extractor": {"name": "subtitle_upload", "version": "subtitle_upload-1.0.0",
                          "discovery_status": "available"},
            "subtitle_sources": subtitle_paths,
        },
    )
    return True


def _user_sessdata(db: Session, user_id: str) -> tuple[str | None, str | None]:
    """读取用户托管的 B 站登录态最小凭据（docs/04 §5）。

    返回 (sessdata, 错误信息)。没有托管 → (None, None)；
    解密失败 → (None, 错误提示)，由调用方进入 needs_input。
    明文只在本函数内解密并传给提取器，不写日志、不落库。
    """
    row = (
        db.query(ProviderProfile, Credential)
        .join(Credential, Credential.profile_id == ProviderProfile.id)
        .filter(
            ProviderProfile.user_id == user_id,
            ProviderProfile.kind == "bilibili_session",
            Credential.revoked_at.is_(None),
        )
        .order_by(Credential.created_at.desc())
        .first()
    )
    if row is None:
        return None, None
    profile, cred = row
    try:
        value = cred_crypto.decrypt_secret(
            cred.encrypted_secret, cred.encrypted_dek, cred.nonces_json,
            get_settings().load_master_key(),
            user_id=user_id, profile_id=profile.id, credential_version=cred.version,
        )
    except Exception as exc:
        return None, f"B 站登录凭据解密失败（{type(exc).__name__}）；请重新提交 SESSDATA。"
    return value, None


def _extract_bilibili(db: Session, store: ObjectStore, job: Job, item: Item,
                      source: SourceRevision, payload: dict,
                      title_note: str = "") -> None:
    """B 站字幕适配器路径（docs/04）。

    用户托管了登录态（SESSDATA）则以登录态探测；network_error/blocked 上抛
    走任务级有限退避；其余状态进入补充材料，不伪造全文。
    确认 no_track 且部署/用户开关均开启时，自动转入本地 ASR 路径（docs/11 §4）。
    """
    sessdata, sess_err = _user_sessdata(db, item.user_id)
    if sess_err:
        _needs_input(db, job, item, sess_err, "login_required")
        return
    try:
        ext = bili.extract(payload.get("original_url"), share_text=payload.get("share_text"),
                           sessdata=sessdata)
    except bili.BilibiliError as exc:
        if exc.status in ("network_error", "blocked"):
            raise  # 有限退避重试，由 run_once 顶层落到任务表
        if exc.status == "no_track" and _auto_asr_ready(db, item.user_id):
            # 登录错误/其余原因不自动触发；只有明确的「平台无字幕轨」才转 ASR
            settings = get_settings()
            asr_stage.start_asr(db, item=item, source=source,
                                model_alias=settings.asr_model, requested_by="auto")
            job.state = "succeeded"
            return
        _needs_input(db, job, item, exc.message, exc.status)
        return

    if not ext.segments:
        _needs_input(db, job, item, "字幕轨存在但没有可用片段；请补充字幕文件或粘贴摘录。", "empty_subtitle")
        return

    video_id = ext.video.bvid or f"av{ext.video.aid}"
    track_id = re.sub(r"[^A-Za-z0-9_-]", "_", (ext.track or {}).get("track_id") or "track")
    original_path = f"originals/bilibili/{video_id}/P{ext.part}/{track_id}.{ext.raw_suffix}"
    original_file = pipeline.register_file(
        db, store, user_id=item.user_id, item_id=item.id,
        data=ext.raw_subtitle, relative_path=original_path,
        role="source_material", mime="application/json",
    )

    if subfmt.looks_unpunctuated(ext.segments) and _auto_asr_ready(db, item.user_id):
        # 无标点字幕轨（B 站 AI 字幕常见）读不下去，自动转本地 ASR 换带标点的
        # 转写稿；无标点字幕的原始 JSON 仍留存作证据（docs/04 §4.6 不改写原文）。
        settings = get_settings()
        asr_stage.start_asr(db, item=item, source=source,
                            model_alias=settings.asr_model, requested_by="auto")
        job.state = "succeeded"
        return

    srt_file = pipeline.register_file(
        db, store, user_id=item.user_id, item_id=item.id,
        data=subfmt.segments_to_srt(ext.segments).encode("utf-8"),
        relative_path="transcript.srt", role="source_material", mime="application/x-subrip",
    )
    warnings = list(ext.warnings) + ["已从 B 站字幕生成规范文字稿；AI 加工待执行。"]
    if title_note:
        warnings.append(f"随采集附上的「{title_note}」是视频标题，未当作正文。")
    if subfmt.looks_unpunctuated(ext.segments):
        warnings.append(
            "该字幕轨没有标点（B 站 AI 字幕常见），已按原文保留；"
            "如需可读稿可开启自动转写或补充带标点的字幕文件。"
        )
    from ..domain.source_labels import source_fields

    bili_fields = source_fields("bilibili", "video")
    _publish_segments_revision(
        db, store, job, item, source,
        segments=ext.segments, warnings=warnings, extra_files=[original_file, srt_file],
        meta_updates={
            "platform": bili_fields["platform"],
            "media_kind": bili_fields["media_kind"],
            "source_type": bili_fields["source_type"],
            "source_label": bili_fields["source_label"],
            "icon_key": bili_fields["icon_key"],
            "title": ext.title,
            "author": ext.author,
            "published_at": ext.published_at,
            "canonical_url": ext.canonical_url,
            "coverage": ext.coverage,
            "original_media_retained": True,
            "source_locator": {
                "type": "bilibili_video",
                "bvid": ext.video.bvid,
                "aid": ext.video.aid,
                "cid": ext.cid,
                "part": ext.part,
                "pages_count": ext.pages_count,
            },
            "subtitle_tracks": ext.tracks,
            "extractor": {"name": "bilibili_subtitles", "version": bili.EXTRACTOR_VERSION,
                          "discovery_status": "available",
                          "login_state_used": ext.login_state_used},
        },
    )


def _webpage_target(payload: dict) -> str | None:
    """普通网页/公众号适配目标：非 B 站、非小红书的 HTTP(S) 链接。

    小红书有登录墙与 OCR 专项路径（docs/02 §5.1、§5.5），在专用适配器
    提供前不按普通网页处理；B 站由字幕适配器负责。
    """
    url = (payload.get("original_url") or "").strip() or webpage.extract_first_url(payload.get("share_text"))
    if not url:
        return None
    platform = guess_platform(url)
    if platform in ("bilibili", "xiaohongshu"):
        return None
    return url


def _extract_webpage(db: Session, store: ObjectStore, job: Job, item: Item,
                     source: SourceRevision, payload: dict) -> None:
    """普通网页/公众号正文适配器路径（docs/02 §5.1）。

    network_error 上抛走任务级有限退避；blocked/empty_content 进入补充
    材料（用户可粘贴正文或补截图），不伪造全文。
    """
    target = _webpage_target(payload) or ""
    try:
        # 正文图片默认不提取；「提取图片」按钮走 refetch 时由 payload 带上开关
        ext = webpage.extract(target, share_text=payload.get("share_text"),
                              include_images=bool(payload.get("include_images")))
    except webpage.WebpageError as exc:
        if exc.status == "network_error":
            raise  # 有限退避重试，由 run_once 顶层落到任务表
        _needs_input(db, job, item, exc.message, exc.status)
        return

    slug = webpage.page_slug(target)
    html_file = pipeline.register_file(
        db, store, user_id=item.user_id, item_id=item.id,
        data=ext.raw_html, relative_path=f"originals/webpage/{slug}/page.html",
        role="source_material", mime=ext.raw_mime or "text/html",
    )
    extra_files = [html_file]
    for i, img in enumerate(ext.images, start=1):
        extra_files.append(pipeline.register_file(
            db, store, user_id=item.user_id, item_id=item.id,
            data=img.data, relative_path=f"originals/webpage/{slug}/img-{i:03d}.{img.ext}",
            role="source_material", mime=img.mime,
        ))

    warnings = list(ext.warnings) + ["已从网页正文生成规范文字稿；AI 加工待执行。"]
    from ..domain.source_labels import source_fields

    # 公众号仍是 wechat_mp（标签"微信公众号"），只有普通网页才是"网页"
    web_fields = source_fields(ext.platform, "text")
    _publish_segments_revision(
        db, store, job, item, source,
        segments=ext.segments, warnings=warnings, extra_files=extra_files,
        meta_updates={
            "platform": web_fields["platform"],
            "media_kind": web_fields["media_kind"],
            "source_type": web_fields["source_type"],
            "source_label": web_fields["source_label"],
            "icon_key": web_fields["icon_key"],
            "title": ext.title,
            "author": ext.author,
            "published_at": ext.published_at,
            "canonical_url": ext.canonical_url,
            "coverage": ext.coverage,
            "original_media_retained": True,  # 原始 HTML 响应已留存（不等于完整镜像）
            "source_locator": {"type": "webpage", "final_url": ext.canonical_url},
            "extractor": {"name": "webpage_article", "version": webpage.EXTRACTOR_VERSION,
                          "discovery_status": "available"},
            "images_archived": len(ext.images),
        },
        missing_materials=ext.missing_materials,
    )


def recover_expired_leases(session_factory) -> int:
    now = utcnow()
    with session_factory() as db:
        rows = db.query(Job).filter(Job.state == "running", Job.lease_until < now).all()
        for job in rows:
            job.state = "queued"
            job.lease_token = None
            job.lease_until = None
        db.commit()
        return len(rows)


def cleanup_expired_uploads(db: Session, store: ObjectStore) -> int:
    now = utcnow()
    expired = db.query(Upload).filter(Upload.state == "completed", Upload.expires_at.isnot(None), Upload.expires_at < now).all()
    cleaned = 0
    for up in expired:
        # 存在性查询：同 storage_key 可能有多条 StoredFile 登记（docs/13 §6.3）
        referenced = db.query(StoredFile).filter(StoredFile.storage_key == up.storage_key).first()
        audio_ref = db.query(AudioAsset).filter(AudioAsset.upload_id == up.id).first()
        if referenced is None and audio_ref is None:
            up.state = "expired"
            cleaned += 1
        else:
            up.expires_at = None  # 已被引用（含音频原件），保护
    db.commit()
    return cleaned


def cleanup_audio_upload_sessions(db: Session, store: ObjectStore) -> int:
    """音频上传会话 24 小时无活动过期；释放未完成会话的 staging 占用。"""
    now = utcnow()
    expired = (
        db.query(AudioUploadSession)
        .filter(AudioUploadSession.state == "receiving",
                AudioUploadSession.expires_at.isnot(None),
                AudioUploadSession.expires_at < now)
        .all()
    )
    for sess in expired:
        store.discard_staging(sess.staging_path)
        sess.state = "expired"
    if expired:
        db.commit()
    return len(expired)


def retention_sweep(session_factory, store: ObjectStore) -> dict[str, int]:
    """保留期清理（docs/02 §14.3）：到期 Bundle、孤儿文件、过期事件与幂等摘要。

    文件登记与 Bundle 清单在同一事务提交，因此"已提交但不被任何存续清单引用"
    的 StoredFile 即孤儿；运行中任务产生的文件也必然随其 Bundle 一起提交，不会被误删。
    """
    settings = get_settings()
    now = utcnow()
    stats = {"expired_uploads": 0, "expired_bundles": 0, "orphan_files": 0, "events": 0,
             "idempotency": 0, "device_auth": 0, "audio_sessions": 0}
    with session_factory() as db:
        # 未引用上传（24h 过期，docs/02 §14.3）与音频上传会话（docs/13 §6.2）
        stats["expired_uploads"] = cleanup_expired_uploads(db, store)
        stats["audio_sessions"] = cleanup_audio_upload_sessions(db, store)

        # 到期 Bundle：未回执超过未回执保留期，或已回执超过回执后保留期
        acked_cutoff = now - timedelta(days=settings.acked_bundle_retention_days)
        for bundle in db.query(BundleRevision).filter(
            BundleRevision.expires_at.isnot(None), BundleRevision.expires_at < now
        ).all():
            receipt = db.query(Receipt).filter(
                Receipt.user_id == bundle.user_id,
                Receipt.item_id == bundle.item_id,
                Receipt.bundle_revision == bundle.revision,
            ).one_or_none()
            if receipt is not None and receipt.received_at >= acked_cutoff:
                continue  # 已回执且未超过回执后保留期
            store.delete_object(bundle.manifest_key)
            db.delete(bundle)
            stats["expired_bundles"] += 1
        db.commit()

        # 孤儿文件：读取所有存续清单，收集仍被引用的对象。
        # 音频原件由 AudioAsset 持有独立引用，不随 Bundle 到期解除（docs/13 §6.3）。
        referenced: set[str] = set()
        for bundle in db.query(BundleRevision).all():
            try:
                manifest = json.loads(store.read_object(bundle.manifest_key))
            except Exception:
                continue  # 清单缺失按过期处理，不阻塞其他清理
            for entry in manifest.get("files", []):
                sf = db.query(StoredFile).filter(
                    StoredFile.user_id == bundle.user_id,
                    StoredFile.file_id == entry.get("file_id"),
                ).one_or_none()
                if sf is not None:
                    referenced.add(sf.storage_key)
        # 音频原件引用（含未完成但已登记的会话原件）
        for asset in db.query(AudioAsset).filter(AudioAsset.retention_state == "retained").all():
            up = db.get(Upload, asset.upload_id)
            if up is not None:
                referenced.add(up.storage_key)
            if asset.stored_file_id:
                sf = db.query(StoredFile).filter(
                    StoredFile.user_id == asset.user_id,
                    StoredFile.file_id == asset.stored_file_id,
                ).one_or_none()
                if sf is not None:
                    referenced.add(sf.storage_key)
        # 未完成/已完成待引用的上传会话对象
        for up in db.query(Upload).filter(Upload.state == "completed",
                                          Upload.expires_at.isnot(None)).all():
            if up.expires_at > now:
                referenced.add(up.storage_key)
        for f in db.query(StoredFile).all():
            if f.storage_key not in referenced:
                store.delete_object(f.storage_key)
                db.delete(f)
                stats["orphan_files"] += 1
        db.commit()

        ev_cutoff = now - timedelta(days=settings.event_retention_days)
        stats["events"] = db.query(Event).filter(
            Event.created_at < ev_cutoff
        ).delete(synchronize_session=False)
        stats["idempotency"] = db.query(IdempotencyRecord).filter(
            IdempotencyRecord.expires_at < now
        ).delete(synchronize_session=False)
        # 过期的插件设备授权请求（docs/05 §4.5）：过期超过 1 天清理
        stats["device_auth"] = db.query(DeviceAuthRequest).filter(
            DeviceAuthRequest.expires_at < now - timedelta(days=1)
        ).delete(synchronize_session=False)
        db.commit()
    return stats


def _auto_asr_ready(db: Session, user_id: str) -> bool:
    """no_track 自动转写的前置条件：部署开关开启 + 用户开启 + 默认模型已部署。"""
    settings = get_settings()
    if not asr_stage.asr_enabled(settings):
        return False
    if not asr_stage.user_auto_enabled(db, user_id):
        return False
    return asr_stage.model_available(settings.asr_model)


def run_once(session_factory, gate: idle_mod.AsrGate | None = None) -> bool:
    # 先领取普通任务；没有普通任务且整机空闲时才领取 ASR 工作片段（docs/11 §6.2）。
    # 按可执行 stage 过滤领取，避免抢到 ASR 任务后反复退回导致普通任务饥饿。
    job = claim_job(session_factory, NORMAL_STAGES)
    if job is None and gate is not None:
        allowed, _reason = gate.can_start(
            get_settings(), normal_busy=_normal_jobs_active(session_factory))
        if allowed:
            job = claim_job(session_factory, ASR_STAGES)
    if job is None:
        return False
    job_id = job.id
    lease_token = job.lease_token
    if job.stage == "enrich":
        try:
            enrich_stage.execute(session_factory, job_id, lease_token)
        except Exception as exc:  # noqa: BLE001 —— enrich 未分类异常按可重试处理
            with session_factory() as db2:
                job2 = db2.get(Job, job_id)
                if job2 is not None and job2.lease_token == lease_token and job2.state == "running":
                    retry_or_fail(db2, job2, f"{type(exc).__name__}: {exc}")
                    db2.commit()
        return True
    if job.stage in ASR_STAGES:
        try:
            if job.stage == "asr_prepare":
                asr_stage.execute_prepare(session_factory, job_id, lease_token, gate)
            else:
                asr_stage.execute_transcribe(session_factory, job_id, lease_token, gate)
        except Exception as exc:  # noqa: BLE001 —— Worker 顶层边界，必须把失败落到任务表
            with session_factory() as db2:
                job2 = db2.get(Job, job_id)
                if job2 is not None and job2.lease_token == lease_token and job2.state == "running":
                    retry_or_fail(db2, job2, f"{type(exc).__name__}: {exc}")
                    db2.commit()
        return True
    store = ObjectStore()
    with session_factory() as db:
        try:
            job = submit_with_lease(db, job_id, lease_token)
            if job is None:
                return False  # 租约已被恢复任务接管，放弃本次执行
            item = db.get(Item, job.item_id)
            if item is None:
                job.state = "failed"
                job.last_error = "条目不存在"
                db.commit()
            elif item.deleted_at is not None:
                job.state = "cancelled"
                db.commit()
            elif job.stage == "extract":
                item.pipeline_state = "extracting"
                db.commit()
                run_extract(db, store, job, item)
                db.commit()
        except Exception as exc:  # noqa: BLE001 —— Worker 顶层边界，必须把失败落到任务表
            db.rollback()
            with session_factory() as db2:
                job2 = db2.get(Job, job_id)
                if job2 is not None:
                    retry_or_fail(db2, job2, f"{type(exc).__name__}: {exc}")
                    db2.commit()
    return True


def _normal_jobs_active(session_factory) -> bool:
    """有普通任务正在执行或已到期待领取（ASR 空闲准入的普通任务条件）。"""
    now = utcnow()
    with session_factory() as db:
        row = db.query(Job).filter(
            Job.stage.in_(NORMAL_STAGES),
            (Job.state == "running")
            | (Job.state.in_(("queued", "retry_wait")) & (Job.not_before <= now)),
        ).first()
        return row is not None


def main() -> None:
    settings = get_settings()
    settings.ensure_dirs()
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)

    from ..models import Base
    Base.metadata.create_all(engine)  # 首版创建；后续由 Alembic 迁移管理

    recovered = recover_expired_leases(session_factory)
    if recovered:
        print(f"[worker] 恢复过期租约 {recovered} 个任务")
    if asr_stage.asr_enabled(settings):
        print("[worker] ASR 已启用（仅服务器空闲时执行；空闲准入按宿主机整机指标）")
    print("[worker] 已启动，轮询任务队列…")
    store = ObjectStore()
    gate = idle_mod.AsrGate()
    last_sweep = 0.0
    last_lease_recover = 0.0
    while True:
        try:
            now = time.time()
            if now - last_sweep >= settings.cleanup_sweep_seconds:
                try:
                    stats = retention_sweep(session_factory, store)
                    if any(stats.values()):
                        print(f"[worker] 保留期清理：{stats}")
                except Exception as exc:  # noqa: BLE001 —— 清理失败不阻塞任务处理
                    print(f"[worker] 清理异常：{type(exc).__name__}: {exc}")
                last_sweep = time.time()
            # 周期恢复过期租约，不只依赖重启（docs/11 §6.3）
            if now - last_lease_recover >= 60.0:
                last_lease_recover = now
                recovered = recover_expired_leases(session_factory)
                if recovered:
                    print(f"[worker] 周期恢复过期租约 {recovered} 个任务")
            worked = run_once(session_factory, gate)
            if not worked:
                time.sleep(settings.worker_poll_seconds)
        except KeyboardInterrupt:
            print("[worker] 收到退出信号，结束")
            break
        except Exception as exc:  # noqa: BLE001 —— 主循环兜底，避免进程退出导致队列停摆
            print(f"[worker] 循环异常：{type(exc).__name__}: {exc}")
            time.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    main()
