"""旧提炼产物 → ContentDocument v3 的一次性历史转换器（docs/23 §8.1、docs/24 §7）。

职责边界：
- 只读旧 Bundle 清单里**实际登记**的文件，产出 v3 的「模型输出主体」与引用表输入；
  字段校验、`e` 键引用表与 Markdown 由 `domain/content_v3.py` 负责（docs/24 §11）。
- 不调用模型，不修改旧 Bundle、manifest 与历史回执；写入只发生在 publish_bundle。
- 旧证据解析不出来、或无法判定产物当时依据哪一版原文时标
  `legacy_evidence_unresolved`：不编造来源，也不拿当前最新的 segments.json 顶替。

为什么要回退 AI 纠错：来源版本可以不变而正文被改写。`workers/enrich.py` 先生成提炼
主体（用的是纠错前的片段文本），落盘时才把纠错后的文本写回同一 `source_revision` 的
`segments.json`，并在 `ai_corrections` 里留下 original/corrected。所以转换时读同一份
manifest 的 segments.json 并把纠错回退成 original，才是产物当时真正引用的文本；
回退不了（缺 original）就当无法判定版本，交 unresolved。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from ..models import BundleRevision, ContentMigration, Item, SourceRevision, StoredFile
from ..storage.objects import ObjectStore
from . import pipeline

# 转换器版本：转换规则有实质变化时递增，同一旧输入允许按新版重转一次
CONVERTER_VERSION = "legacy-analysis-v1"

# v3 边界（docs/24 §1）
MAX_BLOCK_TEXT = 2000
MAX_SUMMARY_TEXT = 500
MAX_LIMITATION_TEXT = 1000
MAX_LIMITATIONS = 10
# 读取历史产物的字节闸门：只挡失控大对象，正常字幕正文远小于此
MAX_READ_BYTES = 16 * 1024 * 1024

UNRESOLVED = "legacy_evidence_unresolved"
TRUNCATED = "truncated"
# 与 docs/24 §2 的定位枚举一致；本地定义是为了让统计不依赖组装器
LOCATOR_KINDS = ("time", "paragraph", "line")

# 章节名沿用旧 preview.md 的分区，转换后的阅读体验与历史笔记一致
H_KEY_POINTS = "核心观点与证据"
H_EXCERPTS = "值得保留的原文摘录"
H_METHODS = "方法、适用条件与局限"
H_INSIGHTS = "AI 候选启发"
H_WORKFLOW = "决策过程"

# workflow.decisions.status → 中文标签：对话状态不能丢（docs/23 §8.1 第 3 步）
DECISION_LABEL = {"proposed": "提出", "accepted": "接受", "rejected": "否定", "unknown": "未确认"}


@dataclass
class LegacySourceText:
    """一份固定版本的原文（产物当时实际依据的那一版）：segment_id → 文本。"""

    item_id: str
    source_revision: int
    segments: list[dict]

    @property
    def order(self) -> list[str]:
        return [s["segment_id"] for s in self.segments]

    def contains(self, segment_id: str) -> bool:
        return any(s["segment_id"] == segment_id for s in self.segments)


@dataclass
class LegacyArtifact:
    """一条待迁移的旧提炼产物及其依据原文。"""

    user_id: str
    item_id: str
    bundle_revision: int
    source_revision: int
    kind: str                                  # analysis | preview_only
    schema_version: str | None                 # 旧 analysis.json 的 1.0 / 2.0
    doc: dict = field(default_factory=dict)
    source: LegacySourceText | None = None
    source_note: str = ""                      # 原文解析结果：segments | segments-unreadable | none
    provenance_unverified: bool = False        # 产物自报版本与清单不一致
    has_workflow: bool = False
    asr_derived: bool = False
    ai_corrected: bool = False                 # 同版本正文被 AI 改写过（已回退）
    edited_by_user: bool = False
    source_revision_count: int = 1
    stale_source: bool = False
    source_title: str | None = None             # 来源标题：文档标题的回退（docs/24 §1）
    product_created_at: str = ""                # 旧产物生成时间：转换结果可重复
    bundle_bytes: int = 0
    file_count: int = 0

    @property
    def input_sha256(self) -> str:
        """旧输入摘要：analysis.json + 当时原文 + 身份；同输入重复执行不重复生成。"""
        return pipeline.sha256_hex(pipeline.canonical_json({
            "converter_version": CONVERTER_VERSION,
            "item_id": self.item_id,
            "bundle_revision": self.bundle_revision,
            "source_revision": self.source_revision,
            "analysis_sha256": pipeline.sha256_hex(pipeline.canonical_json(self.doc)) if self.doc else "",
            "source_text_hash": source_text_sha256(self.source) if self.source else "",
        }))


@dataclass
class MigrationPlan:
    """纯转换结果：组装器输入 + 迁移自身发现的缺口（docs/24 §4）。"""

    subject: dict
    materials: list[dict]
    unresolved_ids: list[str]
    gaps: list[dict] = field(default_factory=list)
    missing_stages: list[str] = field(default_factory=list)
    input_sha256: str = ""

    @property
    def status(self) -> str:
        """complete | partial | unresolved | failed（docs/24 §7）。

        unresolved 表示存在定位不了的旧证据，不进完整成功内容集；没有主体可转的旧
        预览（empty_document）按 failed 计，与 apply 路径的实际结果一致。
        """
        if any(g["code"] == "empty_document" for g in self.gaps):
            return "failed"
        if self.unresolved_ids or UNRESOLVED in self.missing_stages:
            return "unresolved"
        return "partial" if self.gaps or self.missing_stages else "complete"


# ---- 读取旧产物 ----

def legacy_candidate_bundles(db: Session, *, user_id: str | None = None,
                             item_id: str | None = None) -> list[tuple[Item, BundleRevision]]:
    """待迁移清单：每条目取最新一个带产物的 Bundle，已是 v3 的条目不再进这里。

    转换成功后新 Bundle 带 `content.json`，所以下一次执行自然跳过——`--all` 可重入。
    """
    q = db.query(Item).filter(Item.deleted_at.is_(None))
    if user_id:
        q = q.filter(Item.user_id == user_id)
    if item_id:
        q = q.filter(Item.id == item_id)
    items = q.order_by(Item.created_at).all()
    if not items:
        return []

    latest: dict[str, BundleRevision] = {}
    for b in (
        db.query(BundleRevision)
        .filter(BundleRevision.user_id.in_({i.user_id for i in items}),
                BundleRevision.item_id.in_([i.id for i in items]),
                BundleRevision.processing_state.in_(["ready", "failed"]))
        .order_by(BundleRevision.revision.asc())
        .all()
    ):
        latest[b.item_id] = b  # 升序覆盖 = 最大版本号

    store = ObjectStore()
    out: list[tuple[Item, BundleRevision]] = []
    for item in items:
        bundle = latest.get(item.id)
        if bundle is None or bundle.user_id != item.user_id:
            continue
        manifest = read_manifest(store, bundle)
        if manifest is None:
            continue
        paths = pipeline.manifest_files_by_path(manifest)
        if "content.json" in paths:
            continue
        if "analysis.json" in paths or "preview.md" in paths:
            out.append((item, bundle))
    return out


def load_legacy_artifact(db: Session, store: ObjectStore, *, item: Item,
                         bundle: BundleRevision) -> LegacyArtifact | None:
    """读取一个旧 Bundle 的产物与它当时依据的原文；没有可读产物返回 None。"""
    manifest = read_manifest(store, bundle)
    if manifest is None:
        return None
    entries = pipeline.manifest_files_by_path(manifest)
    analysis_entry = entries.get("analysis.json")
    doc = None
    if analysis_entry is not None:
        doc = _read_json(db, store, user_id=item.user_id, item_id=item.id,
                         file_id=analysis_entry["file_id"])
    if doc is None and "preview.md" not in entries:
        return None

    processing = manifest.get("processing") or {}
    manifest_src = _as_int(processing.get("source_revision"))
    if manifest_src is None:
        manifest_src = _as_int(manifest.get("source_revision"))
    doc_src = _as_int((doc or {}).get("source_revision"))
    # 极旧清单没有 processing：退到产物自报版本，再退到条目当前版本
    src_rev = manifest_src if manifest_src is not None else (
        doc_src if doc_src is not None else item.source_revision
    )

    art = LegacyArtifact(
        user_id=item.user_id, item_id=item.id, bundle_revision=bundle.revision,
        source_revision=src_rev, kind="analysis" if doc is not None else "preview_only",
        schema_version=(doc or {}).get("schema_version"), doc=doc or {},
        provenance_unverified=doc is not None and doc_src is not None and doc_src != src_rev,
        has_workflow=bool((doc or {}).get("workflow")),
        product_created_at=bundle.created_at.isoformat() if bundle.created_at else "",
        bundle_bytes=sum(int(f.get("bytes") or 0) for f in entries.values()),
        file_count=len(entries),
    )
    _fill_flags(db, manifest, art, item)
    if art.kind == "analysis":
        art.source = _resolve_source_text(db, store, art, entries)
    return art


def _fill_flags(db: Session, manifest: dict, art: LegacyArtifact, item: Item) -> None:
    source = (
        db.query(SourceRevision)
        .filter(SourceRevision.item_id == item.id, SourceRevision.revision == art.source_revision)
        .one_or_none()
    )
    meta = (source.metadata_json if source is not None else None) or {}
    title = meta.get("title")
    art.source_title = title if isinstance(title, str) and title.strip() else None
    art.asr_derived = bool(meta.get("asr")) or bool((manifest.get("source") or {}).get("original_media_retained"))
    art.edited_by_user = bool(meta.get("edited_by_user"))
    art.source_revision_count = (
        db.query(SourceRevision).filter(SourceRevision.item_id == item.id).count() or 1
    )
    art.stale_source = art.source_revision != item.source_revision


def read_manifest(store: ObjectStore, bundle: BundleRevision) -> dict | None:
    """清单对象可能已被保留期清理：读不到就按「不可迁移」处理，不猜内容。"""
    try:
        raw = store.read_object(bundle.manifest_key)
    except OSError:
        return None
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def _resolve_source_text(db: Session, store: ObjectStore, art: LegacyArtifact,
                         entries: dict) -> LegacySourceText | None:
    """只认与产物同一份清单里的 segments.json。

    解析不了就不拿 normalized.md 兜底：那份正文同样可能在同一来源版本上被改写过，
    用它等于用另一版文本冒充依据，而且它没有片段边界，旧 evidence_ids 也无从对应。
    """
    seg_entry = entries.get("segments.json")
    if seg_entry is None:
        art.source_note = "none"
        return None
    doc = _read_json(db, store, user_id=art.user_id, item_id=art.item_id,
                     file_id=seg_entry["file_id"])
    source = _source_from_segments(art, doc)
    art.source_note = "segments" if source is not None else "segments-unreadable"
    return source


def _source_from_segments(art: LegacyArtifact, doc) -> LegacySourceText | None:
    if not isinstance(doc, dict):
        return None
    segs = doc.get("segments")
    if not isinstance(segs, list) or not segs:
        return None
    # 文件自报版本必须就是清单登记的那一版：跨版本取用等于拿新原文冒充旧依据
    file_src = _as_int(doc.get("source_revision"))
    if file_src is not None and file_src != art.source_revision:
        return None

    corrections = {
        c.get("segment_id"): c for c in (doc.get("ai_corrections") or [])
        if isinstance(c, dict) and c.get("segment_id")
    }
    out: list[dict] = []
    reverted = False
    for s in segs:
        if not isinstance(s, dict) or not isinstance(s.get("segment_id"), str):
            return None
        text = s.get("text") or ""
        corr = corrections.get(s["segment_id"])
        if corr is not None and text == (corr.get("corrected") or ""):
            if not isinstance(corr.get("original"), str):
                return None  # 记了纠错却没留下原句：当时文本无从判定
            text = corr["original"]
            reverted = True
        entry: dict = {"segment_id": s["segment_id"], "text": text}
        # 定位与段落归属原样带给组装器：阅读单元怎么合并由 build_ref_table 决定，
        # 迁移不自造第二套定位规则（docs/24 §2）
        for key in ("start_ms", "end_ms", "paragraph_id", "line_no"):
            if s.get(key) is not None:
                entry[key] = s[key]
        locator = s.get("locator")
        if isinstance(locator, dict) and locator.get("kind") in LOCATOR_KINDS:
            entry["locator"] = {k: locator[k] for k in
                                ("kind", "start_ms", "end_ms", "paragraph_id", "line_no")
                                if locator.get(k) is not None}
        out.append(entry)
    art.ai_corrected = bool(corrections) or reverted
    return LegacySourceText(art.item_id, art.source_revision, out)


# ---- 转换（纯函数：不调模型、不落库）----

def plan_conversion(art: LegacyArtifact) -> MigrationPlan:
    """按 docs/23 §8.1 第 2–5 步把旧 analysis.json 转成 v3 主体与引用输入。"""
    plan = MigrationPlan(subject=_subject({}, [], []), materials=[], unresolved_ids=[],
                         input_sha256=art.input_sha256)
    if art.kind == "preview_only":
        plan.gaps.append(_gap("该条目只有旧预览文本，没有结构化提炼产物", code="empty_document"))
        plan.missing_stages.append("summary")
        return plan

    sections: list[dict] = []
    unresolved: list[str] = []

    if art.source is None:
        unresolved = cited_segment_ids(art.doc)
        plan.gaps.append(_gap("旧产物依据的原文版本已不可确定", code=UNRESOLVED,
                              segment_ids=unresolved))
        plan.unresolved_ids = unresolved
        plan.missing_stages.append(UNRESOLVED)
        plan.subject = _subject(art.doc, [], _limitations(art, unresolved))
        return plan

    plan.materials = [{
        "item_id": art.source.item_id,
        "source_revision": art.source_revision,
        "segments": [dict(s) for s in art.source.segments],
    }]
    if art.provenance_unverified:
        plan.gaps.append(_gap("旧产物记录的原文版本与清单登记不一致，原文按清单登记版本解析"))

    def evidence_of(entry: dict) -> list[str]:
        return resolve_evidence(entry, art.source, unresolved)

    blocks = [
        {"kind": "claim",
         "text": _join(kp.get("text"), _conditions_of(kp)),
         "evidence": evidence_of(kp)}
        for kp in _dict_list(art.doc.get("key_points"))
    ]
    _add_section(sections, H_KEY_POINTS, blocks)

    _add_section(sections, H_EXCERPTS, [
        {"kind": "quote", "text": _raw(ex.get("text")), "evidence": evidence_of(ex)}
        for ex in _dict_list(art.doc.get("excerpts"))
    ])

    blocks = []
    for m in _dict_list(art.doc.get("methods")):
        evidence = evidence_of(m)
        blocks.append({"kind": "claim", "text": _join(m.get("text"), _conditions_of(m)),
                       "evidence": evidence})
        # 步骤同属来源内容：沿用方法的依据，不降级成无据文字
        for step in _str_list(m.get("steps")):
            blocks.append({"kind": "claim", "text": step, "evidence": list(evidence)})
    _add_section(sections, H_METHODS, blocks)

    _add_section(sections, H_INSIGHTS, [
        {"kind": "suggestion", "text": _raw(i.get("text")), "evidence": evidence_of(i)}
        for i in _dict_list(art.doc.get("insights"))
    ])

    wf_blocks = _workflow_blocks(art.doc.get("workflow"), art.source, unresolved)
    _add_section(sections, H_WORKFLOW, wf_blocks)

    plan.unresolved_ids = _dedup(unresolved)
    if plan.unresolved_ids:
        plan.missing_stages.append(UNRESOLVED)
        plan.gaps.append(_gap(f"{len(plan.unresolved_ids)} 个旧证据片段未能定位到原文",
                              code=UNRESOLVED, segment_ids=plan.unresolved_ids))
    clipped = _shrink_blocks(sections)
    if clipped:
        plan.missing_stages.append(TRUNCATED)
        plan.gaps.append(_gap(f"{clipped} 处旧文本超过 v3 单块上限，已截断保留", code=TRUNCATED))
    if not _raw(art.doc.get("summary")):
        plan.missing_stages.append("summary")
    plan.subject = _subject(art.doc, sections, _limitations(art, plan.unresolved_ids))
    return plan


def model_output_for(plan: MigrationPlan, ref_table) -> dict:
    """把块的真实原文范围换成任务内 `R` 键，得到组装器输入（docs/24 §2、§3）。

    `R` 由 `build_ref_table` 按自然段/长度合并而成，一个 R 可能覆盖多个片段；旧证据
    落在同一阅读单元里时只会得到一个 R，落在不相邻的单元里则得到多个 R，由组装器
    决定拼接与 `e` 键。迁移不按片段号自造引用，避免和组装器的分组各写一套。
    """
    owner: dict[str, str] = {}
    for key in ref_table.keys():
        entry = ref_table.get(key)
        for sid in getattr(entry, "segment_ids", None) or []:
            owner.setdefault(sid, key)
    sections = []
    for section in plan.subject["sections"]:
        blocks = []
        for block in section["blocks"]:
            refs = [key for key in ref_table.keys()
                    if key in {owner.get(sid) for sid in block.get("evidence") or []}]
            blocks.append({"kind": block["kind"], "text": block["text"], "refs": refs})
        sections.append({"heading": section["heading"], "blocks": blocks})
    return {**plan.subject, "sections": sections}


def resolve_evidence(entry: dict, source: LegacySourceText,
                     unresolved: list[str]) -> list[str]:
    """旧 evidence_ids → 该版原文里真实存在的片段范围（按原文顺序）。

    定位不到的 ID 记进 unresolved：不拿别的片段、别的版本或整篇正文冒充依据。
    """
    ids = evidence_ids(entry)
    if not ids:
        return []
    known = [i for i in ids if source.contains(i)]
    for i in ids:
        if i not in known and i not in unresolved:
            unresolved.append(i)
    return _in_order(known, source.order)


def evidence_ids(entry: dict) -> list[str]:
    """旧产物里两种证据字段：evidence_ids（主张/摘录/方法）与 basis_ids（insights）。"""
    if not isinstance(entry, dict):
        return []
    raw = entry.get("evidence_ids")
    if not isinstance(raw, list):
        raw = entry.get("basis_ids")
    return _dedup([i for i in (raw or []) if isinstance(i, str) and i])


def _workflow_blocks(workflow, source: LegacySourceText,
                     unresolved: list[str]) -> list[dict]:
    """对话状态转自然章节：提出/接受/否定/未知、放弃与试错、结果都要留下。"""
    blocks: list[dict] = []
    if not isinstance(workflow, dict):
        return blocks

    problem = _raw(workflow.get("problem"))
    if problem:
        blocks.append({"kind": "text", "text": f"要解决的问题：{problem}", "evidence": []})
    for c in _str_list(workflow.get("constraints")):
        blocks.append({"kind": "text", "text": f"约束：{c}", "evidence": []})

    for d in _dict_list(workflow.get("decisions")):
        status = d.get("status") if d.get("status") in DECISION_LABEL else "unknown"
        text = f"【{DECISION_LABEL[status]}】{_raw(d.get('text'))}"
        if status in ("accepted", "rejected"):
            # 来源明确表态过的结论 → claim；引用定位不了就空依据 + unresolved，不掩盖
            blocks.append({"kind": "claim", "text": text,
                           "evidence": resolve_evidence(d, source, unresolved)})
        else:
            # 提出/未知仍是候选，保持待验证身份（docs/23 §3.3）
            blocks.append({"kind": "suggestion", "text": text, "evidence": []})

    for a in _str_list(workflow.get("abandoned")):
        blocks.append({"kind": "text", "text": f"【放弃】{a}", "evidence": []})
    for a in _str_list(workflow.get("attempts")):
        blocks.append({"kind": "text", "text": f"【试错】{a}", "evidence": []})
    result = _raw(workflow.get("result"))
    if result:
        blocks.append({"kind": "text", "text": f"结果：{result}", "evidence": []})
    return blocks


def _subject(doc: dict, sections: list[dict], limitations: list[str]) -> dict:
    return {
        "title": "",  # 标题由程序用来源标题补（docs/24 §1）
        "summary": _clip(_raw(doc.get("summary")), MAX_SUMMARY_TEXT),
        "sections": sections,
        "limitations": limitations,
    }


def _limitations(art: LegacyArtifact, unresolved: list[str]) -> list[str]:
    out = [_clip(t, MAX_LIMITATION_TEXT) for t in _str_list(art.doc.get("limitations"))]
    out = out[:MAX_LIMITATIONS]
    note = None
    if unresolved:
        note = f"历史迁移：{len(unresolved)} 处旧证据未能定位到原文，相关结论未视为已核实。"
    elif art.source is None:
        note = "历史迁移：未能确定该产物当时依据的原文版本。"
    elif art.provenance_unverified:
        note = "历史迁移：旧产物记录的原文版本与登记清单不一致。"
    if note and len(out) < MAX_LIMITATIONS:
        out.append(note)
    return out


def cited_segment_ids(doc: dict) -> list[str]:
    ids: list[str] = []
    for key in ("key_points", "excerpts", "methods", "insights"):
        for entry in _dict_list(doc.get(key)):
            ids.extend(evidence_ids(entry))
    workflow = doc.get("workflow")
    if isinstance(workflow, dict):
        for d in _dict_list(workflow.get("decisions")):
            ids.extend(evidence_ids(d))
    return _dedup(ids)


# ---- 写入新 Bundle（docs/23 §8.1 第 6–7 步）----

def already_migrated(db: Session, *, user_id: str, item_id: str, input_sha256: str) -> bool:
    return db.query(ContentMigration).filter(
        ContentMigration.user_id == user_id,
        ContentMigration.item_id == item_id,
        ContentMigration.input_sha256 == input_sha256,
        ContentMigration.converter_version == CONVERTER_VERSION,
    ).one_or_none() is not None


def apply_migration(db: Session, store: ObjectStore, *, item: Item, artifact: LegacyArtifact,
                    plan: MigrationPlan) -> dict:
    """生成一个新 Bundle 版本 + 一行迁移台账；旧 Bundle、manifest 与历史回执不动。

    组装器在这里才导入：预览与统计不依赖 `content_v3`（docs/24 §11）。
    """
    from . import content_v3

    if already_migrated(db, user_id=item.user_id, item_id=item.id, input_sha256=plan.input_sha256):
        return {"status": "skipped", "reason": "同输入与同转换器版本已迁移"}

    revision = (item.bundle_revision or 0) + 1
    material_cls = getattr(content_v3, "Material", None)
    materials = [material_cls(**m) for m in plan.materials] if material_cls else list(plan.materials)
    ref_table = content_v3.build_ref_table(materials)
    # 迁移缺口交给组装器并入 completeness：content.json/manifest/API 用同一份值（docs/24 §4）
    document, report = content_v3.assemble_content_document(
        model_output_for(plan, ref_table),
        ref_table=ref_table,
        document_id=digest_document_id(item.id),
        kind="digest",
        revision=revision,
        item_id=item.id,
        source_revision=artifact.source_revision,
        task="digest",
        recipe_version=content_v3.CONTENT_RECIPE_VERSION,
        missing_stages=list(plan.missing_stages),
        extra_gaps=list(plan.gaps),
        source_title=artifact.source_title,
        # 用旧产物的生成时间：同一输入重复转换得到逐字节相同的文档
        created_at=artifact.product_created_at or None,
    )
    if document is None:
        reason = _join_errors(getattr(report, "errors", None))
        _record(db, item, plan, new_bundle_revision=None, status="failed",
                notes=f"组装未通过：{reason}")
        return {"status": "failed", "reason": reason}

    completeness = document.get("completeness") or {}
    state = completeness.get("state") or "complete"
    errors = content_v3.validate_content_document(document)
    if errors:
        reason = _join_errors(errors)
        _record(db, item, plan, new_bundle_revision=None, status="failed",
                notes=f"文档未通过校验：{reason}")
        return {"status": "failed", "reason": reason}

    content_bytes = pipeline.canonical_json(document)
    preview_bytes = content_v3.render_content_markdown(document).encode("utf-8")

    source = (
        db.query(SourceRevision)
        .filter(SourceRevision.item_id == item.id, SourceRevision.revision == artifact.source_revision)
        .one()
    )
    files = _carry_over_files(db, item)
    content_file = pipeline.register_file(
        db, store, user_id=item.user_id, item_id=item.id, data=content_bytes,
        relative_path="content.json", role="generated", mime="application/json",
    )
    preview_file = pipeline.register_file(
        db, store, user_id=item.user_id, item_id=item.id, data=preview_bytes,
        relative_path="preview.md", role="preview", mime="text/markdown",
    )
    db.flush()
    bundle = pipeline.publish_bundle(
        db, store, item=item, source=source, files=files + [content_file, preview_file],
        processing_state="failed" if state == "failed" else "ready",
        pipeline_state="failed" if state == "failed" else "ready",
        warnings=[g["message"] for g in completeness.get("gaps") or [] if g.get("message")],
        result_file_id=content_file.file_id,
        recipe_version=content_v3.CONTENT_RECIPE_VERSION,
        processing_extra={
            "format_version": content_v3.CONTENT_FORMAT_VERSION,
            "completeness": state,
            "content_file_id": content_file.file_id,
        },
    )
    # partial 不借用「完成」掩盖（docs/24 §4 的状态映射）
    item.state_reason = "partial_result" if state == "partial" else ""
    _record(db, item, plan, new_bundle_revision=bundle.revision, status=plan.status,
            notes=_notes(artifact, plan))
    return {"status": plan.status, "bundle_revision": bundle.revision, "completeness": state,
            "unresolved": len(plan.unresolved_ids)}


def record_migration(db: Session, *, user_id: str, item_id: str, input_sha256: str,
                     new_bundle_revision: int | None, status: str, notes: str = "") -> ContentMigration:
    row = ContentMigration(
        user_id=user_id, item_id=item_id, input_sha256=input_sha256,
        converter_version=CONVERTER_VERSION, new_bundle_revision=new_bundle_revision,
        status=status, notes=_clip(notes, 500),
    )
    db.add(row)
    db.flush()
    return row


def _record(db: Session, item: Item, plan: MigrationPlan, *, new_bundle_revision: int | None,
            status: str, notes: str) -> None:
    record_migration(db, user_id=item.user_id, item_id=item.id, input_sha256=plan.input_sha256,
                     new_bundle_revision=new_bundle_revision, status=status, notes=notes)


def _carry_over_files(db: Session, item: Item) -> list[StoredFile]:
    """新 Bundle 继续带上条目现有材料：只读登记行，不改任何旧 Bundle。"""
    rows = list(db.query(StoredFile).filter(StoredFile.user_id == item.user_id,
                                           StoredFile.item_id == item.id))
    keep = {f.relative_path: f for f in pipeline.latest_files_per_path(rows)
            if f.relative_path not in ("analysis.json", "preview.md")}
    return list(keep.values())


# ---- M0 统计：待迁移产物类型（只出计数与类别标签，不输出正文）----

INVENTORY_LABELS = [
    ("bundles", "带旧提炼产物的 Bundle 数"),
    ("items", "涉及条目数"),
    ("schema_1_0", "analysis.json schema_version=1.0"),
    ("schema_2_0", "analysis.json schema_version=2.0"),
    ("schema_other", "analysis.json 版本缺失或未知"),
    ("preview_only", "只有 preview.md 的旧 Bundle"),
    ("asr_derived", "ASR 派生来源"),
    ("multi_source_revision", "来源有多个修订的条目"),
    ("stale_source_revision", "产物依据的来源已非当前版本"),
    ("ai_corrected_same_revision", "同一来源版本被 AI 纠错改写正文"),
    ("edited_by_user", "用户编辑过原文"),
    ("workflow_bearing", "带 workflow 的产物"),
    ("provenance_unverified", "产物自报来源版本与清单不一致"),
    ("source_text_missing", "依据原文不可确定"),
    ("unresolved_evidence", "存在无法定位的旧证据"),
    ("status_complete", "迁移落 complete"),
    ("status_partial", "迁移落 partial"),
    ("status_unresolved", "迁移落 unresolved"),
    ("status_failed", "迁移落 failed（组装不通过）"),
    ("files", "涉及文件数"),
    ("bytes", "涉及文件字节"),
]


def inventory(db: Session, store: ObjectStore, *, user_id: str | None = None,
              item_id: str | None = None) -> dict:
    counts = {key: 0 for key, _label in INVENTORY_LABELS}
    seen_items: set[str] = set()
    for item, bundle in legacy_candidate_bundles(db, user_id=user_id, item_id=item_id):
        art = load_legacy_artifact(db, store, item=item, bundle=bundle)
        if art is None:
            continue
        counts["bundles"] += 1
        seen_items.add(item.id)
        counts["files"] += art.file_count
        counts["bytes"] += art.bundle_bytes
        if art.kind == "preview_only":
            counts["preview_only"] += 1
        elif art.schema_version == "1.0":
            counts["schema_1_0"] += 1
        elif art.schema_version == "2.0":
            counts["schema_2_0"] += 1
        else:
            counts["schema_other"] += 1
        for flag, key in (
            ("asr_derived", "asr_derived"), ("ai_corrected", "ai_corrected_same_revision"),
            ("edited_by_user", "edited_by_user"), ("has_workflow", "workflow_bearing"),
            ("provenance_unverified", "provenance_unverified"),
            ("stale_source", "stale_source_revision"),
        ):
            if getattr(art, flag):
                counts[key] += 1
        if art.source_revision_count > 1:
            counts["multi_source_revision"] += 1
        if art.source is None:
            counts["source_text_missing"] += 1
        plan = plan_conversion(art)
        if plan.unresolved_ids:
            counts["unresolved_evidence"] += 1
        counts[f"status_{plan.status}"] += 1
    counts["items"] = len(seen_items)
    return counts


def format_inventory(counts: dict) -> str:
    width = max(len(label) for _key, label in INVENTORY_LABELS)
    lines = [f"待迁移产物统计（转换器 {CONVERTER_VERSION}）", ""]
    for key, label in INVENTORY_LABELS:
        value = counts.get(key, 0)
        text = f"{value / 1024:.1f} KiB" if key == "bytes" else str(value)
        lines.append(f"{label:<{width}}  {text:>8}")
    return "\n".join(lines)


# ---- 小工具 ----

def digest_document_id(item_id: str) -> str:
    return f"dig-{item_id}"


def source_text_sha256(source: LegacySourceText) -> str:
    """与 docs/24 §2 同一算法：整版原文按片段顺序以 \\n 连接后取 UTF-8 sha256。"""
    joined = "\n".join(s.get("text") or "" for s in source.segments)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _in_order(ids: list[str], order: list[str]) -> list[str]:
    """按原文顺序去重排列片段 ID（引用顺序不能按模型给的乱序）。"""
    positions = sorted({order.index(i) for i in _dedup(ids) if i in order})
    return [order[p] for p in positions]


def _shrink_blocks(sections: list[dict]) -> int:
    """v3 单块 ≤2000：quote 必须保持逐字，超限交给组装器判失败；其余截断保留。"""
    clipped = 0
    for section in sections:
        for block in section["blocks"]:
            text = block.get("text") or ""
            if len(text) <= MAX_BLOCK_TEXT:
                continue
            if block["kind"] == "quote":
                continue
            block["text"] = text[:MAX_BLOCK_TEXT - 20] + "…（历史迁移截断）"
            clipped += 1
    return clipped


def _gap(message: str, *, code: str, refs: list[str] | None = None,
         segment_ids: list[str] | None = None) -> dict:
    return {"code": code, "message": message, "refs": refs or [],
            "segment_ids": segment_ids or [], "block": []}


def _join_errors(errors) -> str:
    codes: list[str] = []
    for e in errors or []:
        codes.append(e if isinstance(e, str) else str(e.get("code") or ""))
    return "；".join(_dedup([c for c in codes if c]))[:400] or "未知原因"


def _notes(art: LegacyArtifact, plan: MigrationPlan) -> str:
    return (f"schema={art.schema_version or art.kind};source={art.source_note or 'none'};"
            f"unresolved={len(plan.unresolved_ids)};gaps={len(plan.gaps)}")


def _add_section(sections: list[dict], heading: str, blocks: list[dict]) -> None:
    if blocks:
        sections.append({"heading": heading, "blocks": blocks})


def _dict_list(value) -> list[dict]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _str_list(value) -> list[str]:
    return [v.strip() for v in value
            if isinstance(v, str) and v.strip()] if isinstance(value, list) else []


def _raw(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _join(text, extra: str) -> str:
    body = _raw(text)
    return f"{body}（{extra}）" if body and extra else body


def _conditions_of(entry: dict) -> str:
    cond = _raw(entry.get("conditions"))
    return f"适用条件：{cond}" if cond else ""


def _clip(text: str, limit: int = MAX_BLOCK_TEXT) -> str:
    text = _raw(text)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _dedup(values) -> list:
    seen: set = set()
    out = []
    for v in values:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _read_json(db: Session, store: ObjectStore, *, user_id: str, item_id: str, file_id: str):
    raw = _read_text(db, store, user_id=user_id, item_id=item_id, file_id=file_id)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _read_text(db: Session, store: ObjectStore, *, user_id: str, item_id: str,
               file_id: str) -> str | None:
    """只按 (user, item, file_id) 取已登记对象，不接受任意路径（docs/24 §7）。"""
    row: StoredFile | None = db.query(StoredFile).filter(
        StoredFile.user_id == user_id, StoredFile.file_id == file_id,
        StoredFile.item_id == item_id,
    ).one_or_none()
    if row is None or row.bytes > MAX_READ_BYTES or not store.object_exists(row.storage_key):
        return None
    try:
        return store.read_object(row.storage_key).decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
