"""Agent 任务句柄（docs/27 §异步任务表）。

为什么不是直接把 `jobs` 表的 ID 交给模型：`jobs` 按
(user, item, source_revision, stage, recipe) 唯一并会被 `enqueue_stage` **复用同一行**
——用户同时点了「重新加工」，那一行的状态就被重置，agent 手里的句柄就不再代表
「我提交的那一次」。`AgentTask` 是这次提交的凭据，`Job` 仍然是唯一的执行体：
**复用机制、不复用表**，模型调用照旧走 `ProviderOperation` 的
`sent → unknown_outcome` 语义，不在这里另起一套。

工具调用有 60 秒上限，任何要跑模型的活都只能「提交 + 轮询」，所以这里刻意不并发、
不重试在途执行，只把既有流水线的进展如实投影成一个可轮询的状态。
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..config import get_settings
from ..domain.errors import ApiError
from ..models import AgentTask, BundleRevision, Item, Job, new_id, utcnow

# kind → (入队哪个 stage, digest_requested)。两个 kind 都是「用已有材料再跑一遍」，
# 不重新抓取、不改原文、不删除任何东西。
KINDS = {
    "reprocess_item": ("enrich", True),
    "optimize_text": ("enrich", False),
}
# 句柄的最长寿命：超过就如实标 unknown_outcome，让调用方知道「结果不确定」，
# 而不是无限期挂着让模型以为还在跑
MAX_TASK_AGE_SECONDS = 2 * 3600
# 底层任务还没跑完时，句柄隔多久再看一次
RECHECK_SECONDS = 15


def submit_agent_task(db: Session, *, user_id: str, site: str, kind: str,
                      item_id: str | None, params: dict,
                      idempotency_key: str | None) -> AgentTask:
    """登记一次提交并把执行体排进既有流水线；同一 key 重复提交返回原句柄。"""
    if kind not in KINDS:
        raise ApiError("SCHEMA_INVALID",
                       f"未知任务类型：{kind}（可用：{', '.join(sorted(KINDS))}）", status_code=422)
    key = (idempotency_key or "").strip()[:64] or None
    if key is not None:
        existing = db.query(AgentTask).filter(
            AgentTask.user_id == user_id, AgentTask.site == site,
            AgentTask.idempotency_key == key,
        ).one_or_none()
        if existing is not None:
            return existing
    if not item_id:
        raise ApiError("SCHEMA_INVALID", f"{kind} 需要 item_id", status_code=422)

    from .pipeline import enqueue_stage  # 局部导入：避免 api→domain 环上的多余耦合

    item = db.query(Item).filter(Item.id == item_id, Item.user_id == user_id,
                                 Item.deleted_at.is_(None)).one_or_none()
    if item is None:
        raise ApiError("NOT_FOUND", "条目不存在", status_code=404)

    stage, digest_requested = KINDS[kind]
    job = enqueue_stage(db, user_id=user_id, item_id=item.id, source_revision=item.source_revision,
                        stage=stage, reset_attempt=False, digest_requested=digest_requested)
    task = AgentTask(
        id=new_id(), user_id=user_id, site=site, kind=kind, item_id=item.id, job_id=job.id,
        params_json=dict(params or {}), idempotency_key=key, state="queued",
        not_before=utcnow() + timedelta(seconds=RECHECK_SECONDS),
    )
    db.add(task)
    db.flush()
    return task


def claim_agent_task(session_factory) -> AgentTask | None:
    """原子领取一个到期的 agent 任务句柄（照 `claim_job` 的 BEGIN IMMEDIATE + RETURNING）。"""
    import secrets

    now = utcnow().replace(tzinfo=None)  # 原生 SQL 参数不经过 TypeDecorator，需去掉 tzinfo
    lease_token = secrets.token_hex(16)
    lease_until = now + timedelta(seconds=get_settings().job_lease_seconds)
    with session_factory() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.execute(
            text(
                """
                UPDATE agent_tasks
                SET state='running', lease_token=:lt, lease_until=:lu, attempt=attempt+1
                WHERE id = (
                    SELECT id FROM agent_tasks
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
        task = db.get(AgentTask, row[0])
        if task is None:
            return None
        task.lease_token = lease_token
        db.commit()
        return task


def _release(task: AgentTask, state: str, not_before_seconds: int) -> None:
    task.state = state
    task.not_before = utcnow() + timedelta(seconds=not_before_seconds)
    task.lease_token = None
    task.lease_until = None


def execute_agent_task(session_factory, task_id: str, lease_token: str) -> None:
    """把底层 Job 的进展投影到句柄上；本函数不执行任何模型调用。

    只有持有当前租约的循环能改这一行；状态还是 running 但租约不属于自己时直接放弃
    （与 `submit_with_lease` 同一个约定），避免恢复后的两个 Worker 互相覆盖。
    """
    with session_factory() as db:
        task = db.get(AgentTask, task_id)
        if task is None or task.lease_token != lease_token or task.state != "running":
            return
        job = db.get(Job, task.job_id) if task.job_id else None
        if job is None:
            task.state = "failed"
            task.last_error = "关联的处理任务不存在（可能已被清理）"
            task.lease_token = None
            task.lease_until = None
            db.commit()
            return
        if utcnow() - task.created_at > timedelta(seconds=MAX_TASK_AGE_SECONDS):
            # 底层还在跑但已经超出句柄寿命：如实说「不确定」，不假装成功也不重复提交
            task.state = "unknown_outcome"
            task.last_error = "任务仍在服务器上排队，已超出本次句柄的等待上限"
            task.lease_token = None
            task.lease_until = None
            db.commit()
            return
        if job.state in ("queued", "retry_wait", "running"):
            _release(task, "retry_wait", RECHECK_SECONDS)
            db.commit()
            return
        if job.state == "cancelled":
            task.state = "cancelled"
            task.last_error = "处理已取消（条目可能被删除）"
        elif job.state == "failed":
            task.state = "failed"
            # 不透传 job.last_error 原文（可能含第三方响应片段，docs/17 §10.3）
            task.last_error = "云端加工失败，请在条目的「处理记录」里看具体原因"
        else:  # succeeded
            task.state = "succeeded"
            task.result_json = _result_payload(db, task)
        task.lease_token = None
        task.lease_until = None
        db.commit()


def _result_payload(db: Session, task: AgentTask) -> dict:
    """成功后的真实产出：整理稿在哪个版本、有没有可读。"""
    item = db.get(Item, task.item_id) if task.item_id else None
    if item is None:
        return {"item_id": task.item_id}
    bundle = db.query(BundleRevision).filter(
        BundleRevision.user_id == item.user_id, BundleRevision.item_id == item.id,
        BundleRevision.processing_state.in_(["ready", "failed"]),
    ).order_by(BundleRevision.revision.desc()).first()
    return {
        "item_id": item.id,
        "pipeline_state": item.pipeline_state,
        "source_revision": item.source_revision,
        "bundle_revision": bundle.revision if bundle else item.bundle_revision,
        "processing_state": bundle.processing_state if bundle else None,
        "digest_ready": bool(bundle and bundle.processing_state == "ready"),
    }


def recover_expired_agent_leases(session_factory) -> int:
    """过期租约回到 queued：Worker 崩了句柄不会永远卡在 running（照 Job 的做法）。"""
    now = utcnow()
    with session_factory() as db:
        rows = db.query(AgentTask).filter(AgentTask.state == "running",
                                          AgentTask.lease_until < now).all()
        for task in rows:
            task.state = "queued"
            task.lease_token = None
            task.lease_until = None
            task.not_before = now
        db.commit()
        return len(rows)
