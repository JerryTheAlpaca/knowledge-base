"""供应商操作对账（M5 遗留）单元测试。

覆盖：任务终止/任务丢失时 sent/reserved 操作被列出、运行中任务不列出、
refunded 退款落账、billed 按预留结算、reserved 拒绝 billed。
"""
from __future__ import annotations

import secrets

import pytest

from kbserver import reconcile
from kbserver.domain import budget
from kbserver.models import (
    Capture,
    Item,
    Job,
    ProviderProfile,
    UsageLedger,
    utcnow,
)


def _mk_job(db, user_id: str, state: str = "failed"):
    cap = Capture(
        user_id=user_id,
        client_capture_id=f"cc-{secrets.token_hex(8)}",
        request_hash="test-hash",
        input_json={},
        received_at=utcnow(),
    )
    db.add(cap)
    db.flush()
    item = Item(user_id=user_id, capture_id=cap.id, pipeline_state="enriching")
    db.add(item)
    db.flush()
    job = Job(user_id=user_id, item_id=item.id, source_revision=1,
              stage="enrich", recipe_hash="h", state=state)
    db.add(job)
    db.flush()
    return job, item


def _mk_op(db, user_id: str, job_id: str | None, state: str, reserved: int = 500_000):
    profile = ProviderProfile(
        user_id=user_id, kind="llm", adapter="openai-compatible",
        endpoint="https://api.example.com/v1", model="test-model",
    )
    db.add(profile)
    db.flush()
    op = budget.create_operation(
        db, user_id=user_id, job_id=job_id, profile_id=profile.id,
        request_fingerprint=f"fp-{secrets.token_hex(4)}",
        reserved_cost=reserved, currency="CNY",
        price_snapshot={"currency": "CNY"},
    )
    if state != "reserved":
        op.state = state  # 模拟 enrich 标记 sent 后中断
    db.flush()
    return op


def test_stuck_listed_for_terminal_and_missing_jobs(db, user_a):
    uid = user_a["user_id"]
    job, _item = _mk_job(db, uid, state="failed")
    op_failed = _mk_op(db, uid, job.id, state="sent")
    op_reserved = _mk_op(db, uid, None, state="reserved")  # 任务行丢失

    stuck = reconcile.stuck_operations(db, min_age_hours=0.0)
    ids = {s["operation_id"] for s in stuck}
    assert op_failed.id in ids and op_reserved.id in ids
    entry = next(s for s in stuck if s["operation_id"] == op_failed.id)
    assert entry["job_state"] == "failed"
    assert entry["reserved_cost"] == 500_000


def test_running_job_not_listed(db, user_a):
    uid = user_a["user_id"]
    job, _item = _mk_job(db, uid, state="queued")
    op = _mk_op(db, uid, job.id, state="sent")

    # 测试库跨用例共享，只断言本用例的操作未被列出（运行中任务由恢复路径对账）
    stuck_ids = {s["operation_id"] for s in reconcile.stuck_operations(db, min_age_hours=0.0)}
    assert op.id not in stuck_ids


def test_resolve_refunded(db, user_a):
    uid = user_a["user_id"]
    job, _item = _mk_job(db, uid, state="failed")
    op = _mk_op(db, uid, job.id, state="reserved")

    reconcile.resolve_operation(db, op, "refunded")
    db.flush()
    assert op.state == "failed"
    refund = (
        db.query(UsageLedger)
        .filter(UsageLedger.operation_id == op.id, UsageLedger.event_type == "refund")
        .one()
    )
    assert refund.estimated_cost == -500_000
    # 净已提交回落为 0
    assert budget.usage_summary(db, uid, utcnow().strftime("%Y-%m"))["committed"] == 0


def test_resolve_billed_settles_at_reserved(db, user_a):
    uid = user_a["user_id"]
    job, _item = _mk_job(db, uid, state="failed")
    op = _mk_op(db, uid, job.id, state="sent")

    reconcile.resolve_operation(db, op, "billed")
    db.flush()
    assert op.state == "succeeded"
    settle = (
        db.query(UsageLedger)
        .filter(UsageLedger.operation_id == op.id, UsageLedger.event_type == "settle")
        .one()
    )
    assert settle.estimated_cost == 0  # 已计费：预留转为实际费用，无差额
    summary = budget.usage_summary(db, uid, utcnow().strftime("%Y-%m"))
    assert summary["committed"] == 500_000


def test_reserved_cannot_be_billed(db, user_a):
    uid = user_a["user_id"]
    job, _item = _mk_job(db, uid, state="failed")
    op = _mk_op(db, uid, job.id, state="reserved")

    with pytest.raises(ValueError):
        reconcile.resolve_operation(db, op, "billed")
