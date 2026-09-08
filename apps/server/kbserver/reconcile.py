"""供应商操作对账（docs/05 §5：去计费后仅保留调用状态处置）。

语义（与 workers/enrich.py 恢复路径一致）：
- state=sent：请求已发出、结果未知。任务仍会重试的由恢复路径自动转为
  unknown_outcome；任务已终止（failed/cancelled）或任务行丢失时，操作
  无人处理，需要人工处置。
- state=prepared：请求确定未发出（标记 sent 之前中断），可直接按失败处置。

CLI 输出：`python -m kbserver.cli reconcile [--resolve unknown_outcome|failed]`。
- unknown_outcome：把 sent 记为「结果未知」，条目显示结果未知，用户可显式重新加工。
- failed：把 prepared 记为「未发出即中断」，可安全重新规划。
不自动把任何操作记为成功，也不自动重发。
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from .domain import provider_ops
from .models import Item, Job, ProviderOperation, utcnow

TERMINAL_JOB_STATES = {"failed", "cancelled"}


def stuck_operations(db: Session, *, min_age_hours: float = 1.0) -> list[dict]:
    """找出任务已终止或任务行丢失、仍停留在 sent/prepared 的供应商操作。

    任务还在队列/重试中的由 enrich 恢复路径自动处置，不在此列出。
    """
    cutoff = utcnow() - timedelta(hours=min_age_hours)
    rows = (
        db.query(ProviderOperation)
        .filter(
            ProviderOperation.state.in_(("sent", "prepared", "reserved")),
            ProviderOperation.created_at < cutoff,
        )
        .order_by(ProviderOperation.created_at.asc())
        .all()
    )
    out: list[dict] = []
    for op in rows:
        job = db.get(Job, op.job_id) if op.job_id else None
        if job is not None and job.state not in TERMINAL_JOB_STATES:
            continue  # 任务还有恢复机会，不在此处置
        item = db.get(Item, job.item_id) if job else None
        out.append({
            "operation": op,
            "operation_id": op.id,
            "user_id": op.user_id,
            "state": op.state,
            "job_state": job.state if job else None,
            "item_deleted": bool(item is not None and item.deleted_at is not None),
            "item_state": item.pipeline_state if item else None,
            "created_at": op.created_at,
        })
    return out


def resolve_operation(db: Session, op: ProviderOperation, outcome: str) -> None:
    """按人工判断落定调用状态（不含任何金额语义）。"""
    if outcome == "unknown_outcome":
        if op.state == "prepared":
            raise ValueError("prepared 状态的操作确定未发出，应按 failed 处置")
        provider_ops.mark_unknown(op, "人工对账：结果未知，可显式重新加工")
    elif outcome == "failed":
        provider_ops.finish_operation(op, "failed", "人工对账：未发出即中断")
    else:
        raise ValueError(f"未知对账结果：{outcome}")
