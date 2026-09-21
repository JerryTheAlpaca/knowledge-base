"""分享作品数据访问：用户范围查询、任务领取、版本提交与对象引用（docs/20 §11、§13.1）。

- 所有查询都带 user_id：SQLite 没有行级安全，租户隔离落在这一层；
  不存在与无权访问统一按未找到处理，不回显他人标题。
- 领取沿用 jobs 的 BEGIN IMMEDIATE + 随机租约模式，但操作 share_runs：
  waiting_user / awaiting_confirmation 不是领取候选，也不占执行并发。
- 文件先写对象存储并校验摘要，再在短事务里登记引用（与 pipeline 同一顺序）。
"""
from __future__ import annotations

import secrets
from datetime import timedelta

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..config import get_settings
from ..domain.errors import ApiError
from ..domain.sharing import ARTIFACT_ROLES as _ARTIFACT_ROLES
from ..models import (
    ProviderOperation,
    ShareArtifact,
    ShareConversation,
    ShareMessage,
    ShareRevision,
    ShareRun,
    ShareWork,
    new_id,
    utcnow,
)
from ..storage.objects import ObjectStore

# 正在执行、不占用户执行并发的等待态
WAITING_STATES = ("waiting_user", "awaiting_confirmation")
RUNNING_STATES = ("running",)
OPEN_STATES = ("queued", "retry_wait", "running", *WAITING_STATES, "waiting_resources")


# ---- 作品 ----


def get_work(db: Session, user_id: str, work_id: str) -> ShareWork | None:
    return db.scalar(
        select(ShareWork).where(ShareWork.id == work_id, ShareWork.user_id == user_id,
                                ShareWork.deleted_at.is_(None))
    )


def list_works(db: Session, user_id: str, *, limit: int = 20, offset: int = 0) -> tuple[list[ShareWork], int]:
    q = select(ShareWork).where(ShareWork.user_id == user_id, ShareWork.deleted_at.is_(None))
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(ShareWork.updated_at.desc(), ShareWork.id).limit(limit).offset(offset)).all()
    return list(rows), int(total)


def create_work(db: Session, *, user_id: str, title: str) -> ShareWork:
    work = ShareWork(user_id=user_id, title=title[:200], version=1)
    db.add(work)
    db.flush()
    return work


def bump_work_version(db: Session, work: ShareWork, *, expected: int | None) -> ShareWork:
    """乐观锁：expected 与当前版本不符时返回 409，不覆盖较新的作品状态（A22）。"""
    fresh = db.get(ShareWork, work.id)
    if fresh is None:
        raise ApiError("NOT_FOUND", "作品不存在")
    if expected is not None and fresh.version != expected:
        raise ApiError(
            "REVISION_CONFLICT",
            "这件作品刚刚被其他标签页改过，请刷新后再操作",
            details={"current_version": fresh.version, "expected_version": expected},
        )
    fresh.version = fresh.version + 1
    fresh.updated_at = utcnow()
    db.flush()
    return fresh


# ---- 任务 ----


def create_run(db: Session, *, work: ShareWork, user_id: str, request_text: str,
               base_revision_id: str | None, profile_id: str | None, profile_version: int | None,
               model_config_json: dict, runtime_version: str, prompt_version: str,
               recipe_hash: str, stage: str = "preparing") -> ShareRun:
    if work.active_run_id:
        existing = db.get(ShareRun, work.active_run_id)
        if existing is not None and existing.state in OPEN_STATES:
            raise ApiError("CONFLICT", "这件作品还有一次创作没有结束",
                           details={"run_id": existing.id, "state": existing.state})
    run = ShareRun(
        user_id=user_id, work_id=work.id, request_text=request_text[:8000],
        base_revision_id=base_revision_id, profile_id=profile_id, profile_version=profile_version,
        model_config_json=model_config_json, runtime_version=runtime_version,
        prompt_version=prompt_version, recipe_hash=recipe_hash, stage=stage,
        state="queued", not_before=utcnow(),
    )
    db.add(run)
    db.flush()
    work.active_run_id = run.id
    work.updated_at = utcnow()
    db.flush()
    return run


