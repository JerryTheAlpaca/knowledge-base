"""独立 Worker：持久化任务调度（docs/02 §7.3、§8.1）。

- BEGIN IMMEDIATE 领取到期任务；随机 lease_token、120s 租约；完成只允许当前租约提交。
- 启动恢复：过期 running 租约回到 queued。
- extract：字幕文件上传或 B 站链接走 M4 适配器；有正文/分享文字 → 生成
  normalized.md + segments.json 并发布新 Bundle，随后入 enrich；
  其余只有链接或附件 → needs_input，绝不伪造正文。
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
from ..extractors import bilibili as bili
from ..extractors import subtitles as subfmt
from ..models import (
    BundleRevision,
    Capture,
    Event,
    IdempotencyRecord,
    Item,
    Job,
    Receipt,
    SourceRevision,
    StoredFile,
    Upload,
    utcnow,
)
from ..storage.objects import ObjectStore
from . import enrich as enrich_stage


def claim_job(session_factory) -> Job | None:
    """原子领取一个到期任务。SQLite RETURNING 保证单写者下不重复领取。"""
    now = utcnow().replace(tzinfo=None)  # 原生 SQL 参数不经过 TypeDecorator，需去掉 tzinfo
    lease_token = secrets.token_hex(16)
    lease_until = now + timedelta(seconds=get_settings().job_lease_seconds)
    with session_factory() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.execute(
            text(
                """
                UPDATE jobs
                SET state='running', lease_token=:lt, lease_until=:lu, attempt=attempt+1
                WHERE id = (
                    SELECT id FROM jobs
                    WHERE state IN ('queued','retry_wait') AND not_before <= :now
                    ORDER BY not_before
                    LIMIT 1
                )
                RETURNING id
                """
            ),
            {"lt": lease_token, "lu": lease_until, "now": now},
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


def _bundle_files(db: Session, item: Item) -> list[StoredFile]:
    rows = list(db.query(StoredFile).filter(StoredFile.item_id == item.id, StoredFile.user_id == item.user_id))
    return pipeline.latest_files_per_path(rows)


def run_extract(db: Session, store: ObjectStore, job: Job, item: Item) -> None:
    capture = db.get(Capture, item.capture_id)
    payload = capture.input_json or {}
    source = _latest_source(db, item)
    meta = source.metadata_json

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
    # 3) 用户正文；B 站分享文字只有标题+链接，不能当正文（docs/04 §5）
    is_bili = _is_bilibili_capture(payload, meta)
    body = user_text or ("" if is_bili else share_text)
    if body:
        _extract_plain_text(
            db, store, job, item, source, body=body,
            origin="user_submission" if (user_text or share_text) else "user_supplement",
        )
        return
    # 4) B 站链接：字幕适配器（docs/04）
    if is_bili:
        _extract_bilibili(db, store, job, item, source, payload)
        return
    # 5) 其余只有链接/图片/音频：M4 其他适配器提供前不做伪造提取
    item.pipeline_state = "needs_input"
    item.state_detail = "缺少正文：等待来源适配器（M4）或用户补充材料"
    job.state = "succeeded"
    pipeline.emit_event(db, item.user_id, item_id=item.id, bundle_revision=item.bundle_revision,
                        event_type="item_needs_input", payload={"reason": item.state_detail})


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
    segments_doc = {"source_revision": source.revision, "segments": segments}

    files = _bundle_files(db, item)
    files.append(pipeline.register_file(
        db, store, user_id=item.user_id, item_id=item.id,
        data=normalized_md.encode("utf-8"), relative_path="normalized.md",
        role="source_material", mime="text/markdown",
    ))
    files.append(pipeline.register_file(
        db, store, user_id=item.user_id, item_id=item.id,
        data=pipeline.canonical_json(segments_doc), relative_path="segments.json",
        role="source_material", mime="application/json",
    ))
    db.flush()

    pipeline.publish_bundle(
        db, store, item=item, source=source, files=files,
        processing_state="original_only", pipeline_state="enriching",
        warnings=["已生成规范文字稿；AI 加工待执行。"],
    )
    job.state = "succeeded"

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


def _needs_input(db: Session, job: Job, item: Item, detail: str, reason: str) -> None:
    item.pipeline_state = "needs_input"
    item.state_detail = detail[:200]
    job.state = "succeeded"
    pipeline.emit_event(db, item.user_id, item_id=item.id, bundle_revision=item.bundle_revision,
                        event_type="item_needs_input", payload={"reason": reason, "detail": detail[:200]})


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


def _extract_bilibili(db: Session, store: ObjectStore, job: Job, item: Item,
                      source: SourceRevision, payload: dict) -> None:
    """B 站字幕适配器路径（docs/04）。

    network_error/blocked 上抛走任务级有限退避；其余状态进入补充材料，
    不伪造全文，不启动 ASR。
    """
    try:
        ext = bili.extract(payload.get("original_url"), share_text=payload.get("share_text"))
    except bili.BilibiliError as exc:
        if exc.status in ("network_error", "blocked"):
            raise  # 有限退避重试，由 run_once 顶层落到任务表
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
    srt_file = pipeline.register_file(
        db, store, user_id=item.user_id, item_id=item.id,
        data=subfmt.segments_to_srt(ext.segments).encode("utf-8"),
        relative_path="transcript.srt", role="source_material", mime="application/x-subrip",
    )
    warnings = list(ext.warnings) + ["已从 B 站字幕生成规范文字稿；AI 加工待执行。"]
    _publish_segments_revision(
        db, store, job, item, source,
        segments=ext.segments, warnings=warnings, extra_files=[original_file, srt_file],
        meta_updates={
            "platform": "bilibili",
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
                          "discovery_status": "available"},
        },
    )


def _publish_segments_revision(db: Session, store: ObjectStore, job: Job, item: Item,
                               source: SourceRevision, *, segments: list[dict],
                               warnings: list[str], extra_files: list,
                               meta_updates: dict) -> None:
    """适配器产出了新材料/新规范正文 → 新增不可变来源版本并发布，随后入 enrich。

    旧版本不修改（docs/02 §6.1）；enrich 按 item.source_revision 校验片段（A13）。
    """
    new_revision = source.revision + 1
    meta2 = dict(source.metadata_json)
    meta2.update(meta_updates)
    meta2["missing_materials"] = []
    source2 = SourceRevision(
        item_id=item.id, user_id=item.user_id, revision=new_revision,
        content_hash=pipeline.sha256_hex(
            pipeline.canonical_json({"segments": segments, "meta_updates": meta_updates})
        ),
        metadata_json=meta2, artifacts_json={},
    )
    db.add(source2)
    db.flush()
    item.source_revision = new_revision

    files = _bundle_files(db, item) + list(extra_files)
    files.append(pipeline.register_file(
        db, store, user_id=item.user_id, item_id=item.id,
        data=subfmt.segments_to_normalized_md(segments).encode("utf-8"),
        relative_path="normalized.md", role="source_material", mime="text/markdown",
    ))
    files.append(pipeline.register_file(
        db, store, user_id=item.user_id, item_id=item.id,
        data=pipeline.canonical_json({"source_revision": new_revision, "segments": segments}),
        relative_path="segments.json", role="source_material", mime="application/json",
    ))
    db.flush()

    pipeline.publish_bundle(
        db, store, item=item, source=source2, files=files,
        processing_state="original_only", pipeline_state="enriching",
        warnings=warnings,
    )
    job.state = "succeeded"
    pipeline.enqueue_stage(
        db, user_id=item.user_id, item_id=item.id, source_revision=new_revision, stage="enrich"
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
        referenced = db.query(StoredFile).filter(StoredFile.storage_key == up.storage_key).one_or_none()
        if referenced is None:
            up.state = "expired"
            cleaned += 1
        else:
            up.expires_at = None  # 已被引用，保护
    db.commit()
    return cleaned


def retention_sweep(session_factory, store: ObjectStore) -> dict[str, int]:
    """保留期清理（docs/02 §14.3）：到期 Bundle、孤儿文件、过期事件与幂等摘要。

    文件登记与 Bundle 清单在同一事务提交，因此"已提交但不被任何存续清单引用"
    的 StoredFile 即孤儿；运行中任务产生的文件也必然随其 Bundle 一起提交，不会被误删。
    """
    settings = get_settings()
    now = utcnow()
    stats = {"expired_uploads": 0, "expired_bundles": 0, "orphan_files": 0, "events": 0, "idempotency": 0}
    with session_factory() as db:
        # 未引用上传（24h 过期，docs/02 §14.3）
        stats["expired_uploads"] = cleanup_expired_uploads(db, store)

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

        # 孤儿文件：读取所有存续清单，收集仍被引用的对象
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
        db.commit()
    return stats


def run_once(session_factory) -> bool:
    job = claim_job(session_factory)
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
    print("[worker] 已启动，轮询任务队列…")
    store = ObjectStore()
    last_sweep = 0.0
    while True:
        try:
            if time.time() - last_sweep >= settings.cleanup_sweep_seconds:
                try:
                    stats = retention_sweep(session_factory, store)
                    if any(stats.values()):
                        print(f"[worker] 保留期清理：{stats}")
                except Exception as exc:  # noqa: BLE001 —— 清理失败不阻塞任务处理
                    print(f"[worker] 清理异常：{type(exc).__name__}: {exc}")
                last_sweep = time.time()
            worked = run_once(session_factory)
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
