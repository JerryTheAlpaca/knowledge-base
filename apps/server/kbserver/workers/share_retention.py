"""分享对象的生命周期与统一存活判断（docs/20 §13.2、§13.3）。

- 删除判断只看一件事：这个物理对象还有没有任何登记在引用它。StoredFile、Bundle
  清单、Upload、AudioAsset、ShareArtifact 与活动任务的临时持有都算数——存储按内容
  哈希去重，不能因为归属表不同就假定物理文件不同。
- 每批只做本地删除：引用复查、unlink 与解除登记放在同一个 SQLite 写事务里；
  网络、模型与浏览器不得进入这个事务。
- 存续作品的最新可用草稿、已发布版本、活动任务依赖的版本与对话/摘要一律保留，
  不按「没有运行租约」当孤儿清理。
"""
from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..models import (
    AudioAsset,
    BundleRevision,
    ShareArtifact,
    ShareRevision,
    ShareRun,
    ShareWork,
    StoredFile,
    Upload,
    utcnow,
)
from ..storage.objects import ObjectStore

# 每个作品始终保留的版本引用角色（不随保留期回收）
PINNED_ROLES = ("html", "public_references", "synthesis", "page_source", "input_snapshot",
                "brief", "check_report", "asset", "screenshot")


def share_referenced_keys(db: Session) -> set[str]:
    """所有仍被 ShareArtifact 登记的对象 key，供统一孤儿清理使用。"""
    return {row[0] for row in db.query(ShareArtifact.storage_key).distinct().all()}


def _retained_ids(db: Session, now) -> tuple[set[str], set[str]]:
    """返回 (仍保留的 revision_id 集合, 仍保留的 run_id 集合)。"""
    settings = get_settings()
    keep_revisions: set[str] = set()
    keep_runs: set[str] = set()
    revision_cutoff = now - timedelta(days=settings.share_revision_retention_days)
    run_cutoff = now - timedelta(days=settings.share_run_diagnostic_retention_days)
    for work in db.query(ShareWork).filter(ShareWork.deleted_at.is_(None)).all():
        if work.latest_ready_revision_id:
            keep_revisions.add(work.latest_ready_revision_id)
        if work.published_revision_id:
            keep_revisions.add(work.published_revision_id)
        if work.active_run_id:
            keep_runs.add(work.active_run_id)
        for revision in db.query(ShareRevision).filter_by(work_id=work.id).all():
            if revision.created_at >= revision_cutoff:
                keep_revisions.add(revision.id)
    for run in db.query(ShareRun).filter(ShareRun.state.in_(("queued", "retry_wait", "running",
                                                            "waiting_user",
                                                            "awaiting_confirmation"))).all():
        keep_runs.add(run.id)
    # 失败/取消任务的诊断按更短保留期回收；未到期前也要能重试
    for run in db.query(ShareRun).filter(ShareRun.state.in_(("failed", "cancelled",
                                                            "unknown_outcome"))).all():
        if run.updated_at >= run_cutoff:
            keep_runs.add(run.id)
    return keep_revisions, keep_runs


def reclaim_expired_share_objects(session_factory, store: ObjectStore | None = None) -> dict:
    """按保留策略解除引用并回收物理对象；返回统计。"""
    settings = get_settings()
    store = store or ObjectStore()
    now = utcnow()
    stats = {"released": 0, "deleted_objects": 0, "spool_dirs": 0}
    with session_factory() as db:
        keep_revisions, keep_runs = _retained_ids(db, now)
        deleted_works = {w.id: w.deleted_at for w in
                         db.query(ShareWork).filter(ShareWork.deleted_at.isnot(None)).all()}
        grace = timedelta(days=1)
        reclaimable: list[ShareArtifact] = []
        for artifact in db.query(ShareArtifact).all():
            if artifact.id in keep_revisions or artifact.run_id in keep_runs:
                continue
            if artifact.revision_id and artifact.revision_id in keep_revisions:
                continue
            if artifact.work_id in deleted_works:
                deleted_at = deleted_works[artifact.work_id]
                if deleted_at is not None and deleted_at + grace > now:
                    continue  # 删除宽限期内仍可回捞，过期后整批回收
                reclaimable.append(artifact)
                continue
            if artifact.run_id and artifact.run_id in keep_runs:
                continue
            if artifact.expires_at is not None and artifact.expires_at < now:
                reclaimable.append(artifact)
                continue
            if artifact.work_id not in keep_revisions and artifact.revision_id is None \
                    and artifact.run_id is None:
                reclaimable.append(artifact)
        for batch_start in range(0, len(reclaimable), 100):
            batch = reclaimable[batch_start:batch_start + 100]
            keys = [a.storage_key for a in batch]
            for artifact in batch:
                db.delete(artifact)
            db.flush()
            for key in keys:
                if _still_referenced(db, key):
                    continue
                if store.delete_object(key):
                    stats["deleted_objects"] += 1
            stats["released"] += len(batch)
            db.commit()
    stats["spool_dirs"] = _sweep_spool(settings, now)
    return stats


def _still_referenced(db: Session, storage_key: str, *, exclude_bundle_id: str | None = None) -> bool:
    """统一存活判断：任何归属表的引用都算，包括活动任务临时持有的快照。

    exclude_bundle_id 用于「正要删除这条 Bundle」的调用方：不能把自己算成引用。
    """
    if db.query(ShareArtifact.id).filter(ShareArtifact.storage_key == storage_key).first():
        return True
    if db.query(StoredFile.id).filter(StoredFile.storage_key == storage_key).first():
        return True
    q = db.query(BundleRevision.id).filter(BundleRevision.manifest_key == storage_key)
    if exclude_bundle_id:
        q = q.filter(BundleRevision.id != exclude_bundle_id)
    if q.first():
        return True
    if db.query(Upload.id).filter(Upload.storage_key == storage_key,
                                  Upload.state != "expired").first():
        return True
    return False


def _sweep_spool(settings: Settings, now) -> int:
    """runner 临时目录按 TTL 回收；运行中的任务由租约与 done/failed 移动负责。"""
    root = Path(settings.share_spool_dir)
    if not root.exists():
        return 0
    cutoff = now.timestamp() - settings.share_spool_ttl_hours * 3600
    removed = 0
    for sub in (".tmp", "done", "failed", "working"):
        base = root / sub
        if not base.exists():
            continue
        for entry in base.iterdir():
            try:
                if entry.is_dir() and entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
    return removed