def get_run(db: Session, user_id: str, work_id: str, run_id: str) -> ShareRun | None:
    return db.scalar(
        select(ShareRun).where(ShareRun.id == run_id, ShareRun.user_id == user_id,
                               ShareRun.work_id == work_id)
    )


def latest_run(db: Session, user_id: str, work_id: str) -> ShareRun | None:
    """作品跑完以后 active_run_id 会清空，但那一轮对话还要能翻出来看。"""
    return db.scalar(
        select(ShareRun).where(ShareRun.user_id == user_id, ShareRun.work_id == work_id)
        .order_by(ShareRun.created_at.desc()).limit(1)
    )


def claim_run(session_factory, *, max_active_per_user: int | None = None) -> ShareRun | None:
    """原子领取一个到期的分享任务；同用户执行中的任务数受上限约束。

    等待用户回答的任务不参与领取，因此用户可以在别的作品继续工作，
    不会因为一份草稿在等回答就锁死整个账号（docs/20 §11.2）。
    """
    settings = get_settings()
    limit = settings.share_max_active_per_user if max_active_per_user is None else max_active_per_user
    now = utcnow().replace(tzinfo=None)
    lease_token = secrets.token_hex(16)
    lease_until = now + timedelta(seconds=settings.job_lease_seconds)
    with session_factory() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.execute(
            text(
                """
                UPDATE share_runs
                SET state='running', lease_token=:lt, lease_until=:lu, attempt=attempt+1,
                    heartbeat_at=:now
                WHERE id = (
                    SELECT id FROM share_runs
                    WHERE state IN ('queued','retry_wait') AND not_before <= :now
                      AND (
                        SELECT COUNT(*) FROM share_runs r2
                        WHERE r2.user_id = share_runs.user_id AND r2.state = 'running'
                      ) < :max_active
                    ORDER BY not_before
                    LIMIT 1
                )
                RETURNING id
                """
            ),
            {"lt": lease_token, "lu": lease_until, "now": now, "max_active": int(limit)},
        ).fetchone()
        db.commit()
        if row is None:
            return None
        run = db.get(ShareRun, row[0])
        if run is None:
            return None
        run.lease_token = lease_token
        db.commit()
        return run


def submit_with_lease(db: Session, run_id: str, lease_token: str) -> ShareRun | None:
    """只有持有当前租约的执行者可以提交，防止与接管者互相覆盖。"""
    run = db.get(ShareRun, run_id)
    if run is None or run.lease_token != lease_token or run.state != "running":
        return None
    return run


def heartbeat(session_factory, run_id: str, lease_token: str, *, seconds: int | None = None) -> bool:
    """长调用期间续租；返回 False 表示租约已丢失，调用方必须停止后续步骤。"""
    seconds = seconds or get_settings().job_lease_seconds
    now = utcnow()
    with session_factory() as db:
        result = db.execute(
            text("UPDATE share_runs SET lease_until=:lu, heartbeat_at=:now "
                 "WHERE id=:id AND lease_token=:lt AND state='running'"),
            {"lu": now + timedelta(seconds=seconds), "now": now, "id": run_id, "lt": lease_token},
        )
        db.commit()
        return result.rowcount > 0


def wait_for_user(db: Session, run: ShareRun, *, state: str, stage: str, reason_code: str = "") -> None:
    """持久化对话后释放执行资源：清空租约，不后台轮询模型（docs/20 §3.5）。"""
    run.state = state
    run.stage = stage
    run.reason_code = reason_code
    run.lease_token = None
    run.lease_until = None
    run.updated_at = utcnow()


def reschedule(db: Session, run: ShareRun, *, stage: str, seconds: float,
               state: str = "queued", reason_code: str = "") -> None:
    run.state = state
    run.stage = stage
    run.reason_code = reason_code
    run.not_before = utcnow() + timedelta(seconds=seconds)
    run.lease_token = None
    run.lease_until = None
    run.updated_at = utcnow()


