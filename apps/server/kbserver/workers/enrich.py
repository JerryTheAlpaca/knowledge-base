"""enrich 阶段：按用户凭据调用模型 → 校验 → 发布成品 Bundle（docs/02 §4.2、§7.3、§8.1、§11）。

事务边界：
- Phase A（事务）：校验任务/来源/凭据，创建 provider_operation（prepared）。
- Phase B（事务外）：解密凭据、标记 operation=sent、调用模型（长任务调用前续租）。
- Phase C（事务）：落定操作状态、发布新 Bundle、落 Item 状态。

故障语义（docs/02 §8.3；docs/05 §5：不再有金额预算与账本）：
- 无凭据 -> waiting_key；401/403/解密失败 -> waiting_key；
  429/5xx/连接失败 -> retry_wait 有限退避；请求已发出但超时/租约丢失 ->
  unknown_outcome（不盲目重发，用户可显式重新加工）；
  内容主体或引用校验失败 -> 每次逻辑操作最多 1 次修复调用，仍失败保留诊断文件、
  不覆盖成品（docs/23 §5.2 第 7 条：JSON 错误与引用错误共用这一份额度）。
- 单个内容块校验不过不再作废整篇：有效内容发布为「部分结果」（docs/24 §4）。
- AI 听错词修正产出**新的不可变来源修订**，旧版原文继续保留（docs/24 §7）。
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
from ..domain import content_v3, pipeline, provider_ops, templates
from ..extractors import paragraphs as parafmt
from ..extractors import subtitles as subfmt
from .publish import ai_paragraphing_enabled, auto_enrich_enabled
from ..models import (
    Capture,
    Credential,
    Item,
    Job,
    ProviderOperation,
    ProviderProfile,
    SourceRevision,
    StoredFile,
    User,
    utcnow,
)
from ..providers.llm import (
    GenerateRequest,
    OpenAICompatibleProvider,
    ProviderAuthFailed,
    ProviderInvalidRequest,
    ProviderOutcomeUnknown,
    ProviderRetryable,
    parse_model_json,
)
from ..security import credentials as cred_crypto
from ..storage.objects import ObjectStore


class ContentInvalid(Exception):
    """模型输出组装不成可用的 v3 文档（含修复调用后仍失败）。"""

    def __init__(self, errors: list[dict], raw: str, doc: dict | None):
        super().__init__("；".join(
            e.get("message", "") if isinstance(e, dict) else str(e) for e in errors[:5]))
        self.errors = errors
        self.raw = raw
        self.doc = doc


# 修正规则版本：派生来源修订按（父修订、本版本、结果文本摘要）去重（docs/24 §7）
CORRECTION_RULE_VERSION = "asr-correction-v1"


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
    # 优化文本配置（纠错与分段）；未设置时兜底填充整理配置，
    # 思考档位仍按优化档设置独立生效
    optimize_profile_id: str | None = None
    optimize_endpoint: str | None = None
    optimize_model: str | None = None
    optimize_capabilities: dict = field(default_factory=dict)
    segments: list[dict] = field(default_factory=list)
    paragraphs: list[dict] = field(default_factory=list)
    user_note: str | None = None
    source_meta: dict = field(default_factory=dict)
    conversation_mode: bool = False
    chunked: bool = False
    chunks: list[list[dict]] = field(default_factory=list)
    max_output_tokens: int = 2000
    subtitle_refs: dict[str, str] = field(default_factory=dict)
    paragraphing_enabled: bool = True
    # 「AI 自动整理」：关掉时本任务只做文字优化，不生成知识笔记
    digest_enabled: bool = True
    # 本次调用的 R 引用表与块边界：写入 jobs.input_json，重启后仍可按同一绑定恢复
    ref_table: content_v3.RefTable = field(default_factory=content_v3.RefTable)


def _caps_with_thinking(caps: dict | None, level: str) -> dict:
    """按用途思考档位覆盖能力表：off=显式关思考；low/high/max=开思考并带强度。

    档位存在设置里（digest_thinking/optimize_thinking），同一份模型配置因此可以
    整理开思考、优化关思考，无需重复配置两遍。
    """
    out = dict(caps or {})
    if level == "off":
        out["thinking_mode"] = False
        out.pop("thinking_effort", None)
    else:
        out["thinking_mode"] = True
        out["thinking_effort"] = level
    return out


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


def _load_segments(db: Session, item: Item) -> tuple[list[dict], list[dict]]:
    """读取当前来源版本对应的 segments.json；按登记时间取最新并核对 source_revision。

    同时返回 paragraphs（阅读层段落，摘录语义完整性的分组依据）；旧 bundle 没有
    paragraphs 字段时按片段现算，保证提示词始终有段落索引。
    """
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
            segments = [
                s for s in (doc.get("segments") or [])
                if isinstance(s, dict) and s.get("segment_id") and s.get("text")
            ]
            paragraphs = doc.get("paragraphs")
            if not isinstance(paragraphs, list) or not paragraphs:
                paragraphs = parafmt.group_paragraphs(segments)
            return segments, [p for p in paragraphs if isinstance(p, dict) and p.get("paragraph_id")]
    return [], []


# ---- 引用表与阅读单元 ----

def _indexed_segments(segments: list[dict], paragraphs: list[dict]) -> list[dict]:
    """给片段补段落归属：`build_ref_table` 以自然段为阅读单元分组（docs/24 §2）。"""
    mapping = parafmt.segment_paragraph_map(paragraphs) if paragraphs else {}
    return [dict(s, paragraph_id=mapping.get(s.get("segment_id"))) for s in segments]


def build_plan_ref_table(item_id: str, source_revision: int,
                         segments: list[dict], paragraphs: list[dict]) -> content_v3.RefTable:
    return content_v3.build_ref_table([content_v3.Material(
        item_id=item_id, source_revision=source_revision,
        segments=_indexed_segments(segments, paragraphs),
    )])


def _material_view(ref_table: content_v3.RefTable,
                   segments: list[dict] | None = None) -> list[dict]:
    """模型看到的 material：整表，或只给覆盖这批片段的阅读单元。

    跨块的阅读单元在其覆盖到的每个块里都完整出现——摘录按整单元原文比对，
    半截原文会让逐字校验产生假失败（docs/24 §2）。
    """
    if segments is None:
        return [{"ref": key, "text": entry.text} for key, entry in ref_table.entries()]
    wanted = {s.get("segment_id") for s in segments}
    return [
        {"ref": key, "text": entry.text}
        for key, entry in ref_table.entries()
        if wanted.intersection(entry.segment_ids)
    ]


def _ref_table_payload(source_revision: int, ref_table: content_v3.RefTable) -> dict:
    """固定本次操作的输入与引用表，租约丢失后接管者按同一绑定继续（docs/23 §4.1 第 4 条）。

    写在自己那一行 jobs.input_json 上，不碰 Item.source_revision：那是用户编辑的乐观并发基线。
    """
    return {
        "format_version": content_v3.CONTENT_FORMAT_VERSION,
        "recipe_version": content_v3.CONTENT_RECIPE_VERSION,
        "source_revision": source_revision,
        "ref_table": ref_table.to_json(),
    }


def _candidate_sections(document: dict | None,
                        ref_table: content_v3.RefTable) -> list[dict]:
    """把本块组装通过的内容块换回 `R` 编号，交给汇总阶段（docs/23 §5.2 第 3、4 条）。

    组装器已把 `R` 换成本文档内的 `e` 键，并且只留下通过校验的块；汇总输出仍要按
    同一张任务引用表校验，所以按（条目、来源版本、片段范围）原样映射回去，不靠顺序猜。
    """
    if not document:
        return []
    by_identity = {
        (entry.item_id, entry.source_revision, tuple(entry.segment_ids)): key
        for key, entry in ref_table.entries()
    }
    doc_refs = document.get("references") or {}
    sections: list[dict] = []
    for section in document.get("sections") or []:
        blocks = []
        for block in section.get("blocks") or []:
            refs = []
            for key in block.get("refs") or []:
                entry = doc_refs.get(key) or {}
                token = by_identity.get((entry.get("item_id"), entry.get("source_revision"),
                                         tuple(entry.get("segment_ids") or ())))
                if token:
                    refs.append(token)
            blocks.append({"kind": block.get("kind"), "text": block.get("text"), "refs": refs})
        if blocks:
            sections.append({"heading": section.get("heading") or "", "blocks": blocks})
    return sections


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

        rows = (
            db.query(ProviderProfile, Credential)
            .join(Credential, Credential.profile_id == ProviderProfile.id)
            .filter(
                ProviderProfile.user_id == item.user_id,
                ProviderProfile.kind == "llm",
                ProviderProfile.adapter == "openai-compatible",
                Credential.revoked_at.is_(None),
            )
            .all()
        )
        # 整理档：digest=整理文本（NULL 兼容存量）
        digest_rows = [(p, c) for p, c in rows if (p.role or "digest") == "digest"]
        user = db.get(User, item.user_id)
        user_settings = (user.settings_json or {}) if user else {}
        # 手动「开始整理」把意图记在任务行上，优先于关着的自动开关
        digest_enabled = bool(job.digest_requested) or auto_enrich_enabled(db, item.user_id)
        paragraphing_enabled = ai_paragraphing_enabled(db, item.user_id)
        if not digest_enabled and not paragraphing_enabled:
            # 两半都不做却排到了任务（管理 CLI 重算等历史入口）：无事可做。
            # 不发布空 Bundle 版本，也不留下「这次优化没产出」那种误导状态。
            job.state = "cancelled"
            job.last_error = "自动整理与文字优化均已关闭"
            if item.pipeline_state == "enriching":
                item.pipeline_state = "extracted"
                item.state_detail = "AI 自动加工已关闭；可在条目里手动开始整理。"
            db.commit()
            return None
        # 优化档由用户在设置里显式选择（optimize_profile_id），未设置则兜底用整理配置
        optimize_id = user_settings.get("optimize_profile_id")
        opt = next(((p, c) for p, c in rows if p.id == optimize_id), None) if optimize_id else None

        # 主配置取本次真正要用到的那一档：只开「纠错与分段」时不去要求整理档
        # 凭据，否则条目会卡在一个它根本用不上的开关上（waiting_key）。
        if digest_enabled:
            candidates: list[tuple[ProviderProfile, Credential]] = digest_rows
            missing_detail = "未配置整理文本模型凭据：配置后自动继续；原始材料已保存。"
        else:
            candidates = [opt] if opt else digest_rows
            missing_detail = ("未配置优化文本模型：在设置里配好「优化文本模型」使用的"
                              "配置后自动继续；原始材料已保存。")
        if not candidates:
            _waiting(db, job, item, "waiting_key", missing_detail, "item_waiting_key")
            db.commit()
            return None
        if digest_enabled:
            # 整理档优先使用用户设置的默认整理配置，否则取最近配置凭据的
            default_id = user_settings.get("default_profile_id")
            profile, _credential = next(
                ((p, c) for p, c in candidates if p.id == default_id), None
            ) or max(candidates, key=lambda pc: pc[1].created_at)
        else:
            profile, _credential = candidates[0]
        opt_profile, _opt_cred = opt if opt else (None, None)

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
        segments, paragraphs = _load_segments(db, item)
        if not segments:
            _waiting(db, job, item, "needs_input", "缺少可加工的来源片段；请补充材料。",
                     "item_needs_input", reason="empty_segments")
            db.commit()
            return None

        subtitle_refs = _align_subtitle_refs(segments, _load_subtitle_ref(db, item))

        meta = source.metadata_json
        conversation_mode = input_kind in {"conversation", "workflow"} or meta.get("platform") in {
            "ai_conversation", "agent_workflow"
        }
        # 思考档位按用途在设置里调（同一份配置可同时用于整理与优化）；
        # 整理默认 high（DeepSeek 服务端默认一致），优化默认关闭
        caps = _caps_with_thinking(
            profile.capabilities_json, user_settings.get("digest_thinking", "high"))
        context_tokens = int(caps.get("context_tokens") or 8000)
        max_output = int(caps.get("max_output_tokens") or 2000)
        opt_caps = _caps_with_thinking(
            (opt_profile.capabilities_json if opt_profile else profile.capabilities_json),
            user_settings.get("optimize_thinking", "off"),
        )

        chunked = False
        chunks: list[list[dict]] = []
        seg_tokens = sum(templates.estimate_tokens(s["text"]) for s in segments)
        if seg_tokens > max(1000, int(context_tokens * 0.6)):
            chunked = True
            chunks = templates.plan_chunks(segments, int(context_tokens * 0.6), paragraphs)

        ref_table = build_plan_ref_table(item.id, source.revision, segments, paragraphs)

        fingerprint_src = json.dumps(
            [
                content_v3.CONTENT_RECIPE_VERSION, profile.id, profile.model,
                opt_profile.id if opt_profile else "", opt_profile.model if opt_profile else "",
                source.content_hash, chunked, len(chunks),
            ],
            sort_keys=True,
        )
        op = provider_ops.create_operation(
            db,
            user_id=item.user_id,
            job_id=job.id,
            profile_id=profile.id,
            request_fingerprint=pipeline.sha256_hex(fingerprint_src.encode())[:32],
        )
        job.input_json = _ref_table_payload(source.revision, ref_table)
        db.commit()

        return EnrichPlan(
            job_id=job.id,
            lease_token=lease_token,
            item_id=item.id,
            user_id=item.user_id,
            source_revision=source.revision,
            ref_table=ref_table,
            profile_id=profile.id,
            endpoint=profile.endpoint,
            model=profile.model,
            capabilities=caps,
            operation_id=op.id,
            # 优化档：用设置里显式选择的配置；未设置时兜底用整理配置（同一配置），
            # 但思考档位仍按优化档设置独立生效
            optimize_profile_id=opt_profile.id if opt_profile else profile.id,
            optimize_endpoint=opt_profile.endpoint if opt_profile else profile.endpoint,
            optimize_model=opt_profile.model if opt_profile else profile.model,
            optimize_capabilities=opt_caps,
            segments=segments,
            paragraphs=paragraphs,
            user_note=meta.get("user_note"),
            source_meta=meta,
            conversation_mode=conversation_mode,
            chunked=chunked,
            chunks=chunks,
            max_output_tokens=max_output,
            subtitle_refs=subtitle_refs,
            paragraphing_enabled=paragraphing_enabled,
            digest_enabled=digest_enabled,
        )


def _load_subtitle_ref(db: Session, item: Item) -> list[dict] | None:
    """读取 Bundle 内平台字幕对照参考（asr/subtitle_ref.json）；无则 None。"""
    rows = (
        db.query(StoredFile)
        .filter(StoredFile.item_id == item.id,
                StoredFile.relative_path == "asr/subtitle_ref.json")
        .order_by(StoredFile.created_at.desc(), StoredFile.id)
        .all()
    )
    store = ObjectStore()
    for f in rows:
        try:
            doc = json.loads(store.read_object(f.storage_key).decode("utf-8"))
        except Exception:
            continue
        records = doc.get("records") or []
        if records:
            return records
    return None


def _align_subtitle_refs(segments: list[dict],
                         records: list[dict] | None) -> dict[str, str]:
    """按时间重叠把平台字幕行并到 ASR 句段：句段取与其时间重叠的字幕行拼接。"""
    refs: dict[str, str] = {}
    if not records:
        return refs
    for s in segments:
        st, en = s.get("start_ms"), s.get("end_ms")
        if st is None or en is None:
            continue
        parts = [
            r["text"] for r in records
            if r.get("text") and r.get("start_ms") is not None
            and r["start_ms"] < en and (r.get("end_ms") or 0) > st
        ]
        if parts:
            refs[s["segment_id"]] = "".join(parts)
    return refs


def _waiting(db: Session, job: Job, item: Item, state: str, detail: str, event_type: str,
             reason: str = "") -> None:
    item.pipeline_state = state
    item.state_detail = detail[:200]
    item.state_reason = reason
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

def _apply_corrections_as_revision(session_factory, plan: EnrichPlan,
                                   ai_starts: list[str] | None,
                                   corrections: list[dict]) -> None:
    """AI 听错词修正 → 新的不可变来源修订（docs/24 §7）。

    旧版原文一个字都不动：被引用过的文本永远还能读出来。同一父修订、同一规则版本、
    同一结果文本不重复建版本；引用表按新修订重建并固定回本任务行。
    """
    with session_factory() as db:
        item = db.get(Item, plan.item_id)
        parent = db.query(SourceRevision).filter(
            SourceRevision.item_id == plan.item_id,
            SourceRevision.revision == plan.source_revision,
        ).one_or_none()
        if item is None or parent is None:
            return
        segments = [dict(s) for s in plan.segments]
        corrected = {c["segment_id"]: c["corrected"] for c in corrections}
        for s in segments:
            if s["segment_id"] in corrected:
                s["text"] = corrected[s["segment_id"]]
        paragraphs = (
            parafmt.group_paragraphs_from_starts(segments, ai_starts)
            if ai_starts else parafmt.group_paragraphs(segments)
        )
        meta_updates = {
            "origin": "ai_correction",
            "parent_revision": plan.source_revision,
            "correction_rule_version": CORRECTION_RULE_VERSION,
            "ai_corrections": corrections,
        }
        content_hash = pipeline.sha256_hex(
            pipeline.canonical_json({"segments": segments, "meta_updates": meta_updates})
        )
        target = next(
            (r for r in db.query(SourceRevision).filter(
                SourceRevision.item_id == plan.item_id,
                SourceRevision.content_hash == content_hash,
            ).all()
             if (r.metadata_json or {}).get("correction_rule_version") == CORRECTION_RULE_VERSION
             and (r.metadata_json or {}).get("parent_revision") == plan.source_revision),
            None,
        )
        if target is None:
            target = SourceRevision(
                item_id=item.id, user_id=item.user_id, revision=parent.revision + 1,
                content_hash=content_hash, metadata_json=dict(parent.metadata_json, **meta_updates),
                artifacts_json={},
            )
            db.add(target)
            db.flush()
        indexed = _indexed_segments(segments, paragraphs)
        store = ObjectStore()
        merged = {f.relative_path: f for f in _base_bundle_files(db, item)}
        file_ids: list[str] = []
        for path, data, mime in (
            ("normalized.md", subfmt.segments_to_normalized_md(segments).encode("utf-8"),
             "text/markdown"),
            ("readable.md", parafmt.paragraphs_to_readable_md(paragraphs).encode("utf-8"),
             "text/markdown"),
            ("segments.json", pipeline.canonical_json({
                "source_revision": target.revision,
                "segments": indexed,
                "paragraphs": paragraphs,
                "paragraph_source": "ai",
                "ai_corrections": corrections,
            }), "application/json"),
        ):
            f = pipeline.register_file(
                db, store, user_id=item.user_id, item_id=item.id,
                data=data, relative_path=path, role="source_material", mime=mime,
            )
            merged[path] = f
            file_ids.append(f.file_id)
        item.source_revision = target.revision
        job = db.get(Job, plan.job_id)
        plan.source_revision = target.revision
        plan.segments = segments
        plan.paragraphs = paragraphs
        plan.ref_table = build_plan_ref_table(item.id, target.revision, segments, paragraphs)
        if job is not None:
            job.input_json = _ref_table_payload(target.revision, plan.ref_table)
        db.commit()


def call_provider(session_factory, plan: EnrichPlan) -> dict:
    """调用模型（可分块+合并+修复），返回 v3 文档、完整性报告与文字优化计划。

    抛出 ProviderError 子类或 ContentInvalid。
    """
    settings = get_settings()

    def _decrypt_key(profile_id: str) -> str:
        with session_factory() as db:
            cred = (
                db.query(Credential)
                .filter(Credential.profile_id == profile_id, Credential.revoked_at.is_(None))
                .order_by(Credential.created_at.desc())
                .first()
            )
            if cred is None:
                raise ProviderAuthFailed("凭据不存在或已撤销")
            try:
                return cred_crypto.decrypt_secret(
                    cred.encrypted_secret, cred.encrypted_dek, cred.nonces_json,
                    settings.load_master_key(),
                    user_id=plan.user_id, profile_id=profile_id, credential_version=cred.version,
                )
            except Exception as exc:  # 解密失败=凭据不可用；不回退其他用户或管理员 Key
                raise ProviderAuthFailed(f"凭据解密失败：{type(exc).__name__}") from exc

    digest_provider = OpenAICompatibleProvider(
        endpoint=plan.endpoint,
        api_key=_decrypt_key(plan.profile_id),
        model=plan.model,
        capabilities=plan.capabilities,
    )
    # 优化 provider：优化档未设置时用整理配置的同一份端点/密钥构造
    # （思考档位仍按优化档设置独立生效）；凭据不可用时回退整理配置——
    # 分段是尽力而为的加工步骤，不应让它阻塞整条整理
    optimize_provider: OpenAICompatibleProvider | None = None
    if plan.optimize_endpoint:
        try:
            optimize_provider = OpenAICompatibleProvider(
                endpoint=plan.optimize_endpoint,
                api_key=_decrypt_key(plan.optimize_profile_id),
                model=plan.optimize_model,
                capabilities=plan.optimize_capabilities,
            )
        except ProviderAuthFailed:
            optimize_provider = None

    # 标记已发送：此后进程崩溃/租约丢失都按 unknown_outcome 处理（docs/02 §7.3）
    with session_factory() as db:
        op = db.get(ProviderOperation, plan.operation_id)
        if op is None or op.state != "prepared":
            raise ProviderOutcomeUnknown("操作状态已变化，放弃本次发送")
        provider_ops.mark_sent(op)
        db.commit()

    raws: list[dict] = []
    outputs: list[dict] = []  # 解析后的模型输出：诊断文件要留下最后一次候选

    def _call(prompt: str, *, max_output_tokens: int | None = None,
              via: OpenAICompatibleProvider | None = None) -> dict:
        _lease_refresh(session_factory, plan.job_id, plan.lease_token)
        result = (via or digest_provider).generate(GenerateRequest(
            system=templates.SYSTEM_PROMPT,
            user=prompt,
            max_output_tokens=max_output_tokens or plan.max_output_tokens,
            temperature=0.2,
            json_mode=True,
        ))
        raws.append(result.raw)
        try:
            parsed = parse_model_json(result.output_text)
        except ValueError:
            # 主输出不是 JSON：走一次修复调用
            return _repair(prompt, result.output_text, [{"message": "输出不是合法 JSON 对象"}])
        outputs.append(parsed)
        return parsed

    def _repair(original_prompt: str, raw_output: str, errors: list[dict]) -> dict:
        _lease_refresh(session_factory, plan.job_id, plan.lease_token)
        prompt = templates.build_repair_user_prompt(original_prompt, raw_output, errors)
        result = digest_provider.generate(GenerateRequest(
            system=templates.SYSTEM_PROMPT,
            user=prompt,
            max_output_tokens=plan.max_output_tokens,
            temperature=0.0,
            json_mode=True,
        ))
        raws.append(result.raw)
        parsed = parse_model_json(result.output_text)
        outputs.append(parsed)
        return parsed

    # 纠错与分段先于提炼：修正后的文本让提炼摘录与正文一致
    # （用户关闭「AI 自动纠错与分段」时跳过，阅读层保持本地规则分段）。
    # 分段/纠错属于「优化文本」：走优化档 provider（未设置优化档时兜底用整理配置，
    # 思考档位独立、通常关闭——更便宜更快）；凭据不可用时回退整理 provider。
    text_plan = (
        _semantic_paragraph_starts(
            plan, lambda prompt, **kw: _call(prompt, via=optimize_provider, **kw))
        if plan.paragraphing_enabled else None
    )
    if text_plan:
        corrected = {c["segment_id"]: c["corrected"] for c in text_plan["corrections"]}
        for s in plan.segments:
            if s["segment_id"] in corrected:
                s["text"] = corrected[s["segment_id"]]

    # AI 听错词修正改的是被引用的原文：先落成新的不可变来源修订再提炼，旧版原文保留。
    if text_plan and text_plan["corrections"]:
        _apply_corrections_as_revision(
            session_factory, plan, text_plan.get("starts"), text_plan["corrections"])
    elif text_plan and text_plan.get("starts"):
        # 只重新分段、没改字：引用仍然有效，阅读层产物由 finish 按原版本登记
        reparsed = parafmt.group_paragraphs_from_starts(
            plan.segments, text_plan["starts"])
        plan.paragraphs = reparsed or plan.paragraphs

    if not plan.digest_enabled:
        # 「AI 自动整理」关着：文字优化本身就是这次加工的全部内容，
        # 不发起提炼调用，也不生成知识笔记（doc=None 由 finish 分支处理）
        return {"doc": None, "raw": raws[-1] if raws else {}, "ai_text_plan": text_plan}

    ref_table = plan.ref_table

    def _assemble(output, *, missing_stages: list[str], repair_calls: int):
        # revision 由 finish 在知道 Bundle 版本号时盖章：程序字段不经模型（docs/24 §1）
        return content_v3.assemble_content_document(
            output, ref_table=ref_table, document_id=f"dig-{plan.item_id}", kind="digest",
            revision=0, item_id=plan.item_id, source_revision=plan.source_revision,
            task="digest", recipe_version=content_v3.CONTENT_RECIPE_VERSION,
            repair_calls=repair_calls, missing_stages=missing_stages,
            source_title=plan.source_meta.get("title"),
        )

    def _generate(prompt: str, *, missing_stages: list[str] | None = None):
        """一次调用 + 一份额度修复：JSON 错误与引用错误共用（docs/23 §5.2 第 7 条）。

        引用表本身坏了不请求模型修复——那是程序缺陷，不该让模型修内部身份。
        """
        try:
            output = _call(prompt)
        except ValueError as exc:  # 连修复输出也不是 JSON：落失败，不重投任务
            raise ContentInvalid(
                [{"code": "json_unparsable", "message": f"修复后仍无法解析：{exc}"}], "", None
            ) from exc
        document, report = _assemble(output, missing_stages=missing_stages or [], repair_calls=0)
        if not any(e.get("code") != "ref_table_mismatch" for e in report.errors):
            return document, report
        try:
            repaired = _repair(prompt, json.dumps(output, ensure_ascii=False), report.errors)
        except ValueError as exc:
            raise ContentInvalid(
                [{"code": "json_unparsable", "message": f"修复调用没有返回合法 JSON：{exc}"}],
                "", output,
            ) from exc
        return _assemble(repaired, missing_stages=missing_stages or [], repair_calls=1)

    missing: list[str] = []
    if plan.chunked:
        # 分块提取只产候选内容块：坏块剔除并记为未覆盖，不作废整篇（docs/23 §5.2 第 3、8 条）
        candidates: list[dict] = []
        for i, chunk in enumerate(plan.chunks, start=1):
            prompt = templates.build_chunk_user_prompt(
                source_meta=plan.source_meta, material=_material_view(ref_table, chunk),
                chunk_index=i, chunk_total=len(plan.chunks),
            )
            try:
                output = _call(prompt)
            except ValueError:  # 本块输出坏了：其余块继续，最后发布部分结果
                missing.append(f"chunk:{i}")
                continue
            document, report = _assemble(output, missing_stages=[], repair_calls=0)
            sections = _candidate_sections(document, ref_table)
            if not sections:
                missing.append(f"chunk:{i}")
                continue
            candidates.append({"chunk": i, "sections": sections})
        if not candidates:
            raise ContentInvalid(
                [{"code": "missing_subject", "message": "各分段都没有产出可用的内容块"}],
                "", outputs[-1] if outputs else None)
        merge_prompt = templates.build_merge_user_prompt(
            source_meta=plan.source_meta, user_note=plan.user_note,
            candidates=candidates, conversation_mode=plan.conversation_mode,
        )
        document, report = _generate(merge_prompt, missing_stages=missing)
    else:
        prompt = templates.build_digest_user_prompt(
            source_meta=plan.source_meta, user_note=plan.user_note,
            material=_material_view(ref_table), conversation_mode=plan.conversation_mode,
        )
        document, report = _generate(prompt)

    if document is None:
        raise ContentInvalid(report.errors, "", outputs[-1] if outputs else None)
    return {
        "doc": document,
        "completeness": report.completeness,
        "repair_calls": report.repair_calls,
        "raw": raws[-1] if raws else {},
        "ai_text_plan": text_plan,
    }


# 纠错与分段调用的输出预算：推理模型会把大量输出花在思维链上，常规
# max_output_tokens（8K）常在正文输出前耗尽，content 为空（finish_reason=length）。
# 实测 92 句材料思维链 ~2.5 万 token，放宽到 32K 才能拿到正文。
_PARAGRAPHING_MAX_OUTPUT_TOKENS = 32768
# 分段任务再按句数细分：推理模型的思维链长度随任务规模不可控增长，
# 小任务（≤40 句）单次思维链可控，也更容易在请求超时内完成。
_PARAGRAPHING_CHUNK_SEGMENTS = 40


def _semantic_paragraph_starts(plan: EnrichPlan, call) -> dict | None:
    """LLM 纠错与分段 + 听错词修正（尽力而为）：失败返回 None，阅读层保持现有分段。

    分块材料逐块调用（用上一块结尾两句作承接判断，块首句若承接上文则
    不算段首），汇总各块的段首句并校验顺序；同时收集确信的听错句修正
    （逐字保留、仅改错字，长度比例守卫防改写）。
    """
    try:
        digest_chunks = plan.chunks if plan.chunked else [plan.segments]
        # 分段任务按 ≤40 句细分（推理模型思维链随任务规模不可控增长）
        chunks: list[list[dict]] = []
        for chunk in digest_chunks:
            for k in range(0, len(chunk), _PARAGRAPHING_CHUNK_SEGMENTS):
                chunks.append(chunk[k:k + _PARAGRAPHING_CHUNK_SEGMENTS])
        if not chunks:
            return None
        order = [s["segment_id"] for s in plan.segments]
        order_index = {sid: k for k, sid in enumerate(order)}
        seg_by_id = {s["segment_id"]: s for s in plan.segments}
        starts: list[str] = []
        corrections: list[dict] = []
        seen_corr: set[str] = set()
        for idx, chunk in enumerate(chunks, start=1):
            prev_tail = chunks[idx - 2][-2:] if idx > 1 else None
            chunk_ids = {s["segment_id"] for s in chunk}
            refs = {k: v for k, v in plan.subtitle_refs.items() if k in chunk_ids}
            prompt = templates.build_paragraphing_prompt(
                segments=chunk, prev_tail=prev_tail, subtitle_refs=refs or None,
                chunk_index=idx if len(chunks) > 1 else None,
                chunk_total=len(chunks) if len(chunks) > 1 else None,
            )
            doc = call(prompt, max_output_tokens=_PARAGRAPHING_MAX_OUTPUT_TOKENS)
            raw = doc.get("paragraph_starts") if isinstance(doc, dict) else None
            if not isinstance(raw, list):
                return None
            picked_set = {x for x in raw if isinstance(x, str)}
            chunk_order = [s["segment_id"] for s in chunk]
            picked = [sid for sid in chunk_order if sid in picked_set]
            if not picked:
                return None
            starts.extend(picked)

            raw_corr = doc.get("corrections") if isinstance(doc, dict) else None
            if isinstance(raw_corr, list):
                for c in raw_corr:
                    if not isinstance(c, dict):
                        continue
                    sid = c.get("segment_id")
                    new_text = (c.get("text") or "").strip()
                    if sid not in chunk_ids or sid in seen_corr or not new_text:
                        continue
                    orig = (seg_by_id[sid].get("text") or "").strip()
                    if not orig or new_text == orig:
                        continue
                    # 长度比例守卫：防模型整句改写（只允许字词级修正）
                    if not (len(orig) * 0.4 <= len(new_text) <= len(orig) * 2.5 + 4):
                        continue
                    seen_corr.add(sid)
                    corrections.append(
                        {"segment_id": sid, "original": orig, "corrected": new_text})
        if not starts:
            return None
        if order and starts[0] != order[0]:
            starts.insert(0, order[0])
        pos = [order_index.get(sid) for sid in starts]
        if any(p is None for p in pos) or pos != sorted(pos) or len(set(pos)) != len(pos):
            return None
        return {"starts": starts, "corrections": corrections}
    except Exception:  # noqa: BLE001 —— 分段/修正失败不是致命错误，回退现有分段
        return None


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

        store = ObjectStore()
        doc = result["doc"]
        completeness = result.get("completeness") or {}
        content_state = completeness.get("state") or ""
        # 「AI 自动整理」关着时 doc 为 None：本次只改写阅读层文字，不产出笔记
        generated_files: list[StoredFile] = []
        content_file: StoredFile | None = None
        if doc is not None:
            # 文档身份在程序知道 Bundle 版本号的那一刻盖章，不经模型（docs/24 §1）
            doc["revision"] = (item.bundle_revision or 0) + 1
            content_file = pipeline.register_file(
                db, store, user_id=item.user_id, item_id=item.id,
                data=pipeline.canonical_json(doc), relative_path="content.json",
                role="generated", mime="application/json",
            )
            preview = pipeline.register_file(
                db, store, user_id=item.user_id, item_id=item.id,
                data=content_v3.render_content_markdown(doc).encode("utf-8"),
                relative_path="preview.md", role="preview", mime="text/markdown",
            )
            generated_files = [content_file, preview]
            db.flush()

        # 只重新分段、没有改字：按当前来源版本重排阅读层段落。改过字的走派生修订
        #（_apply_corrections_as_revision），那里的文件已经登记，这里按路径自然取最新
        extra_files: list[StoredFile] = []
        text_plan = result.get("ai_text_plan") or {}
        ai_starts = text_plan.get("starts")
        if ai_starts and not text_plan.get("corrections"):
            ai_paragraphs = plan.paragraphs
            if ai_paragraphs:
                extra_files.append(pipeline.register_file(
                    db, store, user_id=item.user_id, item_id=item.id,
                    data=parafmt.paragraphs_to_readable_md(ai_paragraphs).encode("utf-8"),
                    relative_path="readable.md", role="source_material", mime="text/markdown",
                ))
                extra_files.append(pipeline.register_file(
                    db, store, user_id=item.user_id, item_id=item.id,
                    data=pipeline.canonical_json({
                        "source_revision": plan.source_revision,
                        "segments": _indexed_segments(plan.segments, ai_paragraphs),
                        "paragraphs": ai_paragraphs,
                        "paragraph_source": "ai",
                    }),
                    relative_path="segments.json", role="source_material",
                    mime="application/json",
                ))
                db.flush()

        source = (
            db.query(SourceRevision)
            .filter(SourceRevision.item_id == item.id, SourceRevision.revision == plan.source_revision)
            .one()
        )
        base = {f.relative_path: f for f in _base_bundle_files(db, item)}
        for f in extra_files:
            base[f.relative_path] = f
        organized = doc is not None
        pipeline.publish_bundle(
            db, store, item=item, source=source,
            files=list(base.values()) + generated_files,
            processing_state="ready" if organized else "original_only",
            pipeline_state="ready" if organized else "extracted",
            result_file_id=content_file.file_id if organized else None,
            processing_extra=({
                "format_version": content_v3.CONTENT_FORMAT_VERSION,
                "completeness": content_state,
                "content_file_id": content_file.file_id,
            } if organized else None),
            recipe_version=content_v3.CONTENT_RECIPE_VERSION if organized else None,
        )
        item.state_reason = ""
        if organized:
            if content_state == "partial":
                # 部分可用不冒充完成：状态原因给机器码，文案给人看（docs/24 §4）
                gaps = completeness.get("gaps") or []
                first = gaps[0].get("message") if gaps and isinstance(gaps[0], dict) else ""
                item.state_reason = "partial_result"
                item.state_detail = f"已整理出部分内容；{first or '部分内容未通过校验'}"
            else:
                item.state_detail = ""
        else:
            item.state_detail = (
                "已完成文字优化；AI 自动整理已关闭。" if ai_starts
                else "文字优化这次没有产出结果，阅读层保持本地分段；AI 自动整理已关闭。"
            )
        job.state = "succeeded"
        if organized:
            pipeline.emit_event(
                db, item.user_id, item_id=item.id, bundle_revision=item.bundle_revision,
                event_type="item_ready",
                payload={"bundle_revision": item.bundle_revision},
            )
        db.commit()


def _diagnostic_bundle(session_factory, plan: EnrichPlan, exc: ContentInvalid) -> None:
    """组装最终失败：保留诊断文件、任务与条目落 failed；不覆盖成品。"""
    with session_factory() as db:
        job = _owned_job(db, plan)
        item = db.get(Item, plan.item_id)
        op = db.get(ProviderOperation, plan.operation_id)
        if job is None or item is None or op is None:
            return
        provider_ops.finish_operation(op, "failed", "输出未通过校验")
        messages = [e.get("message", "") if isinstance(e, dict) else str(e) for e in exc.errors]

        diagnostic = {
            "format_version": content_v3.CONTENT_FORMAT_VERSION,
            "kind": "content_validation_error",
            "source_revision": plan.source_revision,
            "errors": exc.errors,
            "candidate_output": exc.doc,
        }
        store = ObjectStore()
        diag_file = pipeline.register_file(
            db, store, user_id=item.user_id, item_id=item.id,
            data=pipeline.canonical_json(diagnostic), relative_path="content.error.json",
            role="generated", mime="application/json",
        )
        db.flush()
        job.state = "failed"
        job.last_error = f"ContentInvalid: {'；'.join(messages[:3])}"
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
                    warnings=["AI 输出未能组装成可用内容，已保留诊断文件；原始材料不受影响。"],
                    recipe_version=content_v3.CONTENT_RECIPE_VERSION,
                )
                item.state_reason = "model_output_invalid"
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
    except ContentInvalid as exc:
        _diagnostic_bundle(session_factory, plan, exc)
        return

    finish(session_factory, plan, result)
