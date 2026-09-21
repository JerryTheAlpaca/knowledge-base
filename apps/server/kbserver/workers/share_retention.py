"""分享对象的生命周期与统一存活判断（docs/20 §13.2、§13.3）。

- 删除判断只看一件事：这个物理对象还有没有任何登记在引用它。StoredFile、Bundle
  清单、Upload、AudioAsset、ShareArtifact 与活动任务的临时持有都算数——存储按内容
  哈希去重，不能因为归属表不同就假定物理文件不同。
- 每批只做本地删除：引用复查、unlink 与解除登记放在同一个 SQLite 写事务里；
  网络、模型与浏览器不得进入这个事务。
- 存续作品的最新可用草稿、已发布版本、活动任务依赖的版本与对话/摘要一律保留，
  不按「没有运行租约」当孤儿清理；反过来，被新版本取代且过了 share_revision_retention_days
  的旧版本、过了诊断保留期的旧任务产物就是在这里回收（审查 C-05）。
- 删除的作品按 deleted_at + 宽限期整批回收：宽限期内还能回捞，过期后产物与
  run/会话/消息/版本这些文本行一起清掉（审查 C-12）。
"""
from __future__ import annotations

import shutil
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..models import (
    AudioAsset,
    BundleRevision,
    ProviderOperation,
    ShareArtifact,
    ShareConversation,
    ShareMessage,
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

# run 的终态：落定之后只按诊断保留期保护，不再算「活动任务」
RUN_FINAL_STATES = ("succeeded", "failed", "cancelled", "unknown_outcome")
# 删除作品后的回捞宽限期（天）：过期即整批回收产物与文本行
DELETE_GRACE_DAYS = 1


def share_referenced_keys(db: Session) -> set[str]:
    """所有仍被 ShareArtifact 登记的对象 key，供统一孤儿清理使用。"""
    return {row[0] for row in db.query(ShareArtifact.storage_key).distinct().all()}


def _retained_ids(db: Session, now) -> tuple[set[str], set[str]]:
    """返回 (仍保留的 revision_id 集合, 仍保留的 run_id 集合)。

    版本侧：当前 ready／已发布版本，以及未满 share_revision_retention_days 的历史版本。
    任务侧：还没落定的任务、未满诊断保留期的 run，以及产出上面这些版本的 run——
    版本的 synthesis／page_source／brief／对话产物都登记在 run_id 上（不是
    revision_id），不把产出链一起保住，就会把仍然可用的版本掏成空壳。
    """
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
            if revision.id in keep_revisions or revision.created_at >= revision_cutoff:
                keep_revisions.add(revision.id)
                if revision.run_id:
                    keep_runs.add(revision.run_id)
    for run in db.query(ShareRun).filter(ShareRun.state.notin_(RUN_FINAL_STATES)).all():
        keep_runs.add(run.id)
    # 失败/取消/已完成任务的诊断按更短保留期回收；未到期前也要能重试与回看
    for run in db.query(ShareRun).filter(ShareRun.state.in_(RUN_FINAL_STATES)).all():
        if run.updated_at >= run_cutoff:
            keep_runs.add(run.id)
    return keep_revisions, keep_runs


def reclaim_expired_share_objects(session_factory, store: ObjectStore | None = None) -> dict:
    """按保留策略解除引用并回收物理对象；返回统计。

    正向保护只有两条：产物所属 revision 仍在保留范围内，或所属 run 仍受保护。
    两条都不满足的就是「超过保留期的非当前版本」与「已落定的旧任务产物」——
    这才是真正回收的那一条（审查 C-05）。已删除的作品不进这两个集合，按
    deleted_at + 宽限期整批回收，宽限期过后连同 run／会话／消息／版本这些文本行
    一起清掉（审查 C-12）。
    """
    settings = get_settings()
    store = store or ObjectStore()
    now = utcnow()
    stats = {"released": 0, "deleted_objects": 0, "spool_dirs": 0, "purged_works": 0}
    grace = timedelta(days=DELETE_GRACE_DAYS)
    with session_factory() as db:
        keep_revisions, keep_runs = _retained_ids(db, now)
        deleted_works = {w.id: w.deleted_at for w in
                         db.query(ShareWork).filter(ShareWork.deleted_at.isnot(None)).all()}
        reclaimable: list[ShareArtifact] = []
        for artifact in db.query(ShareArtifact).all():
            deleted_at = deleted_works.get(artifact.work_id)
            if deleted_at is not None:
                if deleted_at + grace > now:
                    continue  # 删除宽限期内仍可回捞，过期后整批回收
                reclaimable.append(artifact)
                continue
            if artifact.run_id in keep_runs or artifact.revision_id in keep_revisions:
                continue
            if artifact.expires_at is not None and artifact.expires_at < now:
                reclaimable.append(artifact)
                continue
            if artifact.run_id is None and artifact.revision_id is None:
                # 既不属于版本也不属于任务：判不出「是否已被取代」，保守留着，
                # 只按显式 expires_at 回收（作品删除走上面的分支）
                continue
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
        stats["purged_works"] = _purge_deleted_work_rows(db, deleted_works, now, grace)
        db.commit()
    stats["spool_dirs"] = _sweep_spool(settings, now)
    return stats


def _purge_deleted_work_rows(db: Session, deleted_works: dict[str, datetime | None],
                             now, grace: timedelta) -> int:
    """宽限期已过的删除作品：回收 run/会话/消息/版本行，返回清掉的作品数。

    用户最初的要求存在 share_runs.request_text（原文，最多 8000 字），对话正文是
    对象、share_messages 是行；只删对象不删行等于把用户写过的话永久留在库里。
    作品行保留 tombstone（deleted_at 就是它被删过的凭据），指针列一并置空，
    免得指向已经消失的版本与任务。
    """
    purged = 0
    for work_id, deleted_at in deleted_works.items():
        if deleted_at is None or deleted_at + grace > now:
            continue
        work = db.get(ShareWork, work_id)
        conv_ids = [row[0] for row in
                    db.query(ShareConversation.id).filter(ShareConversation.work_id == work_id).all()]
        run_ids = [row[0] for row in
                   db.query(ShareRun.id).filter(ShareRun.work_id == work_id).all()]
        # 先子后父：消息 → 模型调用归属 → 版本 → run（run 引用会话）→ 会话
        rows = 0
        if conv_ids:
            rows += db.query(ShareMessage).filter(
                ShareMessage.conversation_id.in_(conv_ids)).delete(synchronize_session=False)
        if run_ids or conv_ids:
            rows += db.query(ProviderOperation).filter(or_(
                ProviderOperation.share_run_id.in_(run_ids),
                ProviderOperation.conversation_id.in_(conv_ids))).delete(
                synchronize_session=False)
        rows += db.query(ShareRevision).filter(ShareRevision.work_id == work_id).delete(
            synchronize_session=False)
        if run_ids:
            rows += db.query(ShareRun).filter(ShareRun.id.in_(run_ids)).delete(
                synchronize_session=False)
        if conv_ids:
            rows += db.query(ShareConversation).filter(
                ShareConversation.id.in_(conv_ids)).delete(synchronize_session=False)
        if not rows:
            continue    # 上一轮已经清过：不再重复计数，也不再挪动 updated_at
        if work is not None:
            work.latest_ready_revision_id = None
            work.published_revision_id = None
            work.active_run_id = None
            work.updated_at = utcnow()
        purged += 1
    return purged


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
