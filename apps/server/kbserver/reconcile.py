"""供应商操作对账（docs/02 §7.3，M5 遗留项）。

语义（与 workers/enrich.py 恢复路径一致）：
- state=sent：请求已发出、结果未知，可能已计费。任务仍会重试的由恢复路径
  自动转为 unknown_outcome；任务已终止（failed/cancelled）或任务行丢失时，
  预留无人处理，需要人工核对供应商账单后落账。
- state=reserved：请求确定未发出（标记 sent 之前中断）。退款不涉及误判，
  可直接按未计费处理。

CLI 输出：`python -m kbserver.cli reconcile [--resolve billed|refunded]`。
- billed：确认已计费 → 按预留金额结算（预留转为已提交费用）。
- refunded：确认未计费 → 全额退款。
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from .domain import budget
from .models import Item, Job, ProviderOperation, utcnow

TERMINAL_JOB_STATES = {"failed", "cancelled"}


def stuck_operations(db: Session, *, min_age_hours: float = 1.0) -> list[dict]:
    """找出任务已终止或任务行丢失、仍停留在 sent/reserved 的供应商操作。

    任务仍在队列/重试中的由 enrich 恢复路径自动对账，不在此列出。
    """
    cutoff = utcnow() - timedelta(hours=min_age_hours)
    rows = (
        db.query(ProviderOperation)
        .filter(
            ProviderOperation.state.in_(("sent", "reserved")),
            ProviderOperation.created_at < cutoff,
        )
        .order_by(ProviderOperation.created_at.asc())
        .all()
    )
    out: list[dict] = []
    for op in rows:
        job = db.get(Job, op.job_id) if op.job_id else None
        if job is not None and job.state not in TERMINAL_JOB_STATES:
            continue  # 任务还有恢复机会，不在此对账
        item = db.get(Item, job.item_id) if job else None
        snapshot = budget.operation_price_snapshot(db, op)
        out.append({
            "operation": op,
            "operation_id": op.id,
            "user_id": op.user_id,
            "state": op.state,
            "job_state": job.state if job else None,
            "item_deleted": bool(item is not None and item.deleted_at is not None),
            "reserved_cost": int(op.reserved_cost),
            "currency": snapshot.get("currency") or "CNY",
            "created_at": op.created_at,
        })
    return out


def resolve_operation(db: Session, op: ProviderOperation, outcome: str) -> None:
    """按人工核实结果落账：billed（已计费）或 refunded（未计费退款）。"""
    snapshot = budget.operation_price_snapshot(db, op)
    currency = snapshot.get("currency") or "CNY"
    if outcome == "billed":
        if op.state == "reserved":
            # reserved 请求确定未发出，不可能已计费
            raise ValueError("reserved 状态的操作确定未发出，只能按 refunded 处理")
        budget.settle_operation(
            db, op, actual_cost=int(op.reserved_cost), usage={},
            currency=currency, price_snapshot=snapshot,
        )
    elif outcome == "refunded":
        budget.refund_operation(db, op, currency=currency, price_snapshot=snapshot)
    else:
        raise ValueError(f"未知对账结果：{outcome}")
