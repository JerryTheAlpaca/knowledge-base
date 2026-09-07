"""独立 Worker：持久化任务调度（docs/02 §7.3、§8.1）。

- BEGIN IMMEDIATE 领取到期任务；随机 lease_token、120s 租约；完成只允许当前租约提交。
- 启动恢复：过期 running 租约回到 queued。
- extract：有正文/分享文字 → 生成 normalized.md + segments.json 并发布新 Bundle，随后入 enrich；
  只有链接或附件 → needs_input（来源适配器在 M4 提供），绝不伪造正文。
- enrich：由 workers/enrich.py 执行（预算预留、模型调用、校验、发布成品 Bundle）。
"""
from __future__ import annotations

import secrets
import time
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import make_engine, make_session_factory
from ..domain import pipeline
from ..models import (
    Capture,
    Item,
    Job,
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

    text = (payload.get("text") or payload.get("share_text") or "").strip()
    supplement = (meta.get("supplement_text") or "").strip()
    body = text or supplement

    if not body:
        # 只有链接/图片/音频：M4 之前不做伪造提取
        item.pipeline_state = "needs_input"
        item.state_detail = "缺少正文：等待来源适配器（M4）或用户补充材料"
        job.state = "succeeded"
        pipeline.emit_event(db, item.user_id, item_id=item.id, bundle_revision=item.bundle_revision,
                            event_type="item_needs_input", payload={"reason": item.state_detail})
        return

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
            "origin": "user_submission" if text else "user_supplement",
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
    while True:
        try:
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
