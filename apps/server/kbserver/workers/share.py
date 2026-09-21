"""分享编排 Worker：状态机、模型调用、检查点与打包交接（docs/20 §4、§5、§6、§13）。

边界：
- 本进程有数据库与当前用户模型凭据访问权；不执行模型生成的程序。
- 页面构建与浏览器检查在独立的 share_runner 容器里做，两边只通过 SHARE_SPOOL_DIR
  按任务目录交接；等待 runner 时释放租约重新排队，不占着执行槽干等。
- 等待用户回答或确认时同样持久化后释放租约；不后台轮询模型，也不因久未回复自动推进。

阶段：preparing → clarifying →（等待用户／等待确认）→ synthesizing → generating
      → packaging →（等待 runner／repairing）→ 可用草稿
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import make_engine, make_session_factory
from ..domain import provider_ops, share_prompts, sharing
from ..domain.share_conversations import merge_brief, prefix_hash, request_messages, stable_prefix
from ..models import (
    Credential,
    Item,
    ProviderOperation,
    ProviderProfile,
    ShareArtifact,
    ShareConversation,
    ShareRevision,
    ShareRun,
    ShareWork,
    SourceRevision,
    StoredFile,
    User,
    utcnow,
)
from ..providers.llm import (
    ConversationMessage,
    ConversationRequest,
    OpenAICompatibleProvider,
    ProviderAuthFailed,
    ProviderInvalidRequest,
    ProviderOutcomeUnknown,
    ProviderRetryable,
    parse_model_json,
)
from ..repositories import shares as repo
from ..security import credentials as cred_crypto
from ..storage.objects import ObjectStore


class ShareInputError(Exception):
    """输入不可用或超出边界：如实失败并保留已有版本，不猜、不补。"""

    def __init__(self, reason_code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message[:500]
        self.details = details or {}


class OutputInvalid(Exception):
    def __init__(self, kind: str, errors: list[str], raw: str = ""):
        super().__init__("；".join(errors[:5]))
        self.kind = kind
        self.errors = errors[:40]
        self.raw = raw[:20000]


@dataclass
class SharePlan:
    run_id: str
    lease_token: str
    user_id: str
    work_id: str
    stage: str
    profile_id: str
    endpoint: str
    model: str
    capabilities: dict
    settings: Settings
    pack: dict = field(default_factory=dict)
    brief: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)
    checkpoint: dict = field(default_factory=dict)
    request_text: str = ""
    confirmed_brief_version: int = 0
    repair_count: int = 0
    input_manifest_key: str = ""


def canonical(data) -> bytes:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _read_json(store: ObjectStore, storage_key: str) -> dict:
    return json.loads(store.read_object(storage_key).decode("utf-8"))


# ---- 材料读取：只用库里已有的固定内容，不在这里触发抓取／ASR／OCR ----


def _segments_of(db: Session, store: ObjectStore, item: Item) -> list[dict]:
    rows = list(db.query(StoredFile).filter(
        StoredFile.item_id == item.id, StoredFile.relative_path == "segments.json"
    ).order_by(StoredFile.created_at.desc(), StoredFile.id).all())
    for f in rows:
        try:
            doc = _read_json(store, f.storage_key)
        except Exception:
            continue
        if doc.get("source_revision") != item.source_revision:
            continue
        out = []
        for i, seg in enumerate(doc.get("segments") or [], start=1):
            text = (seg or {}).get("text")
            if isinstance(text, str) and text.strip():
                out.append({"id": f"seg{i:04d}", "text": text.strip()})
        return out
    return []


def _claims_of(db: Session, store: ObjectStore, item: Item) -> list[dict]:
    """已有单篇提炼观点：只作为候选启发，正文仍以固定片段为准。"""
    rows = list(db.query(StoredFile).filter(
        StoredFile.item_id == item.id, StoredFile.relative_path == "analysis.json"
    ).order_by(StoredFile.created_at.desc(), StoredFile.id).all())
    for f in rows:
        try:
            doc = _read_json(store, f.storage_key)
        except Exception:
            continue
        if doc.get("source_revision") != item.source_revision:
            continue
        return [
            {"id": f"c{i:04d}", "text": (p or {}).get("text", "").strip()[:2000],
             "segment_ids": [s for s in (p or {}).get("evidence_ids") or [] if isinstance(s, str)]}
            for i, p in enumerate(doc.get("key_points") or [], start=1)
            if isinstance(p, dict) and (p.get("text") or "").strip()
        ]
    return []


def _assets_of(db: Session, item: Item) -> list[dict]:
    rows = list(db.query(StoredFile).filter(
        StoredFile.item_id == item.id,
        StoredFile.role == "source_material",
        StoredFile.mime.in_(("image/png", "image/jpeg", "image/webp", "image/gif")),
    ).order_by(StoredFile.created_at, StoredFile.id).all())
    return [
        {"asset_id": f"a{i}", "mime": f.mime, "sha256": f.sha256, "bytes": f.bytes,
         "storage_key": f.storage_key}
        for i, f in enumerate(rows, start=1)
    ]


def pick_profile(db: Session, user_id: str, profile_id: str | None):
    """按用户现有云端提炼配置选模型：显式指定优先，其次默认配置，再否则最近一份。"""
    rows = list(db.query(ProviderProfile, Credential).join(
        Credential, Credential.profile_id == ProviderProfile.id
    ).filter(
        ProviderProfile.user_id == user_id,
        ProviderProfile.kind == "llm",
        ProviderProfile.adapter == "openai-compatible",
        Credential.revoked_at.is_(None),
    ).all())
    if profile_id:
        return next(((p, c) for p, c in rows if p.id == profile_id), None)
    if not rows:
        return None
    user = db.get(User, user_id)
    default_id = ((user.settings_json or {}).get("default_profile_id")) if user else None
    return next(((p, c) for p, c in rows if p.id == default_id), None) or max(rows, key=lambda pc: pc[1].created_at)


def runtime_manifest(settings: Settings) -> dict:
    """运行手册的来源：清单可能按 CWD 或仓库根启动，两个位置都试一次。"""
    path = Path(settings.share_runtime_manifest)
    candidates = [path]
    if not path.is_absolute():
        candidates = [Path.cwd() / path, Path(__file__).resolve().parents[4] / path]
    for candidate in candidates:
        if candidate.exists():
            return json.loads(candidate.read_text(encoding="utf-8"))
    raise ShareInputError("runtime_manifest_missing",
                          f"读不到运行环境清单：{settings.share_runtime_manifest}")


def runtime_version(settings: Settings) -> str:
    try:
        return str(runtime_manifest(settings).get("runtime_version") or "")
    except Exception:
        return ""


def allowed_imports(settings: Settings) -> set[str]:
    return {i.get("specifier") for p in runtime_manifest(settings).get("packages") or []
            for i in p.get("imports") or []}


# ---- 阶段 1：固定输入快照 ----


def prepare_inputs(session_factory, plan: SharePlan) -> None:
    settings = plan.settings
    with session_factory() as db:
        run = repo.submit_with_lease(db, plan.run_id, plan.lease_token)
        if run is None:
            return
        work = db.get(ShareWork, run.work_id)
        if work is None or work.deleted_at is not None:
            repo.finish_run(db, run, "cancelled", detail="作品已删除")
            db.commit()
            return
        selected = (run.checkpoint_json or {}).get("items") or []
        if len(selected) > settings.share_max_items:
            repo.finish_run(db, run, "failed", reason_code="too_many_items",
                            detail=f"一次最多选择 {settings.share_max_items} 篇材料")
            db.commit()
            return
        store = ObjectStore()
        specs: list[sharing.SourceSpec] = []
        missing: list[dict] = []
        total_chars = 0
        for entry in selected:
            item = db.get(Item, entry.get("item_id"))
            if item is None or item.user_id != plan.user_id or item.deleted_at is not None:
                raise ShareInputError("item_unavailable", "选中的材料已不可用")
            if item.source_revision != entry.get("source_revision"):
                raise ShareInputError(
                    "source_changed", "选中的材料在这之后被重新提取过，请按当前材料重新创建作品",
                    details={"item_id": item.id},
                )
            source = db.query(SourceRevision).filter(
                SourceRevision.item_id == item.id,
                SourceRevision.revision == item.source_revision).one_or_none()
            if source is None:
                raise ShareInputError("source_missing", "选中的材料缺少来源版本")
            meta = source.metadata_json or {}
            segments = _segments_of(db, store, item)
            if not segments:
                missing.append({"item_id": item.id, "title": meta.get("title") or "未命名材料"})
                continue
            total_chars += sum(len(s["text"]) for s in segments)
            specs.append(sharing.SourceSpec(
                source_key=f"s{len(specs) + 1}", item_id=item.id,
                source_revision=item.source_revision, bundle_revision=item.bundle_revision,
                title=meta.get("title") or "未命名材料", coverage=meta.get("coverage") or "unknown",
                author=meta.get("author"), canonical_url=meta.get("canonical_url"),
                source_label=meta.get("source_label"), segments=segments,
                claims=_claims_of(db, store, item), assets=_assets_of(db, item),
            ))
        if missing:
            run.checkpoint_json = {**(run.checkpoint_json or {}), "missing_items": missing}
            repo.finish_run(db, run, "failed", reason_code="material_unreadable",
                            detail="有材料还没有可读正文，请补充材料或取消选择后重试")
            db.commit()
            return
        if total_chars > settings.share_max_source_chars:
            repo.finish_run(db, run, "failed", reason_code="source_too_large",
                            detail=f"所选正文合计 {total_chars} 字符，超过 "
                                   f"{settings.share_max_source_chars} 上限；请缩小材料范围")
            db.commit()
            return
        specs = specs[: settings.share_max_source_chunks]
        pack = sharing.build_source_pack(specs)
        manifest = sharing.build_source_pack(specs, include_storage=True)
        pack_sha, pack_key, _ = store.put_bytes(canonical(pack))
        manifest_sha, manifest_key, manifest_bytes = store.put_bytes(canonical(manifest))
        repo.register_artifact(db, store, user_id=plan.user_id, work_id=plan.work_id, run_id=run.id,
                              role="input_snapshot", data=canonical(manifest),
                              storage_key=manifest_key, sha256=manifest_sha)
        run.input_manifest_key = manifest_key
        run.input_hash = pack_sha
        run.runtime_version = runtime_version(settings)
        run.prompt_version = sharing.PROMPT_VERSION
        run.recipe_hash = hashlib.sha256(sharing.SHARE_RECIPE_VERSION.encode()).hexdigest()[:16]
        run.checkpoint_json = {**(run.checkpoint_json or {}), "pack_storage_key": pack_key,
                               "manifest_key": manifest_key, "source_chars": total_chars,
                               "initial_request": run.request_text}
        run.stage = "clarifying"
        run.updated_at = utcnow()
        db.commit()
    plan.pack = pack
    plan.checkpoint["pack_storage_key"] = pack_key
    plan.input_manifest_key = manifest_key


# ---- 会话装配与模型调用 ----


def _conversation(session_factory, plan: SharePlan, purpose: str, *, system: str,
                  pack_text_value: str) -> tuple[ShareConversation, list[ConversationMessage]]:
    """取（或建）会话并装配请求：固定前缀 + 原顺序历史；前缀只序列化一次后持久复用。"""
    with session_factory() as db:
        conv = db.query(ShareConversation).filter(
            ShareConversation.work_id == plan.work_id,
            ShareConversation.purpose == purpose).first()
        store = ObjectStore()
        if conv is None:
            profile = db.get(ProviderProfile, plan.profile_id)
            conv = repo.create_conversation(
                db, user_id=plan.user_id, work_id=plan.work_id, purpose=purpose,
                profile_id=plan.profile_id, profile_version=profile.version if profile else None,
                api_protocol=(profile.capabilities_json or {}).get("api_protocol", "openai-chat")
                if profile else "openai-chat",
            )
            prefix = stable_prefix(system_prompt=system, pack_text=pack_text_value,
                                   initial_request=plan.request_text)
            artifact = repo.register_artifact(
                db, store, user_id=plan.user_id, work_id=plan.work_id, run_id=plan.run_id,
                role="conversation_message",
                data=canonical([m.as_request_message() for m in prefix]))
            conv.prefix_artifact_key = artifact.storage_key
            conv.prefix_hash = prefix_hash(prefix)
            db.commit()
        history = [
            ConversationMessage(
                role=m.role,
                content=store.read_object(m.content_key).decode("utf-8"),
                metadata=m.protocol_metadata_json or {},
            )
            for m in repo.list_messages(db, plan.user_id, conv.id, context_epoch=conv.context_epoch)
        ]
        prefix = [
            ConversationMessage(role=item["role"], content=item["content"])
            for item in json.loads(store.read_object(conv.prefix_artifact_key).decode("utf-8"))
        ]
        db.refresh(conv)
        return conv, request_messages(prefix=prefix, history=[h for h in history if h.role != "system"])


def _cache_policy(plan: SharePlan, conv: ShareConversation | None) -> dict:
    """应用专用 HMAC 从 user_id＋配置版本＋会话＋epoch 派生稳定不透明键（§6.5.4）。"""
    if conv is None or not conv.profile_id:
        return {}
    material = (f"share-cache|{plan.user_id}|{conv.profile_id}|{conv.profile_version}"
                f"|{conv.id}|{conv.context_epoch}")
    digest = hmac.new(plan.settings.load_master_key(), material.encode("utf-8"),
                      hashlib.sha256).hexdigest()
    policy = {"prompt_cache_key": f"kbshare-{digest[:48]}"}
    retention = (plan.capabilities or {}).get("cache_retention")
    if retention in ("in_memory", "24h"):
        policy["prompt_cache_retention"] = retention
    return policy


def call_model(session_factory, plan: SharePlan, *, step_key: str,
               messages: list[ConversationMessage], conv: ShareConversation | None) -> tuple[dict, str]:
    """一次带 ProviderOperation 与检查点的模型调用，返回 (解析后的 JSON, 原始文本)。

    不设 max_tokens：思考型模型的思维链与正文共用输出额度，写死小预算会在正文
    出现之前必然截断（实测澄清 1200 全花在思考上）。被长度截断仍然判为无效。

    同一轮只允许一个在途调用；已发出但没收到响应的调用标 unknown_outcome，
    恢复时不盲目重发（docs/20 §13.1、验收 A20）。
    """
    with session_factory() as db:
        op = repo.latest_operation(db, plan.run_id, step_key)
        if op is not None and op.state == "sent":
            raise ProviderOutcomeUnknown(f"{step_key} 上次请求已发出但结果未确认")
        if op is not None and op.state == "prepared":
            provider_ops.finish_operation(op, "failed", "中断：请求未发出，重新规划")
            db.commit()
        prefix_part = messages[:3]
        created = repo.create_operation(
            db, user_id=plan.user_id, run_id=plan.run_id, step_key=step_key,
            profile_id=plan.profile_id, request_fingerprint=prefix_hash(messages),
            conversation_id=conv.id if conv else None,
            context_epoch=conv.context_epoch if conv else None,
            input_message_seq=conv.last_message_seq if conv else None,
            prefix_hash=prefix_hash(prefix_part),
        )
        db.commit()
        operation_id = created.id

    with session_factory() as db:
        op = db.get(ProviderOperation, operation_id)
        if op is None or op.state != "prepared":
            raise ProviderOutcomeUnknown("操作状态已变化，放弃本次发送")
        provider_ops.mark_sent(op)
        db.commit()

    provider = OpenAICompatibleProvider(
        endpoint=plan.endpoint, api_key=_decrypt(session_factory, plan),
        model=plan.model, capabilities=plan.capabilities,
    )
    repo.heartbeat(session_factory, plan.run_id, plan.lease_token)
    result = provider.generate_conversation(ConversationRequest(
        messages=messages, temperature=0.2,
        json_mode=True, cache_policy=_cache_policy(plan, conv),
    ))
    repo.heartbeat(session_factory, plan.run_id, plan.lease_token)
    with session_factory() as db:
        op = db.get(ProviderOperation, operation_id)
        if op is not None:
            provider_ops.finish_operation(op, "succeeded")
            op.usage_json = result.usage or {}
            op.provider_task_id = (result.provider_request_id or "")[:200] or None
            db.commit()
    if result.truncated:
        raise OutputInvalid("truncated", ["输出被长度截断，请精简内容后重试"], result.output_text)
    try:
        return parse_model_json(result.output_text), result.output_text
    except ValueError as exc:
        raise OutputInvalid("json", [str(exc)], result.output_text) from exc


def _decrypt(session_factory, plan: SharePlan) -> str:
    with session_factory() as db:
        cred = db.query(Credential).filter(
            Credential.profile_id == plan.profile_id, Credential.revoked_at.is_(None)
        ).order_by(Credential.created_at.desc()).first()
        if cred is None:
            raise ProviderAuthFailed("凭据不存在或已撤销")
        try:
            return cred_crypto.decrypt_secret(
                cred.encrypted_secret, cred.encrypted_dek, cred.nonces_json,
                plan.settings.load_master_key(),
                user_id=plan.user_id, profile_id=plan.profile_id, credential_version=cred.version,
            )
        except Exception as exc:  # 解密失败按凭据失效处理，不回退他人 Key
            raise ProviderAuthFailed(f"凭据解密失败：{type(exc).__name__}") from exc


def _append_assistant(session_factory, plan: SharePlan, conv_id: str, text: str,
                      *, reply_to_round_id: str | None = None) -> None:
    store = ObjectStore()
    with session_factory() as db:
        conv = db.get(ShareConversation, conv_id)
        if conv is None:
            return
        repo.append_message(db, store, conversation=conv, run_id=plan.run_id, user_id=plan.user_id,
                            work_id=plan.work_id, role="assistant",
                            content=text if isinstance(text, bytes) else text.encode("utf-8"),
                            reply_to_round_id=reply_to_round_id)
        db.commit()


# ---- 阶段 2：需求澄清 ----


def clarify(session_factory, plan: SharePlan) -> None:
    settings = plan.settings
    round_no = int(plan.checkpoint.get("clarify_round", 0)) + 1
    conv, messages = _conversation(session_factory, plan, "content",
                                   system=share_prompts.CONTENT_SYSTEM,
                                   pack_text_value=share_prompts.pack_text(plan.pack))
    tail = share_prompts.clarification_tail(
        instructions=plan.request_text,
        pack_summary=share_prompts.pack_summary_for_clarification(plan.pack),
        prior_brief=plan.brief, round_no=round_no,
    )
    request = request_messages(prefix=messages, history=[],
                              tail=[ConversationMessage(role="user", content=tail)])
    try:
        doc, _raw = call_model(session_factory, plan, step_key=f"clarify-{round_no}",
                               messages=request, conv=conv)
        errors = sharing.validate_clarification(doc, max_questions=settings.share_max_questions_per_round)
        if errors:
            # 每次回答默认只触发 1 次澄清调用，输出格式错误最多修复 1 次（§14.1）
            repair_request = request_messages(
                prefix=messages, history=[],
                tail=[
                    ConversationMessage(role="user", content=tail),
                    ConversationMessage(role="assistant", content=json.dumps(doc, ensure_ascii=False)),
                    ConversationMessage(role="user", content=share_prompts.clarification_repair_tail(errors)),
                ])
            doc, _raw = call_model(session_factory, plan, step_key=f"clarify-{round_no}-fix",
                                   messages=repair_request, conv=conv)
            errors = sharing.validate_clarification(doc, max_questions=settings.share_max_questions_per_round)
            if errors:
                raise OutputInvalid("clarification", errors)
    except ProviderAuthFailed as exc:
        _wait(session_factory, plan, state="waiting_key", stage="clarifying",
              reason="key_rejected", detail=str(exc))
        return
    except ProviderRetryable as exc:
        _retry(session_factory, plan, f"ProviderRetryable: {exc}")
        return
    except ProviderOutcomeUnknown as exc:
        _finish(session_factory, plan, "unknown_outcome", reason="outcome_unknown", detail=str(exc))
        return
    except (OutputInvalid, ProviderInvalidRequest) as exc:
        _finish(session_factory, plan, "failed", reason="model_output_invalid", detail=str(exc))
        return

    doc.setdefault("schema_version", sharing.SCHEMA_VERSION)
    store = ObjectStore()
    round_id = f"{plan.run_id}-r{round_no}"
    with session_factory() as db:
        run = repo.submit_with_lease(db, plan.run_id, plan.lease_token)
        if run is None:
            return
        assistant_text = json.dumps(doc, ensure_ascii=False, sort_keys=True)
        _append_assistant(session_factory, plan, conv.id, assistant_text, reply_to_round_id=round_id)
        repo.register_artifact(db, store, user_id=plan.user_id, work_id=plan.work_id, run_id=run.id,
                              role="clarification_round",
                              data=canonical({"round_id": round_id, "round": round_no, **doc}))
        brief, provenance = merge_brief(previous_brief=plan.brief, previous_provenance=plan.provenance,
                                        model_brief=doc.get("brief"))
        run.brief_version = (run.brief_version or 0) + 1
        run.brief_key = repo.register_artifact(
            db, store, user_id=plan.user_id, work_id=plan.work_id, run_id=run.id, role="brief",
            data=canonical({"version": run.brief_version, "brief": brief, "provenance": provenance}),
        ).storage_key
        run.pending_round_id = round_id
        run.pending_round_key = store.put_bytes(canonical({"round_id": round_id, **doc}))[1]
        run.checkpoint_json = {**(run.checkpoint_json or {}), "clarify_round": round_no}
        state = "awaiting_confirmation" if doc.get("next_action") == "confirm_brief" else "waiting_user"
        repo.wait_for_user(db, run, state=state, stage="clarifying")
        db.commit()


# ---- 阶段 3：整合稿 ----


def synthesize(session_factory, plan: SharePlan) -> None:
    conv, messages = _conversation(session_factory, plan, "content",
                                   system=share_prompts.CONTENT_SYSTEM,
                                   pack_text_value=share_prompts.pack_text(plan.pack))
    tail = share_prompts.synthesis_tail(brief=plan.brief, provenance=plan.provenance,
                                        confirmed_version=plan.confirmed_brief_version)
    request = request_messages(prefix=messages, history=[],
                              tail=[ConversationMessage(role="user", content=tail)])
    try:
        doc, _raw = call_model(session_factory, plan, step_key="synthesis", messages=request,
                               conv=conv)
        errors = sharing.validate_synthesis(doc, pack=plan.pack)
        if errors:
            raise OutputInvalid("synthesis", errors)
    except ProviderAuthFailed as exc:
        _wait(session_factory, plan, state="waiting_key", stage="synthesizing",
              reason="key_rejected", detail=str(exc))
        return
    except ProviderRetryable as exc:
        _retry(session_factory, plan, f"ProviderRetryable: {exc}")
        return
    except ProviderOutcomeUnknown as exc:
        _finish(session_factory, plan, "unknown_outcome", reason="outcome_unknown", detail=str(exc))
        return
    except (OutputInvalid, ProviderInvalidRequest) as exc:
        _finish(session_factory, plan, "failed", reason="model_output_invalid", detail=str(exc))
        return
    doc.setdefault("schema_version", sharing.SCHEMA_VERSION)
    store = ObjectStore()
    _append_assistant(session_factory, plan, conv.id, json.dumps(doc, ensure_ascii=False, sort_keys=True))
    with session_factory() as db:
        run = repo.submit_with_lease(db, plan.run_id, plan.lease_token)
        if run is None:
            return
        key = repo.register_artifact(db, store, user_id=plan.user_id, work_id=plan.work_id,
                                     run_id=run.id, role="synthesis",
                                     data=canonical(doc)).storage_key
        run.checkpoint_json = {**(run.checkpoint_json or {}), "synthesis_key": key}
        run.stage = "generating"
        run.updated_at = utcnow()
        db.commit()


# ---- 阶段 4：页面源文件 ----


def generate_page(session_factory, plan: SharePlan, *, repair: bool = False) -> None:
    settings = plan.settings
    manifest = _read_json(ObjectStore(), plan.checkpoint.get("manifest_key") or plan.input_manifest_key)
    assets = [{"asset_id": a["asset_id"], "mime": a["mime"], "sha256": a["sha256"],
               "storage_key": a.get("storage_key")}
              for s in manifest.get("sources") or [] for a in s.get("assets") or []]
    # 发给模型的素材目录一律剥掉 storage_key（domain/sharing.py 的口径：模型只用逻辑
    # 标识）。原表留在 checkpoint["asset_catalog"] 里，交接 runner 时还要靠它读对象。
    assets_for_model = [{k: v for k, v in a.items() if k != "storage_key"} for a in assets]
    synthesis = _read_json(ObjectStore(), plan.checkpoint["synthesis_key"])
    references = sharing.public_reference_view(synthesis, plan.pack)
    runbook = share_prompts.runbook_text(runtime_manifest(settings))
    conv, messages = _conversation(
        session_factory, plan, "code", system=share_prompts.code_system(runbook),
        pack_text_value=canonical({"runbook": runbook, "assets": assets_for_model}).decode("utf-8"),
    )
    if repair:
        tail = share_prompts.repair_tail(
            diagnostics=plan.checkpoint.get("diagnostics") or [],
            keep=plan.checkpoint.get("keep") or ["正文内容、引用与用户要求的交互"],
            source=plan.checkpoint.get("page_source") or {})
        step = f"repair-{plan.repair_count}"
    else:
        tail = share_prompts.code_tail(synthesis=synthesis, asset_catalog=assets_for_model,
                                       reference_catalog=references, runbook=runbook,
                                       instructions=plan.request_text)
        step = "page_source"
    request = request_messages(prefix=messages, history=[],
                              tail=[ConversationMessage(role="user", content=tail)])
    try:
        doc, _raw = call_model(session_factory, plan, step_key=step, messages=request, conv=conv)
        errors = sharing.validate_page_source(
            doc, allowed_imports=allowed_imports(settings),
            known_asset_ids={a["asset_id"] for a in assets},
            known_reference_ids={r["ref_id"] for r in references},
            max_html_chars=settings.share_max_html_bytes // 4,
            max_interactions=settings.share_max_interactions,
        )
        if errors:
            raise OutputInvalid("page_source", errors)
    except ProviderAuthFailed as exc:
        _wait(session_factory, plan, state="waiting_key", stage="generating",
              reason="key_rejected", detail=str(exc))
        return
    except ProviderRetryable as exc:
        _retry(session_factory, plan, f"ProviderRetryable: {exc}")
        return
    except ProviderOutcomeUnknown as exc:
        _finish(session_factory, plan, "unknown_outcome", reason="outcome_unknown", detail=str(exc))
        return
    except (OutputInvalid, ProviderInvalidRequest) as exc:
        _after_failed_build(session_factory, plan, errors=getattr(exc, "errors", [str(exc)]))
        return
    store = ObjectStore()
    _append_assistant(session_factory, plan, conv.id, canonical(doc))
    with session_factory() as db:
        run = repo.submit_with_lease(db, plan.run_id, plan.lease_token)
        if run is None:
            return
        key = repo.register_artifact(db, store, user_id=plan.user_id, work_id=plan.work_id,
                                     run_id=run.id, role="page_source",
                                     data=canonical(doc)).storage_key
        run.checkpoint_json = {**(run.checkpoint_json or {}), "page_source_key": key,
                               "page_source": doc, "public_references": references,
                               "asset_catalog": assets,
                               "keep": _keep_notes(synthesis, references)}
        run.stage = "packaging"
        run.updated_at = utcnow()
        db.commit()
    handoff_to_runner(session_factory, plan)


def _keep_notes(synthesis: dict, references: list[dict]) -> list[str]:
    notes = [f"章节：{s.get('heading')}" for s in synthesis.get("sections") or []][:12]
    notes.append(f"公开来源 {len(references)} 条")
    return notes


# ---- 阶段 5：打包与检查交接 ----


def spool_dirs(settings: Settings) -> Path:
    root = Path(settings.share_spool_dir)
    for sub in (".tmp", "ready", "working", "done", "failed"):
        _spool_dir(root / sub)
    return root


def _spool_dir(path: Path) -> None:
    """交接目录被两个不同 uid 的容器共用，靠共同组 kbshare（gid 950）读写（docs/20 §14.3、审查 C-02）。

    2770 + setgid：对端按组就能写与删条目，新建的子目录还自动继承同一个组；
    不再对机器上任意进程放开 0777。compose 里两个容器都 group_add 了这个 gid。
    """
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o2770)
    except PermissionError:  # 属主是 runner 镜像里的用户，改不动也能按组读写
        pass


_MIN_BODY_CHARS_FALLBACK = 300  # 取不到材料字符数时的保守值


def _min_body_chars(source_chars) -> int:
    """runner 正文最少字符数：按本轮可读材料规模给，200~800 之间。"""
    try:
        chars = int(source_chars)
    except (TypeError, ValueError):
        return _MIN_BODY_CHARS_FALLBACK
    if chars <= 0:
        return _MIN_BODY_CHARS_FALLBACK
    return min(800, max(200, chars // 25))


def _task_timeout_ms(settings: Settings) -> int:
    """runner 的整任务墙钟（它自己的自我了断），取渲染超时的两倍、限在 60~180 秒。

    与 share_runner_max_wait_seconds 不是一回事：后者是服务端等结果的耐心，
    前者防止一个卡死的页面长期占住唯一的 runner 执行槽。
    """
    return min(180_000, max(60_000, settings.share_render_timeout_seconds * 2 * 1000))


def handoff_to_runner(session_factory, plan: SharePlan) -> None:
    """写任务信封到 ready：先写临时目录，校验后原子 rename（docs/20 §14.3）。"""
    settings = plan.settings
    store = ObjectStore()
    with session_factory() as db:
        run = repo.submit_with_lease(db, plan.run_id, plan.lease_token)
        if run is None:
            return
        checkpoint = dict(run.checkpoint_json or {})
        work_id, run_id = run.work_id, run.id
    page_source = checkpoint.get("page_source") or {}
    # 任务目录名只用服务端生成的安全字符：对象 key 里有斜杠，不能直接当目录名
    suffix = hashlib.sha256(str(checkpoint.get("page_source_key") or "").encode()).hexdigest()[:8]
    task_id = f"{run_id}-{suffix}"
    root = spool_dirs(settings)
    staging = Path(tempfile.mkdtemp(prefix="task-", dir=str(root / ".tmp")))
    try:
        _spool_dir(staging)
        _spool_dir(staging / "input")
        _spool_dir(staging / "assets")
        (staging / "input" / "page_source.json").write_bytes(canonical(page_source))
        asset_entries = []
        for asset in checkpoint.get("asset_catalog") or []:
            data = store.read_object(asset["storage_key"]) if asset.get("storage_key") else b""
            name = f"{asset['asset_id']}.{_EXT.get(asset.get('mime', ''), 'bin')}"
            (staging / "assets" / name).write_bytes(data)
            asset_entries.append({"asset_id": asset["asset_id"], "file": f"assets/{name}",
                                  "mime": asset["mime"], "sha256": asset["sha256"],
                                  "bytes": len(data)})
        synthesis = _read_json(store, checkpoint["synthesis_key"])
        envelope = {
            "schema_version": "1.0", "kind": "build_check", "task_id": task_id,
            "lease_id": runner_lease_id(run_id, task_id),
            "runtime_version": runtime_version(settings),
            "page_source": "input/page_source.json", "assets": asset_entries,
            "references": checkpoint.get("public_references") or [],
            "coverage_notes": sharing.coverage_notes(plan.pack),
            "limitations": synthesis.get("limitations") or [],
            "revision_label": f"草稿 {work_id[:6]}",
            "limits": {"max_html_bytes": settings.share_max_html_bytes},
            # 交给 runner 的两项自查边界：正文最少字符数（按材料规模给，不让空页面
            # 自我认证）与整任务墙钟（runner 自我了断，不同于服务端等结果的耐心）。
            "check": {"enabled": True, "max_screenshots": settings.share_max_screenshots,
                      "ready_timeout_ms": max(3000, settings.share_render_timeout_seconds * 1000),
                      "min_body_chars": _min_body_chars(checkpoint.get("source_chars")),
                      "task_timeout_ms": _task_timeout_ms(settings)},
        }
        envelope["input_hash"] = _spool_input_hash(staging, envelope)
        (staging / "task.json").write_bytes(canonical(envelope))
        target = root / "ready" / task_id
        shutil.rmtree(target, ignore_errors=True)
        os.replace(staging, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    with session_factory() as db:
        run = repo.submit_with_lease(db, plan.run_id, plan.lease_token)
        if run is None:
            return
        run.checkpoint_json = {**(run.checkpoint_json or {}), "runner_task": task_id,
                               "runner_deadline": (utcnow() + timedelta(
                                   seconds=settings.share_runner_max_wait_seconds)).isoformat()}
        repo.reschedule(db, run, stage="awaiting_runner", seconds=settings.share_runner_poll_seconds)
        db.commit()


_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}


def runner_lease_id(run_id: str, task_id: str) -> str:
    return f"{run_id}:{task_id}"


def _spool_input_hash(staging: Path, envelope: dict) -> str:
    items = [("input/page_source.json", staging / "input" / "page_source.json")]
    items += [(a["file"], staging / a["file"]) for a in envelope.get("assets") or []]
    pairs = [f"{rel}:{hashlib.sha256(path.read_bytes()).hexdigest()}"
             for rel, path in sorted(items, key=lambda x: x[0])]
    return hashlib.sha256("\n".join(pairs).encode("utf-8")).hexdigest()


def poll_runner(session_factory, plan: SharePlan) -> None:
    """看 runner 是否已把结果落到 done/failed；没完成就释放租约重新排队。"""
    settings = plan.settings
    with session_factory() as db:
        run = repo.submit_with_lease(db, plan.run_id, plan.lease_token)
        if run is None:
            return
        checkpoint = dict(run.checkpoint_json or {})
        task_id, deadline = checkpoint.get("runner_task"), checkpoint.get("runner_deadline")
    if not task_id:
        handoff_to_runner(session_factory, plan)
        return
    root = Path(settings.share_spool_dir)
    for state, ok in (("done", True), ("failed", False)):
        result_path = root / state / task_id / "result.json"
        if not result_path.exists():
            continue
        result = json.loads(result_path.read_bytes().decode("utf-8"))
        if result.get("lease_id") != runner_lease_id(plan.run_id, task_id):
            # 迟到的结果属于旧一次执行：不采纳、不清理当前任务
            _reschedule_waiting(session_factory, plan)
            return
        ingest_runner_result(session_factory, plan, result, ok=ok, result_dir=result_path.parent)
        return
    if deadline and datetime.fromisoformat(deadline) < utcnow():
        with session_factory() as db:
            run = repo.submit_with_lease(db, plan.run_id, plan.lease_token)
            if run is None:
                return
            repo.finish_run(db, run, "failed", reason_code="runner_timeout",
                            detail="页面构建与检查超时没有返回结果；已保留输入与之前的可用版本")
            db.commit()
        cleanup_runner_task(settings, task_id)
        return
    _reschedule_waiting(session_factory, plan)


def _reschedule_waiting(session_factory, plan: SharePlan) -> None:
    with session_factory() as db:
        run = repo.submit_with_lease(db, plan.run_id, plan.lease_token)
        if run is None:
            return
        repo.reschedule(db, run, stage="awaiting_runner",
                        seconds=plan.settings.share_runner_poll_seconds)
        db.commit()


def ingest_runner_result(session_factory, plan: SharePlan, result: dict, *, ok: bool,
                         result_dir: Path) -> None:
    settings = plan.settings
    store = ObjectStore()
    diagnostics = [f"{d.get('code')}: {d.get('message')}" for d in result.get("diagnostics") or []
                   if d.get("severity") != "warning"]
    with session_factory() as db:
        run = repo.submit_with_lease(db, plan.run_id, plan.lease_token)
        if run is None:
            return
        work = db.get(ShareWork, plan.work_id)
        if work is None or work.deleted_at is not None or run.cancel_requested_at is not None:
            repo.finish_run(db, run, "cancelled", detail="作品已删除或任务已停止，不采纳迟到结果")
            db.commit()
            return
        checkpoint = dict(run.checkpoint_json or {})
        repo.register_artifact(db, store, user_id=plan.user_id, work_id=plan.work_id, run_id=run.id,
                              role="check_report", data=canonical(result.get("build") or result))
        for shot in (result.get("screenshots") or [])[: settings.share_max_screenshots]:
            path = result_dir / "out" / "screenshots" / shot.get("file", "")
            if path.exists():
                repo.register_artifact(db, store, user_id=plan.user_id, work_id=plan.work_id,
                                      run_id=run.id, role="screenshot",
                                      data=path.read_bytes(), mime="image/png")
        if ok:
            commit_ready_revision(db, store, plan, run, checkpoint, result, result_dir)
            db.commit()
            task_id = checkpoint.get("runner_task")
        elif _repairable(diagnostics) and (run.repair_count or 0) < settings.share_max_repairs:
            run.repair_count = (run.repair_count or 0) + 1
            run.checkpoint_json = {**checkpoint, "diagnostics": diagnostics[:20],
                                   "runner_task": None, "runner_deadline": None}
            run.stage = "repairing"
            run.not_before = utcnow()
            run.updated_at = utcnow()
            db.commit()
            task_id = None
        else:
            repo.finish_run(db, run, "failed", reason_code="check_failed",
                            detail="；".join(diagnostics[:3]) or "页面检查未通过")
            db.commit()
            task_id = checkpoint.get("runner_task")
    if task_id:
        cleanup_runner_task(settings, task_id)


def commit_ready_revision(db, store: ObjectStore, plan: SharePlan, run: ShareRun, checkpoint: dict,
                          result: dict, result_dir: Path) -> None:
    """只有已通过检查、对象存在且哈希匹配的产物才成为可用版本（docs/20 §13.4）。"""
    html = (result_dir / "out" / "index.html").read_bytes()
    sha, key, size = store.put_bytes(html)
    if result.get("html_sha256") and result["html_sha256"] != sha:
        raise OutputInvalid("html_digest", ["成品摘要与检查结果不一致，不提交版本"])
    synthesis_key = checkpoint.get("synthesis_key") or ""
    references = checkpoint.get("public_references") or []
    refs_key = repo.register_artifact(
        db, store, user_id=plan.user_id, work_id=plan.work_id, run_id=run.id,
        role="public_references", data=canonical(references), visibility="public").storage_key
    revision = ShareRevision(
        user_id=plan.user_id, work_id=plan.work_id,
        revision=repo.allocate_revision(db, db.get(ShareWork, plan.work_id)),
        run_id=run.id, base_revision_id=run.base_revision_id,
        confirmed_brief_key=run.brief_key or "", input_manifest_key=run.input_manifest_key,
        synthesis_key=synthesis_key, source_code_key=checkpoint.get("page_source_key") or "",
        html_key=key, html_sha256=sha, html_bytes=size, public_references_key=refs_key,
        check_report_key="", runtime_version=result.get("runtime_version") or run.runtime_version,
    )
    db.add(revision)
    db.flush()
    repo.register_artifact(db, store, user_id=plan.user_id, work_id=plan.work_id, run_id=run.id,
                          revision_id=revision.id, role="html", data=html, visibility="private",
                          storage_key=key, sha256=sha)
    work = db.get(ShareWork, plan.work_id)
    if work is not None:
        work.latest_ready_revision_id = revision.id
        if not work.title:
            work.title = str((checkpoint.get("page_source") or {}).get("title") or "")[:200]
        work.updated_at = utcnow()
    repo.finish_run(db, run, "succeeded")


def _repairable(diagnostics: list[str]) -> bool:
    """未知库、代码截断、明显脚本错误、溢出等可进入修复；缺正文、凭据失效、
    检查环境起不来不通过让 AI 改代码解决。"""
    hard_blocks = ("BROWSER_UNAVAILABLE", "INPUT_HASH_MISMATCH", "TASK_FILE", "ASSET_MISSING",
                   "RUNTIME_VERSION", "VERSION_UNSUPPORTED", "MATERIAL")
    if not diagnostics:
        return False
    return not any(any(b in d for b in hard_blocks) for d in diagnostics)


def cleanup_runner_task(settings: Settings, task_id: str | None) -> None:
    """采纳结果或判定超时后调用：runner 已把产物移到 done/failed，输入侧可以一并回收。"""
    if not task_id:
        return
    root = Path(settings.share_spool_dir)
    for sub in ("done", "failed", "ready", "working"):
        shutil.rmtree(root / sub / task_id, ignore_errors=True)


# ---- 状态写入辅助 ----


def _wait(session_factory, plan: SharePlan, *, state: str, stage: str, reason: str,
          detail: str = "") -> None:
    with session_factory() as db:
        run = repo.submit_with_lease(db, plan.run_id, plan.lease_token)
        if run is None:
            return
        run.error_detail = detail[:2000]
        repo.wait_for_user(db, run, state=state, stage=stage, reason_code=reason)
        db.commit()


def _finish(session_factory, plan: SharePlan, state: str, *, reason: str = "", detail: str = "") -> None:
    with session_factory() as db:
        run = repo.submit_with_lease(db, plan.run_id, plan.lease_token)
        if run is None:
            return
        repo.finish_run(db, run, state, detail=detail, reason_code=reason)
        db.commit()


def _retry(session_factory, plan: SharePlan, error: str) -> None:
    with session_factory() as db:
        run = repo.submit_with_lease(db, plan.run_id, plan.lease_token)
        if run is None:
            return
        repo.retry_or_fail(db, run, error)
        db.commit()


def _after_failed_build(session_factory, plan: SharePlan, *, errors: list[str]) -> None:
    """模型输出违约：还能修就带着诊断重来，不能修就保留旧可用版本并如实失败。"""
    with session_factory() as db:
        run = repo.submit_with_lease(db, plan.run_id, plan.lease_token)
        if run is None:
            return
        if _repairable([str(e) for e in errors]) and (run.repair_count or 0) < plan.settings.share_max_repairs:
            run.repair_count = (run.repair_count or 0) + 1
            run.checkpoint_json = {**(run.checkpoint_json or {}), "diagnostics": [str(e) for e in errors][:20]}
            run.stage = "repairing"
            run.not_before = utcnow()
            run.updated_at = utcnow()
            db.commit()
            return
        repo.finish_run(db, run, "failed", reason_code="page_source_invalid",
                        detail="；".join(str(e) for e in errors[:3]))
        db.commit()


# ---- 总入口 ----


def load_plan(session_factory, run_id: str, lease_token: str) -> SharePlan | None:
    settings = get_settings()
    store = ObjectStore()
    with session_factory() as db:
        run = repo.submit_with_lease(db, run_id, lease_token)
        if run is None:
            return None
        work = db.get(ShareWork, run.work_id)
        if work is None or work.deleted_at is not None or run.cancel_requested_at is not None:
            repo.finish_run(db, run, "cancelled",
                            detail="作品已删除" if work is None or work.deleted_at else "用户停止了本次生成")
            db.commit()
            return None
        if run.profile_id is None:
            picked = pick_profile(db, run.user_id, None)
            if picked is None:
                repo.wait_for_user(db, run, state="waiting_key", stage=run.stage, reason_code="no_profile")
                db.commit()
                return None
            run.profile_id, run.profile_version = picked[0].id, picked[0].version
            db.commit()
        profile = db.get(ProviderProfile, run.profile_id)
        if profile is None:
            repo.finish_run(db, run, "failed", reason_code="profile_missing", detail="模型配置已删除")
            db.commit()
            return None
        checkpoint = dict(run.checkpoint_json or {})
        brief_doc = _read_json(store, run.brief_key) if run.brief_key else {}
        pack = _read_json(store, checkpoint["pack_storage_key"]) if checkpoint.get("pack_storage_key") else {}
        plan = SharePlan(
            run_id=run.id, lease_token=lease_token, user_id=run.user_id, work_id=run.work_id,
            stage=run.stage, profile_id=profile.id, endpoint=profile.endpoint, model=profile.model,
            capabilities=dict(profile.capabilities_json or {}), settings=settings, pack=pack,
            brief=brief_doc.get("brief") or {}, provenance=brief_doc.get("provenance") or {},
            checkpoint=checkpoint, request_text=run.request_text or "",
            confirmed_brief_version=run.confirmed_brief_version or run.brief_version or 0,
            repair_count=run.repair_count or 0, input_manifest_key=run.input_manifest_key or "",
        )
        db.commit()
    return plan


HANDLERS = {
    "preparing": prepare_inputs,
    "clarifying": clarify,
    "synthesizing": synthesize,
    "generating": generate_page,
    "repairing": lambda s, p: generate_page(s, p, repair=True),
    "packaging": handoff_to_runner,
    "awaiting_runner": poll_runner,
}


def execute(session_factory, run_id: str, lease_token: str) -> None:
    """一次领取内连续推进阶段，直到需要等待（用户／runner）或落定。

    阶段之间不重新排队：一次创作的澄清、整合、生成、打包是同一件事；
    需要等待的阶段一律释放租约后返回，不占着执行槽干等。
    """
    for _ in range(12):
        plan = load_plan(session_factory, run_id, lease_token)
        if plan is None:
            return
        handler = HANDLERS.get(plan.stage)
        if handler is None:
            _finish(session_factory, plan, "failed", reason="bad_stage",
                    detail=f"未知阶段：{plan.stage}")
            return
        stage_before = plan.stage
        try:
            handler(session_factory, plan)
        except ShareInputError as exc:
            _finish(session_factory, plan, "failed", reason=exc.reason_code, detail=exc.message)
            return
        except ProviderAuthFailed as exc:
            _wait(session_factory, plan, state="waiting_key", stage=plan.stage,
                  reason="key_rejected", detail=str(exc))
            return
        except ProviderOutcomeUnknown as exc:
            _finish(session_factory, plan, "unknown_outcome", reason="outcome_unknown",
                    detail=str(exc))
            return
        except ProviderRetryable as exc:
            _retry(session_factory, plan, f"ProviderRetryable: {exc}")
            return
        except (OutputInvalid, ProviderInvalidRequest) as exc:
            _finish(session_factory, plan, "failed", reason="model_output_invalid", detail=str(exc))
            return
        except Exception as exc:  # noqa: BLE001 —— 未预期异常必须落回任务表，否则租约到期前任务卡住
            _retry(session_factory, plan, f"{type(exc).__name__}: {exc}")
            return
        with session_factory() as db:
            run = db.get(ShareRun, run_id)
            if run is None or run.state != "running" or run.stage == stage_before:
                return  # 已落定、进入等待或阶段没推进：交还给轮询


def main() -> None:
    settings = get_settings()
    settings.ensure_dirs()
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)
    from ..models import Base

    Base.metadata.create_all(engine)
    print("[share-worker] 已启动，等待分享创作任务…")
    while True:
        try:
            recovered = repo.recover_expired_leases(session_factory)
            if recovered:
                print(f"[share-worker] 恢复过期租约 {recovered} 个任务")
            run = repo.claim_run(session_factory)
            if run is None:
                time.sleep(settings.worker_poll_seconds)
                continue
            execute(session_factory, run.id, run.lease_token)
        except KeyboardInterrupt:
            print("[share-worker] 收到退出信号")
            break
        except Exception as exc:  # noqa: BLE001 —— 主循环兜底，避免队列停摆
            print(f"[share-worker] 循环异常：{type(exc).__name__}: {exc}")
            time.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    main()