def finish_run(db: Session, run: ShareRun, state: str, *, detail: str = "", reason_code: str = "") -> None:
    run.state = state
    run.error_detail = detail[:2000]
    run.reason_code = reason_code
    run.lease_token = None
    run.lease_until = None
    run.updated_at = utcnow()
    work = db.get(ShareWork, run.work_id)
    if work is not None and work.active_run_id == run.id and state not in OPEN_STATES:
        work.active_run_id = None
        work.updated_at = utcnow()


def retry_or_fail(db: Session, run: ShareRun, error: str, *, max_attempts: int = 5) -> None:
    if run.attempt >= max_attempts:
        finish_run(db, run, "failed", detail=error)
        return
    backoff = min(300, 2 ** max(1, run.attempt) * 5)
    run.state = "retry_wait"
    run.error_detail = error[:2000]
    run.not_before = utcnow() + timedelta(seconds=backoff)
    run.lease_token = None
    run.lease_until = None
    run.updated_at = utcnow()


def recover_expired_leases(session_factory) -> int:
    """只恢复确实处于运行中的阶段；等待用户与等待确认的任务保持原状。"""
    now = utcnow()
    with session_factory() as db:
        rows = db.scalars(
            select(ShareRun).where(ShareRun.state == "running", ShareRun.lease_until < now)
        ).all()
        for run in rows:
            run.state = "queued"
            run.lease_token = None
            run.lease_until = None
            run.updated_at = now
        db.commit()
        return len(rows)


# ---- 对象引用 ----


def register_artifact(db: Session, store: ObjectStore, *, user_id: str, work_id: str, role: str,
                      data: bytes, run_id: str | None = None, revision_id: str | None = None,
                      mime: str = "application/json", visibility: str = "private",
                      expires_at=None, storage_key: str | None = None,
                      sha256: str | None = None) -> ShareArtifact:
    """写对象存储并在同一短事务里登记引用；同一物理对象可被多条引用持有。"""
    if role not in _ARTIFACT_ROLES:
        raise ValueError(f"未知 artifact role：{role}")
    if storage_key is None or sha256 is None:
        sha256, storage_key, size = store.put_bytes(data)
    else:
        size = len(data)
    artifact = ShareArtifact(
        user_id=user_id, work_id=work_id, run_id=run_id, revision_id=revision_id,
        role=role, storage_key=storage_key, sha256=sha256, bytes=size, mime=mime,
        visibility=visibility, expires_at=expires_at,
    )
    db.add(artifact)
    db.flush()
    return artifact


def artifact_by_key(db: Session, user_id: str, work_id: str, storage_key: str) -> ShareArtifact | None:
    return db.scalar(
        select(ShareArtifact).where(ShareArtifact.user_id == user_id,
                                    ShareArtifact.work_id == work_id,
                                    ShareArtifact.storage_key == storage_key)
    )


def read_artifact(db: Session, store: ObjectStore, user_id: str, storage_key: str) -> bytes:
    return store.read_object(storage_key)


def user_storage_bytes(db: Session, user_id: str) -> int:
    return int(db.scalar(
        select(func.coalesce(func.sum(ShareArtifact.bytes), 0)).where(ShareArtifact.user_id == user_id)
    ) or 0)


# ---- 版本 ----


def list_revisions(db: Session, user_id: str, work_id: str) -> list[ShareRevision]:
    return list(db.scalars(
        select(ShareRevision).where(ShareRevision.user_id == user_id, ShareRevision.work_id == work_id)
        .order_by(ShareRevision.revision)
    ))


def get_revision(db: Session, user_id: str, work_id: str, revision: int) -> ShareRevision | None:
    return db.scalar(
        select(ShareRevision).where(ShareRevision.user_id == user_id, ShareRevision.work_id == work_id,
                                    ShareRevision.revision == revision)
    )


def allocate_revision(db: Session, work: ShareWork) -> int:
    """最终版本号在短事务中分配（docs/20 §11.3）。"""
    current = db.scalar(
        select(func.max(ShareRevision.revision)).where(ShareRevision.work_id == work.id)
    )
    return int(current or 0) + 1


# ---- 会话与消息 ----


