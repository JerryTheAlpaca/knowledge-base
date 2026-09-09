"""enrich 阶段：按用户凭据调用模型 → 校验 → 发布成品 Bundle（docs/02 §4.2、§7.3、§8.1、§11）。

事务边界：
- Phase A（事务）：校验任务/来源/凭据，创建 provider_operation（prepared）。
- Phase B（事务外）：解密凭据、标记 operation=sent、调用模型（长任务调用前续租）。
- Phase C（事务）：落定操作状态、发布新 Bundle、落 Item 状态。

故障语义（docs/02 §8.3；docs/05 §5：不再有金额预算与账本）：
- 无凭据 -> waiting_key；401/403/解密失败 -> waiting_key；
  429/5xx/连接失败 -> retry_wait 有限退避；请求已发出但超时/租约丢失 ->
  unknown_outcome（不盲目重发，用户可显式重新加工）；
  JSON/证据校验失败 -> 最多 1 次修复调用，仍失败保留诊断文件、不覆盖成品。
- 旧 enrich 结果发布前复查 source_revision，来源已更新则取消任务（A13）。
- Key 只从当前用户配置解密；解密失败按凭据失效处理，不回退他人 Key。
- 不统计模型用量与费用（docs/05 §5）：响应 usage 不影响成功与否。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..config import get_settings
from ..domain import analysis, pipeline, provider_ops, templates
from ..models import (
    Capture,
    Credential,
    Item,
    Job,
    ProviderOperation,
    ProviderProfile,
    SourceRevision,
    StoredFile,
    utcnow,
)
from ..providers.llm import (
    GenerateRequest,
    OpenAICompatibleProvider,
    ProviderAuthFailed,
    ProviderError,
    ProviderInvalidRequest,
    ProviderOutcomeUnknown,
    ProviderRetryable,
    parse_model_json,
)
from ..security import credentials as cred_crypto
from ..storage.objects import ObjectStore


class AnalysisInvalid(Exception):
    """模型输出未通过 Schema/证据校验（含修复调用后仍失败）。"""

    def __init__(self, errors: list[str], raw: str, doc: dict | None):
        super().__init__("；".join(errors[:5]))
        self.errors = errors
        self.raw = raw
        self.doc = doc


@dataclass
class EnrichPlan:
    job_id: str
    lease_token: str
    item_id: str
    user_id: str
    source_revision: int
    profile_id: str
    endpoint: str
    model: str
    capabilities: dict
    operation_id: str
    segments: list[dict] = field(default_factory=list)
    user_note: str | None = None
    source_meta: dict = field(default_factory=dict)
    conversation_mode: bool = False
    chunked: bool = False
    chunks: list[list[dict]] = field(default_factory=list)
    max_output_tokens: int = 2000


# ---- 公共小工具 ----

def _lease_refresh(session_factory, job_id: str, lease_token: str) -> None:
    now = utcnow().replace(tzinfo=None)
    with session_factory() as db:
        db.execute(
            text("UPDATE jobs SET lease_until=:lu WHERE id=:jid AND lease_token=:lt AND state='running'"),
            {"lu": now + timedelta(seconds=get_settings().job_lease_seconds),
             "jid": job_id, "lt": lease_token},
        )
        db.commit()


def _owned_job(db: Session, plan: EnrichPlan) -> Job | None:
    """仅当租约仍属于本次执行时返回任务，防止与接管者互相覆盖。"""
    job = db.get(Job, plan.job_id)
    if job is None or job.lease_token != plan.lease_token or job.state != "running":
        return None
    return job


def _base_bundle_files(db: Session, item: Item) -> list[StoredFile]:
    """新 bundle 的基础文件 = 全部原始材料与提取产物（含用户上传附件）。
    只排除旧加工产物（generated/preview），它们由本次 enrich 重新生成。"""
    rows = (
        db.query(StoredFile)
        .filter(
            StoredFile.item_id == item.id,
            StoredFile.user_id == item.user_id,
            StoredFile.role.in_(["original_submission", "source_material"]),
        )
        .all()
    )
    return pipeline.latest_files_per_path(rows)


def _load_segments(db: Session, item: Item) -> list[dict]:
    """读取当前来源版本对应的 segments.json；按登记时间取最新并核对 source_revision。"""
    rows = (
        db.query(StoredFile)
        .filter(StoredFile.item_id == item.id, StoredFile.relative_path == "segments.json")
        .order_by(StoredFile.created_at.desc(), StoredFile.id)
        .all()
    )
    store = ObjectStore()
    for f in rows:
        try:
            doc = json.loads(store.read_object(f.storage_key).decode("utf-8"))
        except Exception:
            continue
        if doc.get("source_revision") == item.source_revision:
            return [
                s for s in (doc.get("segments") or [])
                if isinstance(s, dict) and s.get("segment_id") and s.get("text")
            ]
    return []


# ---- Phase A：校验与操作登记 ----

def prepare(session_factory, job_id: str, lease_token: str) -> EnrichPlan | None:
    """校验并登记调用操作；返回 None 表示任务已在库中落定（无需再调用模型）。"""
    with session_factory() as db:
        job = db.get(Job, job_id)
        if job is None or job.lease_token != lease_token or job.state != "running":
            return None
        item = db.get(Item, job.item_id)
        if item is None:
            job.state = "failed"
            job.last_error = "条目不存在"
            db.commit()
            return None
        if item.deleted_at is not None:
            job.state = "cancelled"
            db.commit()
            return None
        source = (
            db.query(SourceRevision)
            .filter(SourceRevision.item_id == item.id, SourceRevision.revision == job.source_revision)
            .one_or_none()
        )
        if source is None:
            job.state = "cancelled"
            job.last_error = "来源版本缺失"
            db.commit()
            return None
        if item.source_revision != job.source_revision:
            # 来源已更新（补充材料/重提取），本次旧结果不得发布（A13）
            job.state = "cancelled"
            job.last_error = f"来源已更新至 r{item.source_revision}，取消旧加工任务"
            db.commit()
            return None

        row = (
            db.query(ProviderProfile, Credential)
            .join(Credential, Credential.profile_id == ProviderProfile.id)
            .filter(
                ProviderProfile.user_id == item.user_id,
                ProviderProfile.kind == "llm",
                ProviderProfile.adapter == "openai-compatible",
                Credential.revoked_at.is_(None),
            )
            .order_by(Credential.created_at.desc())
            .first()
        )
        if row is None:
            _waiting(db, job, item, "waiting_key",
                     "未配置模型凭据：配置后自动继续；原始材料已保存。", "item_waiting_key")
            db.commit()
            return None
        profile, _credential = row

        # 已有未完成操作：sent 表示请求可能已生效，不盲目重发（docs/02 §7.3）
        op = provider_ops.latest_for_job(db, job.id)
        if op is not None and op.state == "sent":
            _mark_unknown_outcome(db, job, item, op, "上次请求已发出但结果未确认；可从条目发起重新加工。")
            db.commit()
            return None
        if op is not None and op.state in ("prepared", "reserved"):
            # 上次登记后未发出即中断：请求确定未发出，安全放弃并重新规划
            provider_ops.finish_operation(op, "failed", "中断：请求未发出，重新规划")
            db.flush()

        capture = db.get(Capture, item.capture_id)
        input_kind = (capture.input_json or {}).get("input_kind") if capture else None
        segments = _load_segments(db, item)
        if not segments:
            _waiting(db, job, item, "needs_input", "缺少可加工的来源片段；请补充材料。", "item_needs_input")
            db.commit()
            return None

        meta = source.metadata_json
        conversation_mode = input_kind in {"conversation", "workflow"} or meta.get("platform") in {
            "ai_conversation", "agent_workflow"
        }
        caps = profile.capabilities_json or {}
        context_tokens = int(caps.get("context_tokens") or 8000)
        max_output = int(caps.get("max_output_tokens") or 2000)

        chunked = False
        chunks: list[list[dict]] = []
        seg_tokens = sum(templates.estimate_tokens(s["text"]) for s in segments)
        if seg_tokens > max(1000, int(context_tokens * 0.6)):
            chunked = True
            chunks = templates.plan_chunks(segments, int(context_tokens * 0.6))

        fingerprint_src = json.dumps(
            [pipeline.RECIPE_VERSION, profile.id, profile.model, source.content_hash, chunked, len(chunks)],
            sort_keys=True,
        )
        op = provider_ops.create_operation(
            db,
            user_id=item.user_id,
            job_id=job.id,
            profile_id=profile.id,
            request_fingerprint=pipeline.sha256_hex(fingerprint_src.encode())[:32],
        )
        db.commit()

        return EnrichPlan(
            job_id=job.id,
            lease_token=lease_token,
            item_id=item.id,
            user_id=item.user_id,
            source_revision=source.revision,
            profile_id=profile.id,
            endpoint=profile.endpoint,
            model=profile.model,
            capabilities=caps,
            operation_id=op.id,
            segments=segments,
            user_note=meta.get("user_note"),
            source_meta=meta,
            conversation_mode=conversation_mode,
            chunked=chunked,
            chunks=chunks,
            max_output_tokens=max_output,
        )


def _waiting(db: Session, job: Job, item: Item, state: str, detail: str, event_type: str) -> None:
    item.pipeline_state = state
    item.state_detail = detail[:200]
    job.state = "succeeded"
    pipeline.emit_event(db, item.user_id, item_id=item.id, bundle_revision=item.bundle_revision,
                        event_type=event_type)


def _mark_unknown_outcome(db: Session, job: Job, item: Item, op: ProviderOperation, note: str) -> None:
    provider_ops.mark_unknown(op, note)
    item.pipeline_state = "unknown_outcome"
    item.state_detail = note[:200]
    job.state = "succeeded"
    pipeline.emit_event(db, item.user_id, item_id=item.id, bundle_revision=item.bundle_revision,
                        event_type="item_unknown_outcome", payload={"operation_id": op.id})


# ---- Phase B：调用模型 ----

def call_provider(session_factory, plan: EnrichPlan) -> dict:
    """调用模型（可分块+合并+修复），返回 {"doc", "raw"}。

    抛出 ProviderError 子类或 AnalysisInvalid。
    """
    settings = get_settings()
    with session_factory() as db:
        cred = (
            db.query(Credential)
            .filter(Credential.profile_id == plan.profile_id, Credential.revoked_at.is_(None))
            .order_by(Credential.created_at.desc())
            .first()
        )
        if cred is None:
            raise ProviderAuthFailed("凭据不存在或已撤销")
        try:
            api_key = cred_crypto.decrypt_secret(
                cred.encrypted_secret, cred.encrypted_dek, cred.nonces_json,
                settings.load_master_key(),
                user_id=plan.user_id, profile_id=plan.profile_id, credential_version=cred.version,
            )
        except Exception as exc:  # 解密失败=凭据不可用；不回退其他用户或管理员 Key
            raise ProviderAuthFailed(f"凭据解密失败：{type(exc).__name__}") from exc

    provider = OpenAICompatibleProvider(
        endpoint=plan.endpoint,
        api_key=api_key,
        model=plan.model,
        capabilities=plan.capabilities,
    )

    # 标记已发送：此后进程崩溃/租约丢失都按 unknown_outcome 处理（docs/02 §7.3）
    with session_factory() as db:
        op = db.get(ProviderOperation, plan.operation_id)
        if op is None or op.state != "prepared":
            raise ProviderOutcomeUnknown("操作状态已变化，放弃本次发送")
        provider_ops.mark_sent(op)
        db.commit()

    raws: list[dict] = []

    def _call(prompt: str) -> dict:
        _lease_refresh(session_factory, plan.job_id, plan.lease_token)
        result = provider.generate(GenerateRequest(
            system=templates.SYSTEM_PROMPT,
            user=prompt,
            max_output_tokens=plan.max_output_tokens,
            temperature=0.2,
            json_mode=True,
        ))
        raws.append(result.raw)
        try:
            return parse_model_json(result.output_text)
        except ValueError:
            # 主输出不是 JSON：走一次修复调用
            return _repair(prompt, result.output_text, ["输出不是合法 JSON 对象"])

    def _repair(original_prompt: str, raw_output: str, errors: list[str]) -> dict:
        _lease_refresh(session_factory, plan.job_id, plan.lease_token)
        prompt = templates.build_repair_user_prompt(original_prompt, raw_output, errors)
        result = provider.generate(GenerateRequest(
            system=templates.SYSTEM_PROMPT,
            user=prompt,
            max_output_tokens=plan.max_output_tokens,
            temperature=0.0,
            json_mode=True,
        ))
        raws.append(result.raw)
        return parse_model_json(result.output_text)

    segment_ids = {s["segment_id"] for s in plan.segments}
    segment_texts = {s["segment_id"]: s.get("text") or "" for s in plan.segments}

    def _validate(doc: dict, base_prompt: str, raw_text: str) -> dict:
        errors = analysis.validate_analysis(
            doc, source_revision=plan.source_revision, segment_ids=segment_ids,
            segment_texts=segment_texts,
        ) if isinstance(doc, dict) else ["输出不是 JSON 对象"]
        if errors:
            try:
                doc2 = _repair(base_prompt, raw_text, errors)
            except (ValueError, ProviderError):
                raise AnalysisInvalid(errors, raw_text, doc if isinstance(doc, dict) else None) from None
            errors2 = analysis.validate_analysis(
                doc2, source_revision=plan.source_revision, segment_ids=segment_ids,
                segment_texts=segment_texts,
            )
            if errors2:
                raise AnalysisInvalid(errors2, raw_text, doc2)
            return doc2
        return doc

    if plan.chunked:
        candidates: dict[str, list] = {"key_points": [], "excerpts": [], "methods": [], "insights": []}
        chunk_errors: list[str] = []
        for i, chunk in enumerate(plan.chunks, start=1):
            prompt = templates.build_chunk_user_prompt(
                source_meta=plan.source_meta, segments=chunk,
                chunk_index=i, chunk_total=len(plan.chunks),
            )
            doc = _call(prompt)
            for key in candidates:
                value = doc.get(key)
                if isinstance(value, list):
                    candidates[key].extend(v for v in value if isinstance(v, dict))
                else:
                    chunk_errors.append(f"第 {i} 段输出缺少 {key}")
        chunk_errors = list(dict.fromkeys(chunk_errors))
        merge_prompt = templates.build_merge_user_prompt(
            source_meta=plan.source_meta, user_note=plan.user_note,
            candidates=candidates, conversation_mode=plan.conversation_mode,
            source_revision=plan.source_revision,
        )
        merge_doc = _call(merge_prompt)
        if chunk_errors:
            merge_doc.setdefault("limitations", []).append("部分分段输出不完整，对应内容可能缺失。")
        doc = _validate(merge_doc, merge_prompt, json.dumps(merge_doc, ensure_ascii=False))
    else:
        prompt = templates.build_user_prompt(
            source_meta=plan.source_meta, user_note=plan.user_note,
            segments=plan.segments, conversation_mode=plan.conversation_mode,
            source_revision=plan.source_revision,
        )
        doc = _call(prompt)
        doc = _validate(doc, prompt, json.dumps(doc, ensure_ascii=False))

    return {"doc": doc, "raw": raws[-1] if raws else {}}


# ---- Phase C：落定状态与发布 ----

def finish(session_factory, plan: EnrichPlan, result: dict) -> None:
    with session_factory() as db:
        job = _owned_job(db, plan)
        if job is None:
            return  # 租约丢失：operation 处于 sent，恢复路径按 unknown_outcome 处理
        item = db.get(Item, plan.item_id)
        op = db.get(ProviderOperation, plan.operation_id)
        if item is None or op is None:
            return

        provider_ops.finish_operation(op, "succeeded")

        if item.deleted_at is not None:
            job.state = "cancelled"
            db.commit()
            return
        if item.source_revision != plan.source_revision:
            # 调用期间来源更新：不发布旧结果（A13）
            job.state = "cancelled"
            job.last_error = f"来源已更新至 r{item.source_revision}，放弃发布旧结果"
            db.commit()
            return

        doc = result["doc"]
        doc.setdefault("schema_version", templates.SCHEMA_VERSION)
        doc["source_revision"] = plan.source_revision
        doc["recipe_version"] = pipeline.RECIPE_VERSION
        # 结构化证据映射（docs/08 §6.1）：claim_id -> 原文片段 ID；云端不生成双链
        doc["evidence_map"] = analysis.evidence_map(doc)

        store = ObjectStore()
        preview_md = analysis.render_preview_md(doc, user_note=plan.user_note)

        analysis_file = pipeline.register_file(
            db, store, user_id=item.user_id, item_id=item.id,
            data=pipeline.canonical_json(doc), relative_path="analysis.json",
            role="generated", mime="application/json",
        )
        preview_file = pipeline.register_file(
            db, store, user_id=item.user_id, item_id=item.id,
            data=preview_md.encode("utf-8"), relative_path="preview.md",
            role="preview", mime="text/markdown",
        )
        db.flush()

        source = (
            db.query(SourceRevision)
            .filter(SourceRevision.item_id == item.id, SourceRevision.revision == plan.source_revision)
            .one()
        )
        pipeline.publish_bundle(
            db, store, item=item, source=source,
            files=_base_bundle_files(db, item) + [analysis_file, preview_file],
            processing_state="ready", pipeline_state="ready",
            result_file_id=analysis_file.file_id,
            warnings=["AI 加工完成。"],
        )
        item.state_detail = ""
        job.state = "succeeded"
        pipeline.emit_event(
            db, item.user_id, item_id=item.id, bundle_revision=item.bundle_revision,
            event_type="item_ready",
            payload={"bundle_revision": item.bundle_revision},
        )
        db.commit()


def _diagnostic_bundle(session_factory, plan: EnrichPlan, exc: AnalysisInvalid) -> None:
    """校验最终失败：保留诊断文件、任务与条目落 failed；不覆盖成品。"""
    with session_factory() as db:
        job = _owned_job(db, plan)
        item = db.get(Item, plan.item_id)
        op = db.get(ProviderOperation, plan.operation_id)
        if job is None or item is None or op is None:
            return
        provider_ops.finish_operation(op, "failed", "输出未通过校验")

        diagnostic = {
            "schema_version": "1.0",
            "kind": "analysis_validation_error",
            "source_revision": plan.source_revision,
            "errors": exc.errors,
            "candidate_output": exc.doc,
        }
        store = ObjectStore()
        diag_file = pipeline.register_file(
            db, store, user_id=item.user_id, item_id=item.id,
            data=pipeline.canonical_json(diagnostic), relative_path="analysis.error.json",
            role="generated", mime="application/json",
        )
        db.flush()
        job.state = "failed"
        job.last_error = f"AnalysisInvalid: {'；'.join(exc.errors[:3])}"
        if item.deleted_at is None and item.source_revision == plan.source_revision:
            source = (
                db.query(SourceRevision)
                .filter(SourceRevision.item_id == item.id, SourceRevision.revision == plan.source_revision)
                .one_or_none()
            )
            if source is not None:
                pipeline.publish_bundle(
                    db, store, item=item, source=source,
                    files=_base_bundle_files(db, item) + [diag_file],
                    processing_state="failed", pipeline_state="failed",
                    warnings=["AI 输出未通过校验，已保留诊断文件；原始材料不受影响。"],
                )
                item.state_detail = "AI 输出未通过校验；可重新加工。"
        db.commit()


# ---- 总入口 ----

def execute(session_factory, job_id: str, lease_token: str) -> None:
    """enrich 阶段总入口：由 worker.run_once 调用。非 Provider 异常向上抛给 run_once 重试。"""
    plan = prepare(session_factory, job_id, lease_token)
    if plan is None:
        return

    try:
        result = call_provider(session_factory, plan)
    except ProviderOutcomeUnknown as exc:
        with session_factory() as db:
            job = _owned_job(db, plan)
            item = db.get(Item, plan.item_id)
            op = db.get(ProviderOperation, plan.operation_id)
            if job and item and op and item.deleted_at is None:
                _mark_unknown_outcome(db, job, item, op, f"供应商结果未知：{exc}；可从条目发起重新加工。")
                db.commit()
        return
    except ProviderAuthFailed as exc:
        with session_factory() as db:
            job = _owned_job(db, plan)
            item = db.get(Item, plan.item_id)
            op = db.get(ProviderOperation, plan.operation_id)
            if job and item and op and item.deleted_at is None:
                provider_ops.finish_operation(op, "failed", f"凭据被拒绝：{type(exc).__name__}")
                _waiting(db, job, item, "waiting_key",
                         f"模型凭据被拒绝或不可用：{exc}；更新 Key 后自动继续。",
                         "item_waiting_key")
                db.commit()
        return
    except ProviderRetryable as exc:
        with session_factory() as db:
            op = db.get(ProviderOperation, plan.operation_id)
            if op is not None:
                provider_ops.finish_operation(op, "failed", f"可重试错误：{type(exc).__name__}")
                db.commit()
        with session_factory() as db:
            from .worker import retry_or_fail

            job = db.get(Job, job_id)
            if job is not None and job.lease_token == lease_token and job.state == "running":
                retry_or_fail(db, job, f"ProviderRetryable: {exc}")
                db.commit()
        return
    except ProviderInvalidRequest as exc:
        with session_factory() as db:
            job = _owned_job(db, plan)
            item = db.get(Item, plan.item_id)
            op = db.get(ProviderOperation, plan.operation_id)
            if job and item and op and item.deleted_at is None:
                provider_ops.finish_operation(op, "failed", "请求被供应商拒绝")
                job.state = "failed"
                job.last_error = f"ProviderInvalidRequest: {exc}"
                item.pipeline_state = "failed"
                item.state_detail = f"模型请求被拒绝：{str(exc)[:150]}"
                db.commit()
        return
    except AnalysisInvalid as exc:
        _diagnostic_bundle(session_factory, plan, exc)
        return

    finish(session_factory, plan, result)
