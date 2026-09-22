"""分享作品的私有接口（docs/20 §12）。

- 身份、CSRF 与幂等沿用现有约定；新增 shares:read / shares:write 两个 Web 权限，
  不自动扩大旧设备 Token 的权限。
- 作品只允许一轮活跃生成；版本基线变了返回 409 与当前版本摘要，不覆盖。
- 不存在与无权访问统一 404 语义，不回显他人标题。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Header, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..domain import pipeline, share_conversations, sharing
from ..domain.errors import ApiError
from ..models import (
    Item,
    ProviderProfile,
    ShareArtifact,
    ShareConversation,
    ShareMessage,
    ShareRevision,
    ShareRun,
    ShareWork,
    SourceRevision,
    utcnow,
)
from ..repositories import shares as repo
from ..repositories import core as repo_core
from ..security import share_tokens
from ..api.deps import require_scope
from ..storage.objects import ObjectStore
from ..workers import share as share_worker

router = APIRouter(prefix="/v1/shares", tags=["shares"])

STATUS_FIELDS = ("state", "stage", "reason_code", "error_detail")


class CreateShare(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_ids: list[str] = Field(min_length=1, max_length=40)
    instructions: str = Field(default="", max_length=8000)
    profile_id: str | None = None


class AnswerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_conversation_version: int
    round_id: str
    answers: list[dict] = Field(default_factory=list, max_length=6)
    message: str = Field(default="", max_length=4000)


class StartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_conversation_version: int | None = None
    expected_brief_version: int | None = None
    mode: str = Field(default="confirm")


class ModifyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_revision_id: str
    instructions: str = Field(default="", max_length=8000)
    expected_work_version: int | None = None
    profile_id: str | None = None


class PublishBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision_id: str
    expected_work_version: int | None = None
    expires_at: datetime | None = None


# ---- 内部装配 ----


def _run_out(run: ShareRun | None) -> dict | None:
    if run is None:
        return None
    return {
        "run_id": run.id,
        "state": run.state,
        "stage": run.stage,
        "status_text": sharing.status_text(run.state, run.stage),
        "reason_code": run.reason_code,
        "reason_text": sharing.reason_text(run.reason_code),
        # 笼统提示之外把实际原因带给界面，否则用户只看到「没有通过检查」
        "error_detail": (run.error_detail or "")[:200] or None,
        "brief_version": run.brief_version,
        "confirmed_brief_version": run.confirmed_brief_version,
        "pending_round_id": run.pending_round_id,
        "repair_count": run.repair_count,
        "attempt": run.attempt,
        "created_at": run.created_at.isoformat(),
    }


def _actions(db: Session, work: ShareWork, run: ShareRun | None) -> dict:
    if run is None:
        return {"can_answer": False, "can_start": False, "can_retry": False,
                "can_cancel": False, "can_modify": bool(work.latest_ready_revision_id),
                "can_publish": bool(work.latest_ready_revision_id)}
    waiting = run.state in ("waiting_user", "awaiting_confirmation")
    terminal = run.state in ("failed", "cancelled", "succeeded", "unknown_outcome")
    return {
        "can_answer": waiting,
        "can_start": waiting and run.brief_version > 0,
        "can_retry": terminal,
        "can_cancel": not terminal,
        "can_modify": work.latest_ready_revision_id is not None,
        "can_publish": work.latest_ready_revision_id is not None,
    }


def _brief_view(run: ShareRun | None) -> dict | None:
    if run is None or not run.brief_key:
        return None
    doc = json.loads(ObjectStore().read_object(run.brief_key).decode("utf-8"))
    return {
        "version": doc.get("version"),
        "fields": share_conversations.render_brief_lines(doc.get("brief") or {},
                                                        doc.get("provenance") or {}),
        "confirmed_version": run.confirmed_brief_version,
    }


def _round_view(run: ShareRun | None) -> dict | None:
    if run is None or not run.pending_round_key:
        return None
    doc = json.loads(ObjectStore().read_object(run.pending_round_key).decode("utf-8"))
    return {
        "round_id": doc.get("round_id"),
        "understanding": doc.get("understanding"),
        "questions": doc.get("questions") or [],
        "next_action": doc.get("next_action"),
    }


def _get_work(db: Session, user_id: str, work_id: str) -> ShareWork:
    work = repo.get_work(db, user_id, work_id)
    if work is None:
        raise ApiError("NOT_FOUND", "作品不存在")
    return work


def _get_run(db: Session, user_id: str, work_id: str, run_id: str) -> ShareRun:
    run = repo.get_run(db, user_id, work_id, run_id)
    if run is None:
        raise ApiError("NOT_FOUND", "任务不存在")
    return run


def _require_idempotency(endpoint: str, key: str | None) -> str:
    if not key:
        raise ApiError("SCHEMA_INVALID", "缺少 Idempotency-Key 请求头", status_code=422)
    return key


def _idempotent(db: Session, user_id: str, endpoint: str, key: str | None, payload: dict,
                build) -> dict:
    """同 key 同请求返回原响应；同 key 不同请求 409（§12.1）。"""
    _require_idempotency(endpoint, key)
    request_hash = pipeline.sha256_hex(pipeline.canonical_json(payload))
    existing = repo_core.idempotency_lookup(db, user_id, endpoint, key or "")
    if existing:
        if existing.request_hash != request_hash:
            raise ApiError("IDEMPOTENCY_CONFLICT", "同一幂等键对应不同请求内容", status_code=409)
        return existing.response_json
    response = build()
    repo_core.idempotency_save(db, user_id, endpoint, key or "", request_hash, response, 202)
    return response


def _check_items_readable(db: Session, user_id: str, item_ids: list[str], settings) -> list[dict]:
    """材料可用性在选择阶段就说清：只有链接/标题/图片不算可读正文（§3.1）。"""
    if len(item_ids) > settings.share_max_items:
        raise ApiError("SCHEMA_INVALID", f"一次最多选择 {settings.share_max_items} 篇材料")
    specs, unreadable = [], []
    store = ObjectStore()
    for item_id in dict.fromkeys(item_ids):
        item = repo_core.get_item(db, user_id, item_id)
        if item is None or item.deleted_at is not None:
            raise ApiError("NOT_FOUND", "选中的材料不存在或没有权限")
        source = db.query(SourceRevision).filter(
            SourceRevision.item_id == item.id,
            SourceRevision.revision == item.source_revision).one_or_none()
        # 与 worker 取正文同一套读法：segments.json 同路径会有多份登记（编辑原文、
        # 补充材料、重新提取都会新登记一份），要按最新那份核对当前来源版本。
        # 取最早那份会让改过正文的材料永远被判成不可读。
        if source is None or not share_worker._segments_of(db, store, item):
            meta = (source.metadata_json or {}) if source else {}
            unreadable.append({
                "item_id": item.id,
                "title": meta.get("title") or "未命名材料",
                "reason": "no_text",
            })
            continue
        specs.append({"item_id": item.id, "source_revision": item.source_revision})
    if unreadable:
        raise ApiError(
            "SCHEMA_INVALID",
            "有材料还没有可读正文，请先补充材料或取消选择",
            details={"unreadable_items": unreadable},
        )
    if not specs:
        raise ApiError("SCHEMA_INVALID", "至少选择一篇有可读正文的材料")
    return specs


def _capacity_check(db: Session, user_id: str, settings) -> None:
    queued = db.query(ShareRun).filter(
        ShareRun.user_id == user_id, ShareRun.state.in_(("queued", "retry_wait", "running"))
    ).count()
    if queued >= settings.share_max_queued_per_user:
        raise ApiError("CONFLICT", f"同时进行的创作最多 {settings.share_max_queued_per_user} 个",
                       status_code=409)
    waiting = db.query(ShareRun).filter(
        ShareRun.user_id == user_id,
        ShareRun.state.in_(repo.WAITING_STATES)).count()
    if waiting >= settings.share_max_waiting_drafts_per_user:
        raise ApiError("CONFLICT",
                       f"等待你确认的草稿最多 {settings.share_max_waiting_drafts_per_user} 份，"
                       f"请先处理或删除", status_code=409)
    if repo.user_storage_bytes(db, user_id) >= settings.share_max_storage_bytes_per_user:
        raise ApiError("STORAGE_QUOTA_EXCEEDED", "私有作品存储已达到容量上限")


# ---- 作品 ----


@router.post("", status_code=202)
def create_share(
    body: CreateShare,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal=Depends(require_scope("shares:write")),
    db: Session = Depends(get_db),
) -> dict:
    settings = get_settings()
    user = principal.user
    if not settings.share_enabled:
        raise ApiError("CONFLICT", "分享创作还没有启用", status_code=409,
                       details={"reason": "share_disabled"})
    if len(body.instructions) > settings.share_max_instructions_chars:
        raise ApiError("SCHEMA_INVALID",
                       f"创作要求最多 {settings.share_max_instructions_chars} 字符")
    _capacity_check(db, user.id, settings)
    specs = _check_items_readable(db, user.id, body.item_ids, settings)
    profile = None
    if body.profile_id:
        profile = db.get(ProviderProfile, body.profile_id)
        if profile is None or profile.user_id != user.id or profile.kind != "llm":
            raise ApiError("NOT_FOUND", "模型配置不存在")

    payload = body.model_dump(mode="json")

    def build():
        work = repo.create_work(db, user_id=user.id, title="")
        run = repo.create_run(
            db, work=work, user_id=user.id, request_text=body.instructions,
            base_revision_id=None, profile_id=profile.id if profile else None,
            profile_version=profile.version if profile else None,
            model_config_json={}, runtime_version="",
            prompt_version=sharing.PROMPT_VERSION, recipe_hash="", stage="preparing",
        )
        run.checkpoint_json = {"items": specs}
        db.commit()
        return {
            "share_id": work.id, "run_id": run.id, "state": "queued",
            "status_text": sharing.status_text("queued"), "version": work.version,
        }

    return _idempotent(db, user.id, "POST /v1/shares", idempotency_key, payload, build)


@router.get("")
def list_shares(limit: int = 20, offset: int = 0,
                principal=Depends(require_scope("shares:read")),
                db: Session = Depends(get_db)) -> dict:
    limit = max(1, min(limit, 100))
    works, total = repo.list_works(db, principal.user.id, limit=limit, offset=offset)
    items = []
    for work in works:
        run = db.get(ShareRun, work.active_run_id) if work.active_run_id else None
        revision = db.get(ShareRevision, work.latest_ready_revision_id) \
            if work.latest_ready_revision_id else None
        items.append({
            "share_id": work.id,
            "title": work.title or "未命名作品",
            "updated_at": work.updated_at.isoformat(),
            "version": work.version,
            "state": (run.state if run else ("ready" if revision else "idle")),
            "status_text": sharing.status_text(run.state, run.stage) if run
            else ("可以预览" if revision else "尚未开始"),
            "latest_revision": revision.revision if revision else None,
            "share_status": work.share_status,
        })
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/{work_id}")
def get_share(work_id: str, principal=Depends(require_scope("shares:read")),
              db: Session = Depends(get_db)) -> dict:
    work = _get_work(db, principal.user.id, work_id)
    # 跑完的一轮会从 active_run_id 上摘下来，但对话记录还得能翻出来看、失败也要能重试
    run = db.get(ShareRun, work.active_run_id) if work.active_run_id else None
    if run is None:
        run = repo.latest_run(db, principal.user.id, work.id)
    revisions = repo.list_revisions(db, principal.user.id, work.id)
    published = db.get(ShareRevision, work.published_revision_id) if work.published_revision_id else None
    return {
        "share_id": work.id,
        "title": work.title or "未命名作品",
        "version": work.version,
        "updated_at": work.updated_at.isoformat(),
        "run": _run_out(run),
        "brief": _brief_view(run),
        "round": _round_view(run) if run and run.state in ("waiting_user", "awaiting_confirmation")
        else None,
        "actions": _actions(db, work, run),
        "revisions": [{
            "revision_id": r.id, "revision": r.revision, "created_at": r.created_at.isoformat(),
            "bytes": r.html_bytes,
            "is_latest": r.id == work.latest_ready_revision_id,
            "is_published": published is not None and r.id == published.id,
        } for r in revisions],
        "share": {
            "status": work.share_status,
            "has_link": work.share_token_hash is not None and work.share_status == "published",
            "expires_at": work.share_expires_at.isoformat() if work.share_expires_at else None,
            "public_base_configured": bool(get_settings().share_public_base_url),
        },
    }


@router.post("/{work_id}/runs", status_code=202)
def create_modify_run(
    work_id: str,
    body: ModifyBody,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal=Depends(require_scope("shares:write")),
    db: Session = Depends(get_db),
) -> dict:
    settings = get_settings()
    user = principal.user
    work = _get_work(db, user.id, work_id)
    base = db.get(ShareRevision, body.base_revision_id)
    if base is None or base.work_id != work.id:
        raise ApiError("NOT_FOUND", "要修改的版本不存在")
    if work.active_run_id and db.get(ShareRun, work.active_run_id) is not None:
        active = db.get(ShareRun, work.active_run_id)
        if active.state in repo.OPEN_STATES:
            raise ApiError("CONFLICT", "这件作品还有一次创作没有结束", status_code=409,
                           details={"run_id": active.id, "state": active.state})
    _capacity_check(db, user.id, settings)
    repo.bump_work_version(db, work, expected=body.expected_work_version)
    payload = body.model_dump(mode="json")

    def build():
        run = repo.create_run(
            db, work=work, user_id=user.id, request_text=body.instructions,
            base_revision_id=base.id, profile_id=None, profile_version=None,
            model_config_json={}, runtime_version="", prompt_version=sharing.PROMPT_VERSION,
            recipe_hash="", stage="preparing",
        )
        manifest = json.loads(ObjectStore().read_object(base.input_manifest_key).decode("utf-8"))
        run.checkpoint_json = {
            "items": [{"item_id": s["item_id"], "source_revision": s["source_revision"]}
                      for s in manifest.get("sources") or []],
            "base_revision": base.revision,
        }
        run.input_manifest_key = base.input_manifest_key
        run.input_hash = base.html_sha256 and run.input_hash
        run.checkpoint_json["synthesis_key"] = base.synthesis_key
        run.checkpoint_json["prior_page_source_key"] = base.source_code_key
        db.commit()
        return {"share_id": work.id, "run_id": run.id, "state": "queued",
                "status_text": sharing.status_text("queued"), "version": work.version}

    return _idempotent(db, user.id, f"POST /v1/shares/{work_id}/runs", idempotency_key,
                       payload, build)


# ---- 需求对话 ----


@router.get("/{work_id}/runs/{run_id}/conversation")
def get_conversation(work_id: str, run_id: str, limit: int = 50, before_seq: int | None = None,
                     principal=Depends(require_scope("shares:read")),
                     db: Session = Depends(get_db)) -> dict:
    user = principal.user
    work = _get_work(db, user.id, work_id)
    run = _get_run(db, user.id, work_id, run_id)
    conv = db.query(ShareConversation).filter_by(work_id=work.id, purpose="content").first()
    if conv is None:
        return {"messages": [], "conversation_version": 0, "round": _round_view(run),
                "brief": _brief_view(run)}
    store = ObjectStore()
    rows = db.query(ShareMessage).filter(
        ShareMessage.user_id == user.id, ShareMessage.conversation_id == conv.id,
        ShareMessage.context_epoch == conv.context_epoch).order_by(ShareMessage.seq.desc()).limit(
        max(1, min(limit, 100))).all()
    messages = []
    for msg in reversed(rows):
        try:
            content = json.loads(store.read_object(msg.content_key).decode("utf-8"))
        except Exception:
            continue
        if msg.role == "assistant":
            messages.append({"seq": msg.seq, "role": "assistant", "round_id": msg.reply_to_round_id,
                             "understanding": content.get("understanding"),
                             "questions": content.get("questions") or [],
                             "next_action": content.get("next_action")})
        else:
            # text 是选项与补充拼出来的稳定回放文本；界面只要用户自己打的那句，
            # 选项已经折在对应问题组里，两处一起画就把同一句话摆了两遍
            messages.append({"seq": msg.seq, "role": "user", "text": content.get("text"),
                             "answers": content.get("answers") or [],
                             "free_text": content.get("free_text"),
                             "round_id": msg.reply_to_round_id})
    return {
        "messages": messages,
        "conversation_version": conv.version,
        "context_epoch": conv.context_epoch,
        "next_before_seq": rows[-1].seq if rows else None,
        "round": _round_view(run),
        "brief": _brief_view(run),
    }


@router.post("/{work_id}/runs/{run_id}/messages", status_code=202)
def post_answer(
    work_id: str,
    run_id: str,
    body: AnswerBody,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal=Depends(require_scope("shares:write")),
    db: Session = Depends(get_db),
) -> dict:
    user = principal.user
    work = _get_work(db, user.id, work_id)
    run = _get_run(db, user.id, work_id, run_id)
    # 确认阶段同样收自由补充：页面只有一个输入框，「再调整一下」就是一句普通的话，
    # 收进来后回到 clarifying，AI 改完需求会再确认一次。
    if run.state not in ("waiting_user", "awaiting_confirmation"):
        raise ApiError("CONFLICT", "现在没有在等待你的回答", status_code=409,
                       details={"state": run.state})
    if not body.answers and not (body.message or "").strip():
        raise ApiError("SCHEMA_INVALID", "还没有要发送的内容")
    if run.pending_round_id != body.round_id:
        raise ApiError("REVISION_CONFLICT", "这组问题已经更新，请刷新后重新提交", status_code=409,
                       details={"current_round_id": run.pending_round_id})
    conv = db.query(ShareConversation).filter_by(work_id=work.id, purpose="content").first()
    if conv is None:
        raise ApiError("CONFLICT", "需求对话还没有开始", status_code=409)
    round_doc = json.loads(ObjectStore().read_object(run.pending_round_key).decode("utf-8"))
    _validate_answers_belong_to_round(round_doc, body.answers)
    payload = body.model_dump(mode="json")

    def build():
        store = ObjectStore()
        text = share_conversations.normalize_answer_text(round_doc, body.answers, body.message)
        content = {"text": text, "answers": body.answers, "free_text": body.message}
        message = repo.append_message(
            db, store, conversation=conv, run_id=run.id, user_id=user.id, work_id=work.id,
            role="user", content=json.dumps(content, ensure_ascii=False).encode("utf-8"),
            expected_version=body.expected_conversation_version,
            reply_to_round_id=body.round_id,
        )
        repo.reschedule(db, run, stage="clarifying", seconds=0)
        db.commit()
        return {"run_id": run.id, "state": "queued", "message_seq": message.seq,
                "status_text": sharing.status_text("queued", "clarifying")}

    return _idempotent(db, user.id, f"POST /v1/shares/{work_id}/runs/{run_id}/messages",
                       idempotency_key, payload, build)


def _validate_answers_belong_to_round(round_doc: dict, answers: list[dict]) -> None:
    known = {q.get("id") for q in round_doc.get("questions") or []}
    options = {q.get("id"): {o.get("id") for o in q.get("options") or []}
               for q in round_doc.get("questions") or []}
    for answer in answers:
        qid = answer.get("question_id")
        if qid not in known:
            raise ApiError("SCHEMA_INVALID", "回答的问题不属于当前轮次")
        for oid in answer.get("option_ids") or []:
            if oid not in options.get(qid, set()):
                raise ApiError("SCHEMA_INVALID", "选项不属于当前问题")


@router.post("/{work_id}/runs/{run_id}/start", status_code=202)
def start_generation(
    work_id: str,
    run_id: str,
    body: StartBody,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal=Depends(require_scope("shares:write")),
    db: Session = Depends(get_db),
) -> dict:
    user = principal.user
    work = _get_work(db, user.id, work_id)
    run = _get_run(db, user.id, work_id, run_id)
    if run.state not in ("waiting_user", "awaiting_confirmation"):
        raise ApiError("CONFLICT", "现在不能开始生成", status_code=409, details={"state": run.state})
    if body.mode not in ("confirm", "delegate_preferences"):
        raise ApiError("SCHEMA_INVALID", "mode 只允许 confirm／delegate_preferences")
    if not run.brief_key or run.brief_version <= 0:
        raise ApiError("CONFLICT", "需求摘要还在整理中，稍等一下再开始", status_code=409,
                       details={"reason": "brief_processing"})
    conv = db.query(ShareConversation).filter_by(work_id=work.id, purpose="content").first()
    if body.expected_conversation_version is not None and conv is not None \
            and conv.version != body.expected_conversation_version:
        raise ApiError("REVISION_CONFLICT", "对话刚刚被更新，请按最新摘要确认", status_code=409,
                       details={"current_version": conv.version})
    if body.expected_brief_version is not None and body.expected_brief_version != run.brief_version:
        raise ApiError("REVISION_CONFLICT", "需求摘要已更新，请重新确认", status_code=409,
                       details={"current_brief_version": run.brief_version})
    round_doc = json.loads(ObjectStore().read_object(run.pending_round_key).decode("utf-8")) \
        if run.pending_round_key else {}
    blocking = share_conversations.blocking_questions(round_doc)
    if blocking:
        raise ApiError(
            "SCHEMA_INVALID", "还有必须先确认的问题，回答后才能开始生成", status_code=422,
            details={"blocking_questions": [q.get("id") for q in blocking]},
        )
    payload = body.model_dump(mode="json")

    def build():
        store = ObjectStore()
        brief_doc = json.loads(store.read_object(run.brief_key).decode("utf-8"))
        brief, provenance = brief_doc.get("brief") or {}, brief_doc.get("provenance") or {}
        if body.mode == "delegate_preferences":
            brief, provenance = share_conversations.delegate_assumptions(brief, provenance)
            run.brief_version = run.brief_version + 1
            run.brief_key = repo.register_artifact(
                db, store, user_id=user.id, work_id=work.id, run_id=run.id, role="brief",
                data=pipeline.canonical_json(
                    {"version": run.brief_version, "brief": brief, "provenance": provenance}),
            ).storage_key
        run.confirmed_brief_version = run.brief_version
        run.confirmation_kind = body.mode
        run.confirmation_message_id = run.pending_round_id
        repo.reschedule(db, run, stage="synthesizing", seconds=0)
        db.commit()
        return {"run_id": run.id, "state": "queued",
                "confirmed_brief_version": run.confirmed_brief_version,
                "status_text": sharing.status_text("queued", "synthesizing")}

    return _idempotent(db, user.id, f"POST /v1/shares/{work_id}/runs/{run_id}/start",
                       idempotency_key, payload, build)


@router.post("/{work_id}/runs/{run_id}/retry", status_code=202)
def retry_run(work_id: str, run_id: str,
              idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
              principal=Depends(require_scope("shares:write")),
              db: Session = Depends(get_db)) -> dict:
    user = principal.user
    work = _get_work(db, user.id, work_id)
    old = _get_run(db, user.id, work_id, run_id)
    if old.state not in ("failed", "unknown_outcome", "cancelled"):
        raise ApiError("CONFLICT", "这次任务还没有结束，不需要重试", status_code=409,
                       details={"state": old.state})
    _capacity_check(db, user.id, get_settings())
    payload = {"run_id": run_id}

    def build():
        # 复用固定输入：新 run 继承已钉死的材料清单，不重新读「最新版」
        run = repo.create_run(
            db, work=work, user_id=user.id, request_text=old.request_text,
            base_revision_id=old.base_revision_id, profile_id=old.profile_id,
            profile_version=old.profile_version, model_config_json=old.model_config_json,
            runtime_version="", prompt_version=sharing.PROMPT_VERSION,
            recipe_hash=old.recipe_hash, stage="preparing",
        )
        run.checkpoint_json = {"items": (old.checkpoint_json or {}).get("items") or []}
        if not run.checkpoint_json["items"]:
            run.checkpoint_json = dict(old.checkpoint_json or {})
            run.checkpoint_json.pop("runner_task", None)
            run.checkpoint_json.pop("runner_deadline", None)
        db.commit()
        return {"run_id": run.id, "state": "queued",
                "status_text": sharing.status_text("queued"), "share_id": work.id}

    return _idempotent(db, user.id, f"POST /v1/shares/{work_id}/runs/{run_id}/retry",
                       idempotency_key, payload, build)


@router.post("/{work_id}/runs/{run_id}/cancel")
def cancel_run(work_id: str, run_id: str,
               principal=Depends(require_scope("shares:write")),
               db: Session = Depends(get_db)) -> dict:
    work = _get_work(db, principal.user.id, work_id)
    run = _get_run(db, principal.user.id, work_id, run_id)
    if run.state in ("succeeded", "failed", "cancelled"):
        return {"run_id": run.id, "state": run.state}
    run.cancel_requested_at = utcnow()
    if run.state in ("queued", "retry_wait") or run.state in repo.WAITING_STATES:
        repo.finish_run(db, run, "cancelled", detail="用户停止了本次生成")
    db.commit()
    return {"run_id": run.id, "state": run.state,
            "note": "已停止后续步骤；已经发出的模型调用不能撤回，仍会按供应商规则计费"}


# ---- 预览、下载、发布 ----


@router.post("/{work_id}/revisions/{revision}/preview-token")
def preview_token(work_id: str, revision: int,
                  principal=Depends(require_scope("shares:read")),
                  db: Session = Depends(get_db)) -> dict:
    settings = get_settings()
    user = principal.user
    work = _get_work(db, user.id, work_id)
    row = repo.get_revision(db, user.id, work.id, revision)
    if row is None:
        raise ApiError("NOT_FOUND", "版本不存在")
    token, expires_at = share_tokens.issue_preview_token(
        settings.load_master_key(), work_id=work.id, revision_id=row.id,
        ttl_seconds=settings.share_preview_ttl_seconds)
    base = settings.share_public_base_url.rstrip("/")
    body = {"preview_token": token, "expires_at": datetime.fromtimestamp(expires_at).isoformat(),
            "revision": row.revision}
    if base:
        body["url"] = f"{base}/preview/{token}"
    else:
        body["preview_path"] = f"/v1/shares/{work.id}/revisions/{row.revision}/preview-content"
    return body


@router.get("/{work_id}/revisions/{revision}/preview-content")
def preview_content(work_id: str, revision: int, response: Response,
                    principal=Depends(require_scope("shares:read")),
                    db: Session = Depends(get_db)) -> dict:
    """未配置独立分享站点时的私有预览：返回数据 JSON，由前端可信容器设置 srcdoc。

    这里不直接返回可执行 HTML 页面，避免把主站变成任意 HTML 执行入口。
    """
    user = principal.user
    work = _get_work(db, user.id, work_id)
    row = repo.get_revision(db, user.id, work.id, revision)
    if row is None:
        raise ApiError("NOT_FOUND", "版本不存在")
    html = ObjectStore().read_object(row.html_key).decode("utf-8")
    response.headers["Cache-Control"] = "no-store"
    return {"share_id": work.id, "revision": row.revision, "html_sha256": row.html_sha256,
            "bytes": row.html_bytes, "document": html}


@router.get("/{work_id}/revisions/{revision}/download")
def download(work_id: str, revision: int, response: Response,
             principal=Depends(require_scope("shares:read")),
             db: Session = Depends(get_db)):
    from fastapi.responses import Response as FastResponse

    user = principal.user
    work = _get_work(db, user.id, work_id)
    row = repo.get_revision(db, user.id, work.id, revision)
    if row is None:
        raise ApiError("NOT_FOUND", "版本不存在")
    store = ObjectStore()
    data = store.read_object(row.html_key)
    if pipeline.sha256_hex(data) != row.html_sha256:
        raise ApiError("CONFLICT", "成品摘要与记录不一致，已停止下载")
    title = (work.title or "share-page").replace('"', "")[:60]
    response.headers["Cache-Control"] = "no-store"
    return FastResponse(content=data, media_type="text/html; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="share-r{row.revision}.html"'})


@router.post("/{work_id}/publish")
def publish(work_id: str, body: PublishBody,
            idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
            principal=Depends(require_scope("shares:write")),
            db: Session = Depends(get_db)) -> dict:
    settings = get_settings()
    user = principal.user
    work = _get_work(db, user.id, work_id)
    if not settings.share_enabled:
        raise ApiError("CONFLICT", "分享创作还没有启用", status_code=409)
    if not settings.share_public_base_url:
        raise ApiError(
            "CONFLICT",
            "还没有配置独立的分享站点，暂时不能生成公开链接；私有预览和下载不受影响",
            status_code=409, details={"reason": "share_site_not_configured"},
        )
    row = db.get(ShareRevision, body.revision_id)
    if row is None or row.work_id != work.id:
        raise ApiError("NOT_FOUND", "版本不存在")
    data = ObjectStore().read_object(row.html_key)
    if pipeline.sha256_hex(data) != row.html_sha256:
        raise ApiError("CONFLICT", "成品对象与记录不一致，不能发布")
    payload = body.model_dump(mode="json")

    def build():
        repo.bump_work_version(db, work, expected=body.expected_work_version)
        token = share_tokens.new_share_token()
        envelope = share_tokens.encrypt_share_token(token, settings.load_master_key(),
                                                    user_id=user.id, work_id=work.id)
        # 撤销后再次发布生成新令牌，旧链接继续不可用
        work.share_token_hash = share_tokens.hash_share_token(token)
        work.share_token_ciphertext = envelope["ciphertext"]
        work.share_token_dek = envelope["dek"]
        work.share_token_nonces_json = envelope["nonces"]
        work.share_master_key_version = settings.master_key_version
        work.published_revision_id = row.id
        work.share_status = "published"
        work.share_expires_at = body.expires_at
        db.commit()
        return {"share_id": work.id, "url": f"{settings.share_public_base_url.rstrip('/')}"
                                           f"/s/{token}", "revision": row.revision,
                "version": work.version}

    return _idempotent(db, user.id, f"POST /v1/shares/{work_id}/publish", idempotency_key,
                       payload, build)


@router.get("/{work_id}/link")
def read_link(work_id: str, response: Response,
              principal=Depends(require_scope("shares:read")),
              db: Session = Depends(get_db)) -> dict:
    settings = get_settings()
    user = principal.user
    work = _get_work(db, user.id, work_id)
    response.headers["Cache-Control"] = "no-store"
    if work.share_status != "published" or not work.share_token_ciphertext:
        return {"status": work.share_status, "url": None}
    token = share_tokens.decrypt_share_token(
        work.share_token_ciphertext, work.share_token_dek or "", work.share_token_nonces_json or {},
        settings.load_master_key(), user_id=user.id, work_id=work.id)
    return {"status": work.share_status,
            "url": f"{settings.share_public_base_url.rstrip('/')}/s/{token}",
            "expires_at": work.share_expires_at.isoformat() if work.share_expires_at else None}


@router.post("/{work_id}/revoke")
def revoke(work_id: str, body: dict | None = None,
           principal=Depends(require_scope("shares:write")),
           db: Session = Depends(get_db)) -> dict:
    work = _get_work(db, principal.user.id, work_id)
    if work.share_status != "published":
        return {"share_id": work.id, "status": work.share_status}
    work.share_status = "revoked"
    work.share_token_hash = None
    work.share_token_ciphertext = None
    work.share_token_dek = None
    work.share_token_nonces_json = {}
    work.version = work.version + 1
    work.updated_at = utcnow()
    db.commit()
    return {"share_id": work.id, "status": "revoked",
            "note": "服务器不再通过旧链接交付内容；已打开或已下载的内容无法收回"}


@router.delete("/{work_id}")
def delete_work(work_id: str, expected_version: int | None = None,
                principal=Depends(require_scope("shares:write")),
                db: Session = Depends(get_db)) -> dict:
    user = principal.user
    work = _get_work(db, user.id, work_id)
    # expected_version 是删除的乐观锁：不带就按现在的宽容行为删（前端当前不传），
    # 带了就必须对得上，否则旧标签页会把别人已经改过的作品静默删掉（审查 C-12）。
    if expected_version is not None and work.version != expected_version:
        raise ApiError(
            "REVISION_CONFLICT",
            "这件作品刚刚被其他标签页改过，请刷新后再删除",
            details={"current_version": work.version, "expected_version": expected_version},
        )
    if work.active_run_id:
        run = db.get(ShareRun, work.active_run_id)
        if run is not None and run.state in repo.OPEN_STATES:
            run.cancel_requested_at = utcnow()
            repo.finish_run(db, run, "cancelled", detail="作品已删除")
    work.deleted_at = utcnow()
    work.share_status = "revoked"
    work.share_token_hash = None
    work.share_token_ciphertext = None
    work.version = work.version + 1
    # 对象引用保留到期，由清理任务按存活引用回收（不在此处物理删除）；
    # run/会话/消息/版本这些文本行同样留给清理任务在宽限期后回收，不在请求里同步删
    for artifact in db.query(ShareArtifact).filter_by(
            work_id=work.id, user_id=user.id).all():
        artifact.expires_at = utcnow() + timedelta(days=1)
    db.commit()
    return {"share_id": work.id, "deleted": True}