def get_conversation(db: Session, user_id: str, conversation_id: str) -> ShareConversation | None:
    return db.scalar(
        select(ShareConversation).where(ShareConversation.id == conversation_id,
                                        ShareConversation.user_id == user_id)
    )


def create_conversation(db: Session, *, user_id: str, work_id: str, purpose: str,
                        profile_id: str | None, profile_version: int | None,
                        api_protocol: str) -> ShareConversation:
    existing = db.scalar(
        select(ShareConversation).where(ShareConversation.work_id == work_id,
                                        ShareConversation.user_id == user_id,
                                        ShareConversation.purpose == purpose)
    )
    if existing is not None:
        return existing
    conv = ShareConversation(
        user_id=user_id, work_id=work_id, purpose=purpose, profile_id=profile_id,
        profile_version=profile_version, api_protocol=api_protocol, context_epoch=1,
    )
    db.add(conv)
    db.flush()
    return conv


def list_messages(db: Session, user_id: str, conversation_id: str, *, context_epoch: int) -> list[ShareMessage]:
    return list(db.scalars(
        select(ShareMessage).where(ShareMessage.user_id == user_id,
                                   ShareMessage.conversation_id == conversation_id,
                                   ShareMessage.context_epoch == context_epoch)
        .order_by(ShareMessage.seq)
    ))


def append_message(db: Session, store: ObjectStore, *, conversation: ShareConversation, run_id: str,
                   user_id: str, work_id: str, role: str, content: bytes,
                   expected_version: int | None = None, reply_to_round_id: str | None = None,
                   protocol_metadata: dict | None = None) -> ShareMessage:
    """原子追加一条消息：先写对象再落消息行，seq 由会话计数器推进。

    expected_version 是客户端乐观锁：两个标签页回答同一轮时旧提交被拒（A36）。
    """
    if expected_version is not None and conversation.version != expected_version:
        raise ApiError("REVISION_CONFLICT", "对话已被更新，请刷新后重新提交",
                       details={"current_version": conversation.version})
    artifact = register_artifact(
        db, store, user_id=user_id, work_id=work_id, role="conversation_message",
        data=content, run_id=run_id, mime="application/json",
    )
    message = ShareMessage(
        user_id=user_id, conversation_id=conversation.id, run_id=run_id,
        context_epoch=conversation.context_epoch, seq=conversation.last_message_seq + 1,
        role=role, content_key=artifact.storage_key, reply_to_round_id=reply_to_round_id,
        protocol_metadata_json=protocol_metadata or {},
    )
    conversation.last_message_seq = conversation.last_message_seq + 1
    conversation.version = conversation.version + 1
    conversation.updated_at = utcnow()
    db.add(message)
    db.flush()
    return message


# ---- 模型调用归属 ----


def latest_operation(db: Session, run_id: str, step_key: str) -> ProviderOperation | None:
    return db.scalar(
        select(ProviderOperation)
        .where(ProviderOperation.share_run_id == run_id, ProviderOperation.step_key == step_key)
        .order_by(ProviderOperation.created_at.desc(), ProviderOperation.id)
    )


def create_operation(db: Session, *, user_id: str, run_id: str, step_key: str, profile_id: str | None,
                     request_fingerprint: str, conversation_id: str | None = None,
                     context_epoch: int | None = None, input_message_seq: int | None = None,
                     prefix_hash: str = "") -> ProviderOperation:
    op = ProviderOperation(
        id=new_id(), user_id=user_id, job_id=None, share_run_id=run_id, step_key=step_key[:40],
        profile_id=profile_id, request_fingerprint=request_fingerprint[:64], state="prepared",
        conversation_id=conversation_id, context_epoch=context_epoch,
        input_message_seq=input_message_seq, prefix_hash=prefix_hash[:64],
    )
    db.add(op)
    db.flush()
    return op


def operations_for_run(db: Session, run_id: str) -> list[ProviderOperation]:
    return list(db.scalars(
        select(ProviderOperation).where(ProviderOperation.share_run_id == run_id)
        .order_by(ProviderOperation.created_at)
    ))
