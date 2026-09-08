"""供应商调用执行状态（docs/05 §5.2）——从原预算账本中分离出的窄模块。

只负责「一次模型调用的生命周期」记录：prepared（已规划未发送）→ sent（请求
已发出）→ succeeded / failed / unknown_outcome。不含任何金额、价格或用量
统计：供应商账单由用户在供应商平台查看（docs/05 §1.1）。

恢复语义（与 workers/enrich.py 一致）：
- sent 表示请求可能已计费/已生效：中断恢复时标 unknown_outcome，不盲目重发。
- prepared 表示请求确定未发出：中断后可安全放弃（标 failed）并重新规划。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import ProviderOperation, new_id


def create_operation(
    db: Session,
    *,
    user_id: str,
    job_id: str | None,
    profile_id: str | None,
    request_fingerprint: str,
) -> ProviderOperation:
    """网络调用前建操作记录（state=prepared）。调用本身必须发生在事务外。"""
    op = ProviderOperation(
        user_id=user_id,
        job_id=job_id,
        profile_id=profile_id,
        request_fingerprint=request_fingerprint,
        state="prepared",
    )
    db.add(op)
    db.flush()
    return op


def mark_sent(op: ProviderOperation) -> None:
    """发送前标记（短事务提交后再发起网络请求）。"""
    op.state = "sent"


def finish_operation(op: ProviderOperation, state: str, detail: str = "") -> None:
    """在明确结果时落定状态。state ∈ succeeded|failed；unknown_outcome 请用 mark_unknown。"""
    op.state = state
    op.detail = detail[:200]


def mark_unknown(op: ProviderOperation, detail: str = "") -> None:
    """结果未知：保留记录等待人工判断，进程重启后不得自动重发。"""
    op.state = "unknown_outcome"
    op.detail = detail[:200]


def latest_for_job(db: Session, job_id: str) -> ProviderOperation | None:
    return (
        db.query(ProviderOperation)
        .filter(ProviderOperation.job_id == job_id)
        .order_by(ProviderOperation.created_at.desc(), ProviderOperation.id)
        .first()
    )


def unused_operation_id() -> str:
    return new_id()
