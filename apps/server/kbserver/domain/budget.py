"""调用预算账本（docs/02 §14.2）。

- 金额一律整数微单位（1 元 = 1_000_000 micro）。
- 每次计费调用前预留最坏合理费用；成功按 usage 结算释放差额；明确失败退款；
  未知结果保留预留，待对账。
- 月度已提交 = 账本所有事件（reserve + settle 差额 + refund）之和。
- 费用不明（未配置价格）时走"只能统计用量"模式：预留 0、照常记账数量。
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import ProviderOperation, UsageLedger, User, new_id, utcnow

MICRO = 1_000_000
DEFAULT_MONTHLY_BUDGET_MICRO = 20 * MICRO  # 附件中的 20 元/月初始预算
BUDGET_WARN_RATIO = 0.8


def normalize_prices(prices: dict | None) -> dict:
    """API 输入的单价（元/百万 tokens）规范化为微单位存储。"""
    if not prices:
        return {}
    out: dict = {}
    if prices.get("input_per_1m") is not None:
        out["input_per_1m_micro"] = int(round(float(prices["input_per_1m"]) * MICRO))
    if prices.get("output_per_1m") is not None:
        out["output_per_1m_micro"] = int(round(float(prices["output_per_1m"]) * MICRO))
    if prices.get("currency"):
        out["currency"] = str(prices["currency"])[:8]
    if prices.get("source"):
        out["source"] = str(prices["source"])[:200]
    out["updated_at"] = utcnow().isoformat()
    return out


def estimate_cost_micro(input_tokens: int, output_tokens: int, prices: dict) -> int | None:
    """估算单次调用费用；价格缺失返回 None（usage-only 模式）。"""
    in_price = prices.get("input_per_1m_micro")
    out_price = prices.get("output_per_1m_micro")
    if in_price is None and out_price is None:
        return None
    cost = 0
    if in_price:
        cost += input_tokens * int(in_price) // MICRO
    if out_price:
        cost += output_tokens * int(out_price) // MICRO
    return cost


def budget_of(user: User) -> tuple[int, str]:
    """(月预算微单位, 货币)。默认 20 元 CNY。"""
    s = user.settings_json or {}
    budget = s.get("monthly_budget_micro")
    if not isinstance(budget, int) or budget < 0:
        budget = DEFAULT_MONTHLY_BUDGET_MICRO
    currency = s.get("currency") or "CNY"
    return budget, currency


def month_committed(db: Session, user_id: str, month: str) -> int:
    """某月（UTC，YYYY-MM）已提交费用（微单位）。"""
    total = db.scalar(
        select(func.coalesce(func.sum(UsageLedger.estimated_cost), 0)).where(
            UsageLedger.user_id == user_id,
            func.strftime("%Y-%m", UsageLedger.created_at) == month,
        )
    )
    return int(total or 0)


def ledger_entry(
    db: Session,
    *,
    user_id: str,
    operation_id: str,
    event_type: str,
    estimated_cost: int,
    currency: str = "CNY",
    unit: str = "token",
    quantity: int = 0,
    price_snapshot: dict | None = None,
) -> None:
    db.add(
        UsageLedger(
            user_id=user_id,
            operation_id=operation_id,
            currency=currency,
            unit=unit,
            quantity=int(quantity or 0),
            estimated_cost=int(estimated_cost),
            price_snapshot_json=price_snapshot or {},
            event_type=event_type,
        )
    )


def create_operation(
    db: Session,
    *,
    user_id: str,
    job_id: str | None,
    profile_id: str,
    request_fingerprint: str,
    reserved_cost: int,
    currency: str = "CNY",
    price_snapshot: dict | None = None,
) -> ProviderOperation:
    """网络调用前建操作记录并写 reserve 账目（docs/02 §7.2）。"""
    op = ProviderOperation(
        user_id=user_id,
        job_id=job_id,
        profile_id=profile_id,
        request_fingerprint=request_fingerprint,
        state="reserved",
        reserved_cost=int(reserved_cost),
    )
    db.add(op)
    db.flush()
    if reserved_cost:
        ledger_entry(
            db,
            user_id=user_id,
            operation_id=op.id,
            event_type="reserve",
            estimated_cost=int(reserved_cost),
            currency=currency,
            price_snapshot=price_snapshot,
        )
    return op


def operation_price_snapshot(db: Session, op: ProviderOperation) -> dict:
    """从 reserve 账目读价格快照（操作行本身不存价格，docs/02 §14.2）。"""
    row = db.query(UsageLedger).filter(
        UsageLedger.operation_id == op.id,
        UsageLedger.event_type == "reserve",
    ).first()
    return dict((row.price_snapshot_json if row else None) or {})


def settle_operation(
    db: Session,
    op: ProviderOperation,
    *,
    actual_cost: int,
    usage: dict,
    currency: str = "CNY",
    price_snapshot: dict | None = None,
) -> None:
    """成功结算：记录实际用量并释放差额（可为负）。"""
    delta = int(actual_cost) - int(op.reserved_cost)
    tokens = int(usage.get("total_tokens") or 0)
    ledger_entry(
        db,
        user_id=op.user_id,
        operation_id=op.id,
        event_type="settle",
        estimated_cost=delta,
        currency=currency,
        quantity=tokens,
        price_snapshot=price_snapshot,
    )
    op.state = "succeeded"
    op.actual_usage_json = usage


def refund_operation(
    db: Session, op: ProviderOperation, *, currency: str = "CNY", price_snapshot: dict | None = None
) -> None:
    """明确失败：全额退款。未知结果不退款（保留预留待对账）。"""
    if op.reserved_cost:
        ledger_entry(
            db,
            user_id=op.user_id,
            operation_id=op.id,
            event_type="refund",
            estimated_cost=-int(op.reserved_cost),
            currency=currency,
            price_snapshot=price_snapshot,
        )
    op.state = "failed"


def usage_summary(db: Session, user_id: str, month: str) -> dict:
    """按月汇总：预留、结算差额、退款、净已提交、用量 tokens、分配置统计。"""
    rows = db.execute(
        select(
            UsageLedger.event_type,
            func.sum(UsageLedger.estimated_cost),
            func.sum(UsageLedger.quantity),
            ProviderOperation.profile_id,
        )
        .join(ProviderOperation, UsageLedger.operation_id == ProviderOperation.id, isouter=True)
        .where(UsageLedger.user_id == user_id, func.strftime("%Y-%m", UsageLedger.created_at) == month)
        .group_by(UsageLedger.event_type, ProviderOperation.profile_id)
    ).all()
    reserved = 0
    settled_delta = 0
    refunded = 0
    tokens = 0
    by_profile: dict[str, dict] = {}

    def bucket(profile_id: str | None) -> dict:
        return by_profile.setdefault(
            profile_id or "unattributed", {"reserved": 0, "settled_delta": 0, "refunded": 0, "net": 0, "tokens": 0}
        )

    for event_type, cost_sum, qty_sum, profile_id in rows:
        cost_sum = int(cost_sum or 0)
        qty_sum = int(qty_sum or 0)
        b = bucket(profile_id)
        if event_type == "reserve":
            reserved += cost_sum
            b["reserved"] += cost_sum
        elif event_type == "settle":
            settled_delta += cost_sum
            tokens += qty_sum
            b["settled_delta"] += cost_sum
            b["tokens"] += qty_sum
        elif event_type == "refund":
            refunded += cost_sum
            b["refunded"] += cost_sum
    for b in by_profile.values():
        b["net"] = b["reserved"] + b["settled_delta"] + b["refunded"]
    net = reserved + settled_delta + refunded
    return {
        "month": month,
        "reserved": reserved,
        "settled_delta": settled_delta,
        "refunded": refunded,
        "committed": net,
        "tokens": tokens,
        "by_profile": by_profile,
    }
