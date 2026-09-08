"""供应商操作对账集成测试（docs/05 §5：去计费后仅保留调用状态处置）。

覆盖：任务终止/任务丢失时 sent/prepared 操作被列出、运行中任务不列出、
unknown_outcome 处置、failed 处置、prepared 拒绝 unknown_outcome、
CLI 入口。
"""
from __future__ import annotations

import argparse as _argparse
from datetime import timedelta

import pytest

from kbserver.domain import provider_ops
from kbserver.models import Job, utcnow
from kbserver.reconcile import resolve_operation, stuck_operations

from tests.conftest import auth


def _mk_job(db, user_id: str, item_id: str, state: str = "failed") -> Job:
    job = Job(
        user_id=user_id, item_id=item_id, source_revision=1, stage="enrich",
        recipe_hash="t" * 16, state=state,
    )
    db.add(job)
    db.flush()
    return job


def _mk_item(db, user_id: str, capture_id: str | None = None):
    from kbserver.models import Capture, Item

    cap = Capture(user_id=user_id, client_capture_id=capture_id, request_hash="x" * 64,
                  input_json={})
    db.add(cap)
    db.flush()
    item = Item(user_id=user_id, capture_id=cap.id, pipeline_state="failed")
    db.add(item)
    db.flush()
    return item


def test_stuck_operations_lists_terminal_and_missing_jobs(db, user_a):
    uid = user_a["user_id"]
    item = _mk_item(db, uid, "rec-stuck-1")
    job = _mk_job(db, uid, item.id, state="failed")
    op_sent = provider_ops.create_operation(
        db, user_id=uid, job_id=job.id, profile_id=None, request_fingerprint="f1")
    provider_ops.mark_sent(op_sent)
    op_prepared = provider_ops.create_operation(
        db, user_id=uid, job_id=None, profile_id=None, request_fingerprint="f2")
    # 运行中任务的操作：单独条目，避免撞 jobs 唯一约束；
    # 任务处于 running（租约未过期），不会被 claim 领取，也不污染其他测试
    item2 = _mk_item(db, uid, "rec-stuck-2")
    job_running = _mk_job(db, uid, item2.id, state="running")
    job_running.lease_until = utcnow() + timedelta(seconds=120)
    job_running.lease_token = "lease" * 4
    op_running = provider_ops.create_operation(
        db, user_id=uid, job_id=job_running.id, profile_id=None, request_fingerprint="f3")
    db.commit()

    stuck = stuck_operations(db, min_age_hours=0)
    ids = {s["operation_id"] for s in stuck}
    assert op_sent.id in ids and op_prepared.id in ids
    assert op_running.id not in ids, "运行中任务的操作由恢复路径处置，不在此列出"


def test_resolve_unknown_outcome(db, user_a):
    uid = user_a["user_id"]
    op = provider_ops.create_operation(
        db, user_id=uid, job_id=None, profile_id=None, request_fingerprint="f")
    provider_ops.mark_sent(op)
    db.commit()
    resolve_operation(db, op, "unknown_outcome")
    db.commit()
    assert op.state == "unknown_outcome"


def test_resolve_failed(db, user_a):
    uid = user_a["user_id"]
    op = provider_ops.create_operation(
        db, user_id=uid, job_id=None, profile_id=None, request_fingerprint="f")
    db.commit()
    resolve_operation(db, op, "failed")
    db.commit()
    assert op.state == "failed"


def test_prepared_cannot_be_unknown_outcome(db, user_a):
    uid = user_a["user_id"]
    op = provider_ops.create_operation(
        db, user_id=uid, job_id=None, profile_id=None, request_fingerprint="f")
    db.commit()
    with pytest.raises(ValueError):
        resolve_operation(db, op, "unknown_outcome")


def test_no_money_semantics_remain(db, user_a):
    """操作行不再携带金额字段（docs/05 §5）。"""
    uid = user_a["user_id"]
    op = provider_ops.create_operation(
        db, user_id=uid, job_id=None, profile_id=None, request_fingerprint="f")
    db.commit()
    assert not hasattr(op, "reserved_cost")
    assert not hasattr(op, "actual_usage_json")


def test_cli_reconcile_report(db, user_a, capsys):
    """CLI：报告模式列出待处置操作。"""
    from kbserver import cli

    uid = user_a["user_id"]
    op = provider_ops.create_operation(
        db, user_id=uid, job_id=None, profile_id=None, request_fingerprint="f")
    provider_ops.mark_sent(op)
    db.commit()

    args = _argparse.Namespace(command="reconcile", resolve="report", min_age_hours=0, _now=utcnow())
    cli.cmd_reconcile(args)
    out = capsys.readouterr().out
    assert op.id in out
