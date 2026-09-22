"""ContentDocument v3 契约：内容主体 + 引用解析 + 组装与校验（docs/24 §1–§4）。

纯 Python：不读数据库、不发网络请求、不依赖 FastAPI。字段名、枚举与错误码以
docs/24 为唯一裁判，本模块不发明别名。

三条贯穿全模块的规则，改动前先确认没有破坏它们：

1. 模型只写内容主体（title/summary/sections/limitations + 任务扩展），
   `format_version`、UUID、哈希、`source_revision` 等程序字段即使出现也一律
   忽略其值，由程序自填，且**不因此请求修复**（docs/24 §3）。
2. 失败按影响范围处理：坏块只作废自己（claim 全无效不降级为 suggestion、
   claim 混有无效引用整条暂不发布）；只有主体缺失/截断/空架子/程序引用表
   不一致才整篇失败（docs/23 §5.3、docs/24 §4）。
3. `R` 是本次任务的临时选项，`e` 是文档内的临时键，两者都不是永久身份；
   引用解析只做首尾空白归一与精确匹配，绝不猜 R99 想说的是 R9。

与 `domain/analysis.py`（Schema 2.0）的关系：本模块只服务 v3 新主路径，旧校验
器继续供历史读取使用，两者互不导入。逐字比对规则与 `analysis.normalize_for_quote`
有意保持同一语义（docs/08 §6.1），这里独立定义是因为 M3 会清理旧校验器
（docs/23 §5.1），新契约不应反向依赖将被删除的模块。

错误对象形状（`AssemblyReport.errors` 与 `completeness.gaps` 共用）：
`{"code", "message", "refs", "segment_ids", "block"}`；`block` 是 1 基的
`[章节序号, 块序号]`，`[n, 0]` 表示整节。errors 的 `refs` 保留模型给出的 `R`
原词（诊断与修复提示要用），gaps 的 `refs` 尽量给文档内的 `e` 键（读者能跳转），
解析不出的引用才回退给原词。
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

CONTENT_FORMAT_VERSION = "3.0"
CONTENT_RECIPE_VERSION = "content-v3-1"

# ---- 枚举（docs/24 §1、§3、§4）----
DOC_KINDS = ("digest", "knowledge", "synthesis")
BLOCK_KINDS = ("claim", "quote", "suggestion", "text")
TASKS = ("digest", "knowledge_fusion", "share_synthesis")
COMPLETENESS_STATES = ("complete", "partial", "failed")
LOCATOR_KINDS = ("time", "paragraph", "line")

ERROR_CODES = (
    "json_unparsable",
    "missing_subject",
    "truncated",
    "bad_ref",
    "quote_not_verbatim",
    "empty_document",
    "ref_table_mismatch",
    "limit_exceeded",
)
# gaps[].code 允许 §3 的组装码，外加 §4 已有的机器码（分块失败、引用全无效、
# 旧证据未解析）；界面只渲染 message，不渲染这些枚举。
GAP_CODES = ERROR_CODES + ("chunk_failed", "all_refs_invalid", "legacy_evidence_unresolved")

# ---- 体积边界（docs/24 §1）----
MAX_TITLE = 200
MAX_SUMMARY = 500
MAX_HEADING = 200
MAX_BLOCK_TEXT = 2000
MAX_SECTIONS = 40
MAX_BLOCKS = 200
MAX_REFERENCES = 60
MAX_LIMITATIONS = 10
MAX_LIMITATION_LEN = 1000
MAX_CHANGE_SUMMARY = 1000
MAX_DOC_BYTES = 512 * 1024
# 单个阅读单元的字符预算：只决定一个 R 键覆盖多少片段，不影响定位精度
# （docs/23 §4.1 规则 5：单元过长按片段边界拆开）。
MAX_REF_CHARS = 1200

REF_TOKEN_RE = re.compile(r"^R\d{1,5}$")
E_KEY_RE = re.compile(r"^e\d{1,4}$")
SEGMENT_ID_RE = re.compile(r"^s\d{4,}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ISO8601_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)
# 块正文允许必要 Markdown，但不允许 Obsidian 内部链接与旧观点块锚点（docs/24 §1）
WIKI_LINK = "[["
BLOCK_ANCHOR_RE = re.compile(r"\^c\d{4}(?!\d)")

MISSING_STAGE_ORDER = ("summary", "quote_verification", "evidence_resolution", "legacy_evidence_unresolved")


def normalize_for_quote(text: str) -> str:
    """逐字比较用：去掉全部空白，不改动文字本身。"""
    return re.sub(r"\s+", "", text or "")


def source_text_hash(text: str) -> str:
    """固定原文范围的摘要：对 "\n".join(片段原文) 的 UTF-8 字节算 sha256（docs/24 §2）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _compact(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _clean_str(value, max_len: int) -> str | None:
    """合法返回归一后的字符串，非法返回 None（非字符串/空白/超长）。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > max_len:
        return None
    return text


def _locator_from_segment(seg: dict) -> dict | None:
    """阅读单元的定位元数据：显式 locator 优先，否则从片段自带的时间/段落/行号推导。"""
    explicit = seg.get("locator")
    if isinstance(explicit, dict) and explicit.get("kind") in LOCATOR_KINDS:
        return dict(explicit)
    start, end = seg.get("start_ms"), seg.get("end_ms")
    if isinstance(start, int) and isinstance(end, int):
        return {"kind": "time", "start_ms": start, "end_ms": end}
    paragraph_id = seg.get("paragraph_id")
    if isinstance(paragraph_id, str) and paragraph_id:
        return {"kind": "paragraph", "paragraph_id": paragraph_id}
    line_no = seg.get("line_no")
    if isinstance(line_no, int):
        return {"kind": "line", "line_no": line_no}
    return None


def _merge_locators(locators: list[dict]) -> dict | None:
    """单元跨多个片段时的定位：时间可合并，段落/行号不一致就不宣称定位。"""
    kinds = {loc.get("kind") for loc in locators if loc}
    if kinds == {"time"}:
        starts = [loc["start_ms"] for loc in locators]
        ends = [loc["end_ms"] for loc in locators]
        return {"kind": "time", "start_ms": min(starts), "end_ms": max(ends)}
    if kinds == {"paragraph"}:
        ids = {loc.get("paragraph_id") for loc in locators}
        if len(ids) == 1:
            return {"kind": "paragraph", "paragraph_id": ids.pop()}
    if kinds == {"line"}:
        lines = {loc.get("line_no") for loc in locators}
        if len(lines) == 1:
            return {"kind": "line", "line_no": lines.pop()}
    return None


# ---------------------------------------------------------------- 任务内引用表


@dataclass
class RefEntry:
    """任务内一条 `R`：来源身份 + 连续片段范围 + 模型看到的原文 + 定位元数据。"""

    item_id: str
    source_revision: int
    segment_ids: list[str]
    text: str
    locator: dict | None = None

    def key(self) -> tuple:
        return (self.item_id, self.source_revision, tuple(self.segment_ids))

    def to_json(self) -> dict:
        out = {
            "item_id": self.item_id,
            "source_revision": self.source_revision,
            "segment_ids": list(self.segment_ids),
            "text": self.text,
        }
        if self.locator:
            out["locator"] = dict(self.locator)
        return out

    @classmethod
    def from_json(cls, raw: dict) -> "RefEntry":
        locator = raw.get("locator")
        return cls(
            item_id=raw.get("item_id") if isinstance(raw.get("item_id"), str) else "",
            source_revision=raw.get("source_revision"),
            segment_ids=[s for s in (raw.get("segment_ids") or []) if isinstance(s, str)],
            text=raw.get("text") if isinstance(raw.get("text"), str) else "",
            locator=dict(locator) if isinstance(locator, dict) else None,
        )


@dataclass
class Material:
    """一个来源版本：`build_ref_table` 的输入单元。"""

    item_id: str
    source_revision: int
    segments: list[dict] = field(default_factory=list)


class RefTable:
    """任务内 `R` 表：键按分配顺序保存，可 JSON 往返，分块与修复沿用同一绑定。"""

    def __init__(self, entries: list[tuple[str, RefEntry]] | None = None):
        self._entries: dict[str, RefEntry] = {}
        for key, entry in entries or []:
            self._entries[key] = entry

    @classmethod
    def from_json(cls, raw: dict) -> "RefTable":
        """从 `{"R1": {...}, ...}` 恢复；键序保持给定顺序，不重新编号（同一绑定继续复用）。"""
        return cls([
            (key, RefEntry.from_json(value))
            for key, value in raw.items()
            if isinstance(key, str) and isinstance(value, dict)
        ])

    def to_json(self) -> dict:
        return {key: entry.to_json() for key, entry in self._entries.items()}

    def keys(self) -> list[str]:
        return list(self._entries)

    def get(self, ref: str) -> RefEntry | None:
        return self._entries.get(ref)

    def entries(self) -> list[tuple[str, RefEntry]]:
        return list(self._entries.items())

    def order(self) -> dict[str, int]:
        """R 键 → 表中次序；用于判断相邻阅读单元能否拼接比对。"""
        return {key: index for index, key in enumerate(self._entries)}

    def text_of(self, ref: str) -> str:
        entry = self._entries[ref]
        return entry.text

    def __contains__(self, ref: object) -> bool:
        return ref in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def integrity_problems(self) -> list[str]:
        """程序侧一致性检查：表本身坏了就是组装失败，不该让模型修复。"""
        problems: list[str] = []
        seen: dict[tuple, str] = {}
        for key, entry in self._entries.items():
            if not REF_TOKEN_RE.match(key):
                problems.append(f"引用表键 {key} 不是 R + 数字")
            if not entry.item_id:
                problems.append(f"{key} 缺少 item_id")
            if not isinstance(entry.source_revision, int) or isinstance(entry.source_revision, bool):
                problems.append(f"{key} 的 source_revision 必须是整数")
            if not entry.segment_ids:
                problems.append(f"{key} 缺少 segment_ids")
            if any(not SEGMENT_ID_RE.match(s) for s in entry.segment_ids):
                problems.append(f"{key} 的 segment_ids 含非法片段号")
            if len(set(entry.segment_ids)) != len(entry.segment_ids):
                problems.append(f"{key} 的 segment_ids 有重复")
            if not isinstance(entry.text, str) or not entry.text.strip():
                problems.append(f"{key} 缺少原文 text")
            if entry.locator and entry.locator.get("kind") not in LOCATOR_KINDS:
                problems.append(f"{key} 的 locator.kind 非法")
            ident = entry.key()
            if ident in seen:
                problems.append(f"{key} 与 {seen[ident]} 指向同一片段范围")
            for sid in entry.segment_ids:
                overlap = (entry.item_id, entry.source_revision, sid)
                if overlap in seen:
                    problems.append(f"{key} 与 {seen[overlap]} 的片段范围重叠")
                seen[overlap] = key
        return problems


def build_ref_table(materials: list[Material], *, max_chars: int = MAX_REF_CHARS) -> RefTable:
    """按材料顺序统一分配 R1..Rn：多来源的同号片段互不覆盖（docs/24 §2）。

    分组以自然段为主：带 `paragraph_id` 的连续同段片段合成一个阅读单元，其余按
    片段边界累积到 `max_chars`；单元内片段在来源里彼此相邻，因此摘录可以跨片段
    逐字比对。
    """
    entries: list[tuple[str, RefEntry]] = []
    counter = 0
    for material in materials:
        run_ids: list[str] = []
        run_texts: list[str] = []
        run_locs: list[dict] = []
        run_len = 0
        run_para: object = None

        def flush() -> None:
            nonlocal counter, run_ids, run_texts, run_locs, run_len, run_para
            if not run_ids:
                return
            counter += 1
            entries.append((
                f"R{counter}",
                RefEntry(
                    item_id=material.item_id,
                    source_revision=material.source_revision,
                    segment_ids=list(run_ids),
                    text="\n".join(run_texts),
                    locator=_merge_locators(run_locs),
                ),
            ))
            run_ids, run_texts, run_locs, run_len, run_para = [], [], [], 0, None

        for seg in material.segments:
            if not isinstance(seg, dict):
                continue
            sid = seg.get("segment_id")
            text = seg.get("text")
            if not isinstance(sid, str) or not sid.strip() or not isinstance(text, str) or not text.strip():
                continue
            para = seg.get("paragraph_id")
            para = para if isinstance(para, str) and para else None
            if run_ids and (para != run_para or run_len + len(text) > max_chars):
                flush()
            run_ids.append(sid.strip())
            run_texts.append(text)
            run_len += len(text)
            run_para = para
            locator = _locator_from_segment(seg)
            if locator:
                run_locs.append(locator)
        flush()
    return RefTable(entries)


# ---------------------------------------------------------------- 组装


@dataclass
class AssemblyReport:
    """组装结果：文档（失败为 None）+ 错误明细 + 完整性对象（docs/24 §4）。"""

    document: dict | None
    errors: list[dict] = field(default_factory=list)
    completeness: dict = field(default_factory=dict)
    dropped_blocks: int = 0
    repair_calls: int = 0
    # 知识融合的 no_op 结果没有文档，但不是失败，W2 按此决定不写入
    no_op: bool = False
    # 任务扩展字段（change_summary/conflicts/reader_goal/…）不属于文档，交调用方记录
    task_extras: dict = field(default_factory=dict)


def _issue(
    code: str,
    message: str,
    *,
    refs: list[str] | None = None,
    segment_ids: list[str] | None = None,
    block: list[int] | None = None,
) -> dict:
    return {
        "code": code,
        "message": message,
        "refs": list(refs or []),
        "segment_ids": list(segment_ids or []),
        "block": list(block or []),
    }


def _finalize_missing_stages(codes: set[str], extra: list[str] | None) -> list[str]:
    stages = [c for c in MISSING_STAGE_ORDER if c in codes]
    chunks = sorted(
        {e for e in (extra or []) if e.startswith("chunk:")},
        key=lambda s: int(re.sub(r"\D", "", s) or 0),
    )
    others = [e for e in dict.fromkeys(extra or []) if not e.startswith("chunk:") and e not in codes]
    return chunks + stages + others


def _completeness(
    state: str,
    *,
    missing_stages: list[str],
    gaps: list[dict],
    dropped_blocks: int,
    repair_calls: int,
) -> dict:
    return {
        "state": state,
        "missing_stages": missing_stages,
        "gaps": gaps,
        "dropped_blocks": dropped_blocks,
        "repair_calls": repair_calls,
    }


def _parse_subject(model_output) -> tuple[dict | None, dict | None]:
    """返回 (主体, 整篇级错误)。程序字段不在此处读取，稍后被整体忽略。"""
    if isinstance(model_output, dict):
        return model_output, None
    if model_output is None or model_output == "":
        return None, _issue("missing_subject", "模型没有返回内容主体")
    if not isinstance(model_output, str):
        return None, _issue("missing_subject", "模型响应类型不是 JSON 对象或文本")
    raw = model_output.strip()
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        return None, _issue(_parse_code(raw, exc), "模型响应无法解析为 JSON 对象")
    if not isinstance(parsed, dict):
        return None, _issue("missing_subject", "模型响应的顶层不是 JSON 对象")
    return parsed, None


def _parse_code(raw: str, exc: Exception) -> str:
    """区分“输出被截断”与“根本不是 JSON”。

    截断的特征是文档尾部留下半句话/半个对象：报错位置落在末尾，或缺少收尾的 `}`；
    开头就报错、或结构完整但内容非法的响应按不可解析处理，两者的修复提示不同。
    """
    tail = raw.rstrip()
    end = getattr(exc, "pos", 0) or 0
    message = str(exc)
    if not tail or tail.endswith("}"):
        return "json_unparsable"
    if "Unterminated" in message:
        return "truncated"
    if message.startswith("Expecting") and end >= len(raw) * 0.4:
        return "truncated"
    return "json_unparsable"


def _forbidden_link(text: str) -> str | None:
    if WIKI_LINK in text:
        return "包含 Obsidian 内部链接"
    if BLOCK_ANCHOR_RE.search(text):
        return "包含旧观点块锚点"
    return None


def _task_extras(task: str, subject: dict) -> dict:
    """任务扩展结果：只取 §3 列出的字段，程序字段与未知字段一并忽略。"""
    extras: dict = {}
    if task == "knowledge_fusion":
        if isinstance(subject.get("no_op"), bool):
            extras["no_op"] = subject["no_op"]
        change = _clean_str(subject.get("change_summary"), MAX_CHANGE_SUMMARY)
        if change:
            extras["change_summary"] = change
        conflicts = [
            {"topic": c.get("topic"), "description": c.get("description")}
            for c in (subject.get("conflicts") or [])
            if isinstance(c, dict)
            and _clean_str(c.get("topic"), MAX_HEADING)
            and _clean_str(c.get("description"), MAX_LIMITATION_LEN)
        ]
        if conflicts:
            extras["conflicts"] = conflicts
    elif task == "share_synthesis":
        for key, limit in (("reader_goal", MAX_SUMMARY), ("visualization_intent", MAX_CHANGE_SUMMARY)):
            value = _clean_str(subject.get(key), limit)
            if value:
                extras[key] = value
        usage = [
            {"ref": m.get("ref"), "note": _clean_str(m.get("note"), MAX_SUMMARY)}
            for m in (subject.get("material_usage") or [])
            if isinstance(m, dict) and isinstance(m.get("ref"), str)
        ]
        if usage:
            extras["material_usage"] = usage
    return extras


def assemble_content_document(
    model_output: dict | str,
    *,
    ref_table: RefTable,
    document_id: str,
    kind: str,
    revision: int,
    item_id: str,
    source_revision: int,
    task: str,
    recipe_version: str,
    repair_calls: int = 0,
    missing_stages: list[str] | None = None,
    extra_gaps: list[dict] | None = None,
    input_documents: list[dict] | None = None,
    source_title: str | None = None,
    created_at: str | None = None,
) -> tuple[dict | None, AssemblyReport]:
    """按 docs/24 §3 的固定顺序组装文档，返回 `(document, AssemblyReport)`。

    调用方新增的关键字（默认值不改变 §11 的调用形式）：
    - `missing_stages` / `extra_gaps`：W2 把分块级事实（`chunk:<n>` 失败）带进来，
      组装器无法从模型响应推断未覆盖范围；
    - `input_documents`：融合/分享的知识与摘要输入，写入 `provenance`；
    - `source_title`：模型未给标题时的回退（docs/24 §1）；
    - `created_at`：可注入时间戳，便于可重入迁移与确定性测试。
    """
    errors: list[dict] = []
    gaps: list[dict] = []
    dropped = 0
    held_entries: list[RefEntry] = []  # 被挡下的块用到的引用，用于判断摘要是否失去依据
    stage_codes: set[str] = set()
    extras: dict = {}

    def note(error: dict, *, stage: str | None = None) -> None:
        errors.append(error)
        gaps.append(dict(error))
        if stage:
            stage_codes.add(stage)

    def abort(*fatal: dict) -> tuple[dict | None, AssemblyReport]:
        """整篇失败：不发布文档，但把已积累的缺口交出去，供诊断产物与 state_reason 使用。"""
        return None, AssemblyReport(
            document=None,
            errors=errors + list(fatal),
            completeness=_completeness(
                "failed",
                missing_stages=_finalize_missing_stages(stage_codes, missing_stages),
                gaps=gaps + [dict(e) for e in fatal],
                dropped_blocks=dropped,
                repair_calls=repair_calls,
            ),
            repair_calls=repair_calls,
            task_extras=extras,
        )

    subject, fatal = _parse_subject(model_output)
    if fatal is not None:
        return abort(fatal)

    problems = ref_table.integrity_problems()
    if problems:
        # 程序侧不一致按缺陷定位，不请求模型修复内部身份（docs/23 §5.3）
        return abort(_issue("ref_table_mismatch", "任务引用表与实际原文不一致：" + "；".join(problems[:5])))

    # 融合任务可以只报告“无需改动”：主体缺省也不是失败（docs/24 §3）
    if subject.get("no_op") is True and task == "knowledge_fusion":
        return None, AssemblyReport(
            document=None,
            errors=[],
            completeness=_completeness(
                "complete", missing_stages=[], gaps=[], dropped_blocks=0, repair_calls=repair_calls
            ),
            repair_calls=repair_calls,
            no_op=True,
            task_extras=_task_extras(task, subject),
        )

    title = _clean_str(subject.get("title"), MAX_TITLE)
    if title is None:
        if subject.get("title") not in (None, ""):
            note(_issue("limit_exceeded", "title 非字符串或超过上限，改用来源标题"))
        title = _clean_str(source_title, MAX_TITLE) or "未命名内容"

    summary = subject.get("summary")
    if summary is None:
        summary = ""
    elif not isinstance(summary, str):
        summary = ""
        note(_issue("missing_subject", "summary 不是字符串，已忽略"))
    else:
        summary = summary.strip()
        if len(summary) > MAX_SUMMARY:
            summary = ""
            note(_issue("limit_exceeded", "summary 超过上限，已忽略"))

    limitations: list[str] = []
    raw_limits = subject.get("limitations")
    if raw_limits not in (None, []):
        if not isinstance(raw_limits, list):
            note(_issue("missing_subject", "limitations 不是数组，已忽略"))
        else:
            for i, item in enumerate(raw_limits):
                text = _clean_str(item, MAX_LIMITATION_LEN)
                if text is None:
                    note(_issue("limit_exceeded", f"limitations[{i}] 非字符串、为空或超过 {MAX_LIMITATION_LEN} 字，已丢弃"))
                    continue
                limitations.append(text)
            if len(limitations) > MAX_LIMITATIONS:
                note(_issue("limit_exceeded", f"limitations 超过 {MAX_LIMITATIONS} 条，保留前 {MAX_LIMITATIONS} 条"))
                limitations = limitations[:MAX_LIMITATIONS]

    ref_order = ref_table.order()
    sections_raw = subject.get("sections")
    prepared: list[tuple[str, list[dict]]] = []  # (heading, 已通过校验的块，含已解析 refs)
    total_blocks = 0

    if not isinstance(sections_raw, list) or not sections_raw:
        note(_issue("missing_subject", "sections 缺失或不是非空数组"))
        sections_raw = []
    if len(sections_raw) > MAX_SECTIONS:
        extra_sections = [s for s in sections_raw[MAX_SECTIONS:] if isinstance(s, dict)]
        dropped += sum(len(s.get("blocks") or []) for s in extra_sections)
        note(_issue(
            "limit_exceeded",
            f"sections 超过 {MAX_SECTIONS} 节，后面的节整节暂不发布",
            block=[MAX_SECTIONS + 1, 0],
        ))
        sections_raw = sections_raw[:MAX_SECTIONS]

    for si, section in enumerate(sections_raw, start=1):
        if not isinstance(section, dict):
            dropped += 1
            note(_issue("missing_subject", f"sections[{si}] 不是对象，整节暂不发布", block=[si, 0]))
            continue
        blocks_raw = section.get("blocks")
        if not isinstance(blocks_raw, list):
            dropped += 1
            note(_issue("missing_subject", f"sections[{si}].blocks 不是数组，整节暂不发布", block=[si, 0]))
            continue

        heading = section.get("heading")
        if not isinstance(heading, str) or len(heading.strip()) > MAX_HEADING:
            note(_issue("missing_subject", f"sections[{si}].heading 缺失或过长，改用空标题", block=[si, 0]))
            heading = ""

        kept: list[dict] = []
        for bi, block in enumerate(blocks_raw, start=1):
            at = [si, bi]
            if total_blocks >= MAX_BLOCKS:
                dropped += 1
                note(_issue("limit_exceeded", "单文档内容块总数达到上限，后续块暂不发布", block=at))
                continue
            if not isinstance(block, dict):
                dropped += 1
                note(_issue("missing_subject", "内容块不是对象", block=at))
                continue
            bkind = block.get("kind")
            if bkind not in BLOCK_KINDS:
                dropped += 1
                note(_issue("missing_subject", f"块 kind「{bkind}」不属于四种角色", block=at))
                continue
            btext = block.get("text")
            if not isinstance(btext, str) or not btext.strip():
                dropped += 1
                note(_issue("missing_subject", "块 text 为空或不是字符串", block=at))
                continue
            btext = btext.strip()
            if len(btext) > MAX_BLOCK_TEXT:
                dropped += 1
                note(_issue("limit_exceeded", f"块 text 超过 {MAX_BLOCK_TEXT} 字", block=at))
                continue
            reason = _forbidden_link(btext)
            if reason:
                dropped += 1
                note(_issue("missing_subject", f"块 text {reason}", block=at))
                continue

            raw_refs = block.get("refs")
            if raw_refs is None:
                raw_refs = []
            tokens: list[str] = []
            if not isinstance(raw_refs, list) or any(not isinstance(r, str) for r in raw_refs):
                dropped += 1
                note(_issue("bad_ref", "块 refs 不是字符串数组", block=at), stage="evidence_resolution")
                continue
            for token in (r.strip() for r in raw_refs):
                if token and token not in tokens:
                    tokens.append(token)  # 只去空白与精确去重（docs/24 §3）
            if bkind in ("claim", "quote") and not tokens:
                dropped += 1
                note(_issue(
                    "bad_ref",
                    "claim/quote 没有引用原文依据，不作为有依据结论发布" if bkind == "claim"
                    else "quote 没有引用原文范围",
                    block=at,
                ), stage="evidence_resolution")
                continue
            unknown = [t for t in tokens if t not in ref_table]
            if unknown:
                # 混有有效与无效引用：整块暂不发布，也不留下“少了一份依据”的假象
                dropped += 1
                held_entries.extend(ref_table.get(t) for t in tokens if t in ref_table)
                note(_issue(
                    "bad_ref",
                    "引用了本次任务里没有给出的引用编号",
                    refs=unknown,
                    block=at,
                ), stage="evidence_resolution")
                continue

            resolved = [ref_table.get(t) for t in tokens]
            if bkind == "quote":
                if not _quote_is_verbatim(btext, resolved, ref_order, tokens):
                    seg_ids = sorted({s for e in resolved for s in e.segment_ids})
                    dropped += 1
                    held_entries.extend(resolved)
                    note(_issue(
                        "quote_not_verbatim",
                        "摘录未在被引原文中逐字出现",
                        refs=tokens,
                        segment_ids=seg_ids,
                        block=at,
                    ), stage="quote_verification")
                    continue
            total_blocks += 1
            kept.append({"kind": bkind, "text": btext, "tokens": tokens, "entries": resolved, "at": at})

        if kept or heading:
            prepared.append((heading, kept))

    extras = _task_extras(task, subject)

    # 只剩空架子：没有 claim/quote 就没有可发布的有依据内容（docs/24 §4）
    if not any(b["kind"] in ("claim", "quote") for _, sec in prepared for b in sec):
        return abort(_issue("empty_document", "模型输出只剩空架子：没有任何带依据的观点或摘录"))

    # 生成文档内 e 键并按出现顺序改写 refs
    references: dict[str, dict] = {}
    by_identity: dict[tuple, str] = {}
    sections_out: list[dict] = []
    used_identities: set[tuple] = set()
    for heading, blocks in prepared:
        out_blocks: list[dict] = []
        for block in blocks:
            e_keys: list[str] = []
            overflow = False
            for token, entry in zip(block["tokens"], block["entries"]):
                ident = entry.key()
                if ident not in by_identity:
                    if len(references) >= MAX_REFERENCES:
                        overflow = True
                        break
                    e_key = f"e{len(references) + 1}"
                    by_identity[ident] = e_key
                    references[e_key] = _reference_of(entry)
                e_keys.append(by_identity[ident])
                used_identities.add(ident)
            if overflow:
                dropped += 1
                held_entries.extend(block["entries"])
                note(_issue(
                    "limit_exceeded",
                    f"文档引用数已达 {MAX_REFERENCES} 条上限，该块暂不发布",
                    refs=block["tokens"],
                    block=block["at"],
                ))
                continue
            out_blocks.append({"kind": block["kind"], "text": block["text"], "refs": e_keys})
        if out_blocks:
            sections_out.append({"heading": heading, "blocks": out_blocks})

    if not any(b["kind"] in ("claim", "quote") for s in sections_out for b in s["blocks"]):
        return abort(_issue("empty_document", "有效内容在引用解析后全部落空"))

    # 摘要只在“被丢的块带走了文档里不再出现的依据”时清空：程序不猜摘要说了什么，
    # 但那块原文范围已从文档消失，摘要可能仍在描述它（docs/23 §5.3）。
    orphaned = {e.key() for e in held_entries} - used_identities
    if summary and orphaned:
        summary = ""
        stage_codes.add("summary")

    for gap in gaps:
        gap["refs"] = [_doc_ref_key(t, by_identity, ref_table) for t in gap["refs"]]
    for gap in _normalized_extra_gaps(extra_gaps):
        gaps.append(gap)

    # 只要有任何块被挡下、有任何缺口或阶段缺失，就不能当完整成功发布（docs/24 §4）
    state = "partial" if (dropped or gaps or missing_stages or stage_codes) else "complete"
    stages = _finalize_missing_stages(stage_codes, missing_stages)
    completeness = _completeness(
        state, missing_stages=stages, gaps=gaps, dropped_blocks=dropped, repair_calls=repair_calls
    )

    document = {
        "format_version": CONTENT_FORMAT_VERSION,
        "document_id": document_id,
        "kind": kind,
        "revision": revision,
        "created_at": created_at or _utc_now_iso(),
        "title": title,
        "summary": summary,
        "sections": sections_out,
        "references": references,
        "limitations": limitations,
        "completeness": completeness,
        "provenance": _provenance(
            task, recipe_version, item_id, source_revision, ref_table, input_documents, references
        ),
    }
    if len(_compact(document)) > MAX_DOC_BYTES:
        return abort(_issue("limit_exceeded", "文档序列化后超过 512 KiB 上限"))
    return document, AssemblyReport(
        document=document,
        errors=errors,
        completeness=completeness,
        dropped_blocks=dropped,
        repair_calls=repair_calls,
        task_extras=extras,
    )


def _reference_of(entry: RefEntry) -> dict:
    ref = {
        "item_id": entry.item_id,
        "source_revision": entry.source_revision,
        "segment_ids": list(entry.segment_ids),
        "source_text_hash": source_text_hash(entry.text),
    }
    if entry.locator:
        ref["locator"] = dict(entry.locator)
    return ref


def _doc_ref_key(token: str, by_identity: dict[tuple, str], ref_table: RefTable) -> str:
    """gaps 里的引用尽量写成文档内 `e` 键，读者与界面才能据此跳转；解析不出留原词。"""
    entry = ref_table.get(token)
    if entry is not None:
        e_key = by_identity.get(entry.key())
        if e_key:
            return e_key
    return token


def _quote_is_verbatim(
    text: str,
    entries: list[RefEntry],
    order: dict[str, int],
    tokens: list[str],
) -> bool:
    """quote 逐字校验（docs/24 §2）。

    单条引用：在被引范围原文中查找。多条引用只有在同一来源版本、且在任务引用表里
    彼此相邻（相邻阅读单元拼接等于真实原文顺序）时才按拼接比对，避免把不相邻的话
    拼成一句；不满足拼接条件时要求至少一条引用自身包含该摘录。
    """
    target = normalize_for_quote(text)
    if not target:
        return False
    cited = [(order[t], e) for t, e in zip(tokens, entries) if e is not None and t in order]
    if len(cited) == 1:
        return target in normalize_for_quote(cited[0][1].text)
    cited.sort(key=lambda item: item[0])
    positions = [pos for pos, _ in cited]
    same_source = len({e.key()[:2] for _, e in cited}) == 1
    if same_source and positions == list(range(positions[0], positions[0] + len(positions))):
        joined = "\n".join(e.text for _, e in cited)
        return target in normalize_for_quote(joined)
    return any(target in normalize_for_quote(e.text) for _, e in cited)


def _normalized_extra_gaps(extra: list[dict] | None) -> list[dict]:
    out: list[dict] = []
    for gap in extra or []:
        if not isinstance(gap, dict) or not isinstance(gap.get("code"), str) or not gap.get("code"):
            continue
        out.append(_issue(
            gap["code"],
            gap.get("message") if isinstance(gap.get("message"), str) and gap["message"] else "部分内容缺失",
            refs=[r for r in (gap.get("refs") or []) if isinstance(r, str)],
            segment_ids=[s for s in (gap.get("segment_ids") or []) if isinstance(s, str)],
            block=[n for n in (gap.get("block") or []) if isinstance(n, int)],
        ))
    return out


def _provenance(
    task: str,
    recipe_version: str,
    item_id: str,
    source_revision: int,
    ref_table: RefTable,
    input_documents: list[dict] | None,
    references: dict,
) -> dict:
    revisions: list[dict] = []
    seen: set[tuple] = set()

    def add(item, rev) -> None:
        if not isinstance(item, str) or not item or not isinstance(rev, int) or isinstance(rev, bool):
            return
        if (item, rev) in seen:
            return
        seen.add((item, rev))
        revisions.append({"item_id": item, "source_revision": rev})

    add(item_id, source_revision)
    for _, entry in ref_table.entries():
        add(entry.item_id, entry.source_revision)
    for ref in references.values():
        add(ref["item_id"], ref["source_revision"])

    docs: list[dict] = []
    for doc in input_documents or []:
        if not isinstance(doc, dict):
            continue
        doc_id = doc.get("document_id")
        if not isinstance(doc_id, str) or not doc_id:
            continue
        docs.append({
            "document_id": doc_id,
            "kind": doc.get("kind") if doc.get("kind") in DOC_KINDS else "digest",
            "revision": doc.get("revision") if isinstance(doc.get("revision"), int) else 1,
        })

    return {
        "recipe_version": recipe_version,
        "task": task,
        "input_documents": docs,
        "source_revisions": revisions,
    }


# ---------------------------------------------------------------- 校验


def validate_content_document(doc: dict) -> list[str]:
    """校验已组装文档（docs/24 §1、§2）；返回中文错误列表，空列表表示通过。"""
    errors: list[str] = []
    if not isinstance(doc, dict):
        return ["content.json 必须是对象"]

    if doc.get("format_version") != CONTENT_FORMAT_VERSION:
        errors.append(f"format_version 必须是 {CONTENT_FORMAT_VERSION}")
    if not _clean_str(doc.get("document_id"), 200):
        errors.append("document_id 必须是非空字符串")
    if doc.get("kind") not in DOC_KINDS:
        errors.append("kind 必须是 digest|knowledge|synthesis")
    if not isinstance(doc.get("revision"), int) or isinstance(doc.get("revision"), bool) or doc["revision"] < 1:
        errors.append("revision 必须是正整数")
    if not isinstance(doc.get("created_at"), str) or not ISO8601_RE.match(doc["created_at"]):
        errors.append("created_at 必须是 ISO-8601 UTC 字符串")
    if not isinstance(doc.get("title"), str) or not doc["title"] or len(doc["title"]) > MAX_TITLE:
        errors.append(f"title 必须是非空且不超过 {MAX_TITLE} 字的字符串")
    if not isinstance(doc.get("summary"), str) or len(doc["summary"]) > MAX_SUMMARY:
        errors.append(f"summary 必须是字符串且不超过 {MAX_SUMMARY} 字（可以为空）")

    references = doc.get("references")
    if not isinstance(references, dict):
        errors.append("references 必须是对象")
        references = {}
    elif len(references) > MAX_REFERENCES:
        errors.append(f"references 超过 {MAX_REFERENCES} 条")
    else:
        for key, ref in references.items():
            if not E_KEY_RE.match(key):
                errors.append(f"references 键 {key} 必须是 e + 数字")
            errors += [f"references.{key}.{m}" for m in _ref_errors(ref)]

    sections = doc.get("sections")
    if not isinstance(sections, list) or not sections:
        errors.append("sections 必须是非空数组")
        sections = []
    elif len(sections) > MAX_SECTIONS:
        errors.append(f"sections 超过 {MAX_SECTIONS} 节")

    total_blocks = 0
    for i, section in enumerate(sections):
        if not isinstance(section, dict):
            errors.append(f"sections[{i}] 必须是对象")
            continue
        heading = section.get("heading")
        if not isinstance(heading, str) or len(heading) > MAX_HEADING:
            errors.append(f"sections[{i}].heading 必须是字符串且不超过 {MAX_HEADING} 字")
        blocks = section.get("blocks")
        if not isinstance(blocks, list):
            errors.append(f"sections[{i}].blocks 必须是数组")
            continue
        for j, block in enumerate(blocks):
            total_blocks += 1
            field = f"sections[{i}].blocks[{j}]"
            if not isinstance(block, dict):
                errors.append(f"{field} 必须是对象")
                continue
            if block.get("kind") not in BLOCK_KINDS:
                errors.append(f"{field}.kind 必须是 {'|'.join(BLOCK_KINDS)}")
            text = block.get("text")
            if not isinstance(text, str) or not text.strip():
                errors.append(f"{field}.text 必须是非空字符串")
            elif len(text) > MAX_BLOCK_TEXT:
                errors.append(f"{field}.text 超过 {MAX_BLOCK_TEXT} 字")
            else:
                reason = _forbidden_link(text)
                if reason:
                    errors.append(f"{field}.text {reason}")
            refs = block.get("refs")
            if not isinstance(refs, list) or any(not isinstance(r, str) for r in refs):
                errors.append(f"{field}.refs 必须是字符串数组")
                continue
            if block.get("kind") in ("claim", "quote") and not refs:
                errors.append(f"{field} 为 {block.get('kind')}，必须有引用")
            for ref_key in refs:
                if not E_KEY_RE.match(ref_key):
                    errors.append(f"{field}.refs 键 {ref_key} 必须是文档内 e 引用")
                elif ref_key not in references:
                    errors.append(f"{field}.refs 引用了不存在的 {ref_key}")

    if total_blocks > MAX_BLOCKS:
        errors.append(f"内容块总数超过 {MAX_BLOCKS}")
    # 允许 references 里有未被引用的键：插件部分采纳会删块（docs/23 §6.3），
    # 重编号 e 键会让同一文档版本内的定位失效。

    limitations = doc.get("limitations")
    if not isinstance(limitations, list) or len(limitations) > MAX_LIMITATIONS:
        errors.append(f"limitations 必须是数组且不超过 {MAX_LIMITATIONS} 条")
    else:
        for i, item in enumerate(limitations):
            if not isinstance(item, str) or not item.strip() or len(item) > MAX_LIMITATION_LEN:
                errors.append(f"limitations[{i}] 必须是非空且不超过 {MAX_LIMITATION_LEN} 字的字符串")

    errors += _completeness_errors(doc.get("completeness"))
    errors += _provenance_errors(doc.get("provenance"))

    if not errors and len(_compact(doc)) > MAX_DOC_BYTES:
        errors.append(f"文档序列化后超过 {MAX_DOC_BYTES} 字节")
    return errors


def _ref_errors(ref) -> list[str]:
    if not isinstance(ref, dict):
        return ["必须是对象"]
    out: list[str] = []
    if not _clean_str(ref.get("item_id"), 200):
        out.append("item_id 必须是非空字符串")
    rev = ref.get("source_revision")
    if not isinstance(rev, int) or isinstance(rev, bool) or rev < 1:
        out.append("source_revision 必须是正整数")
    segment_ids = ref.get("segment_ids")
    if not isinstance(segment_ids, list) or not segment_ids:
        out.append("segment_ids 必须是非空数组")
    elif any(not isinstance(s, str) or not SEGMENT_ID_RE.match(s) for s in segment_ids):
        out.append("segment_ids 必须是 s + 至少 4 位数字")
    if not isinstance(ref.get("source_text_hash"), str) or not SHA256_RE.match(ref["source_text_hash"]):
        out.append("source_text_hash 必须是 sha256 十六进制")
    locator = ref.get("locator")
    if locator is not None:
        if not isinstance(locator, dict) or locator.get("kind") not in LOCATOR_KINDS:
            out.append("locator.kind 必须是 time|paragraph|line")
        elif locator["kind"] == "time":
            if not all(isinstance(locator.get(k), int) for k in ("start_ms", "end_ms")):
                out.append("locator kind=time 需要 start_ms/end_ms 整数")
        elif locator["kind"] == "paragraph":
            if not _clean_str(locator.get("paragraph_id"), 100):
                out.append("locator kind=paragraph 需要 paragraph_id")
        elif not isinstance(locator.get("line_no"), int):
            out.append("locator kind=line 需要 line_no 整数")
    return out


def _completeness_errors(value) -> list[str]:
    if not isinstance(value, dict):
        return ["completeness 必须是对象"]
    out: list[str] = []
    if value.get("state") not in COMPLETENESS_STATES:
        out.append("completeness.state 必须是 complete|partial|failed")
    stages = value.get("missing_stages")
    if not isinstance(stages, list) or any(
        not isinstance(s, str) or not (s in MISSING_STAGE_ORDER or re.match(r"^chunk:\d{1,4}$", s))
        for s in stages
    ):
        out.append("completeness.missing_stages 含非法机器码")
    gaps = value.get("gaps")
    if not isinstance(gaps, list):
        out.append("completeness.gaps 必须是数组")
    else:
        for i, gap in enumerate(gaps):
            if not isinstance(gap, dict):
                out.append(f"completeness.gaps[{i}] 必须是对象")
                continue
            if gap.get("code") not in GAP_CODES:
                out.append(f"completeness.gaps[{i}].code 非法")
            if not _clean_str(gap.get("message"), MAX_LIMITATION_LEN):
                out.append(f"completeness.gaps[{i}].message 必须是给用户看的非空文案")
            for key in ("refs", "segment_ids"):
                items = gap.get(key)
                if not isinstance(items, list) or any(not isinstance(v, str) for v in items):
                    out.append(f"completeness.gaps[{i}].{key} 必须是字符串数组")
    for key in ("dropped_blocks", "repair_calls"):
        n = value.get(key)
        if not isinstance(n, int) or isinstance(n, bool) or n < 0:
            out.append(f"completeness.{key} 必须是非负整数")
    return out


def _provenance_errors(value) -> list[str]:
    if not isinstance(value, dict):
        return ["provenance 必须是对象"]
    out: list[str] = []
    if not _clean_str(value.get("recipe_version"), 100):
        out.append("provenance.recipe_version 必须是非空字符串")
    if value.get("task") not in TASKS:
        out.append("provenance.task 必须是 digest|knowledge_fusion|share_synthesis")
    docs = value.get("input_documents")
    if not isinstance(docs, list):
        out.append("provenance.input_documents 必须是数组")
    else:
        for i, doc in enumerate(docs):
            if not isinstance(doc, dict) or not _clean_str(doc.get("document_id"), 200) \
                    or doc.get("kind") not in DOC_KINDS or not isinstance(doc.get("revision"), int) \
                    or isinstance(doc.get("revision"), bool):
                out.append(f"provenance.input_documents[{i}] 必须是 document_id/kind/revision")
    revisions = value.get("source_revisions")
    if not isinstance(revisions, list):
        out.append("provenance.source_revisions 必须是数组")
    else:
        for i, rev in enumerate(revisions):
            if not isinstance(rev, dict) or not _clean_str(rev.get("item_id"), 200) \
                    or not isinstance(rev.get("source_revision"), int) or isinstance(rev.get("source_revision"), bool):
                out.append(f"provenance.source_revisions[{i}] 必须是 item_id/source_revision")
    return out


# ---------------------------------------------------------------- 渲染与导出


def ref_table_from_document(doc: dict) -> dict:
    """文档内 `e` 键 → 原文范围（docs/24 §2 的 ref 形状）；插件与迁移复用。

    返回深拷贝：调用方常常要在内存里改写范围做差异预览，不能污染已解析的文档。
    """
    references = doc.get("references") if isinstance(doc, dict) else None
    return {key: copy.deepcopy(value) for key, value in (references or {}).items()}


def _clock(ms: int) -> str:
    total = max(0, int(ms)) // 1000
    return f"{total // 60:02d}:{total % 60:02d}"


def _ref_positions(ref: dict) -> str | None:
    """界面显示自然位置：只给时间范围，绝不显示 R1/e1/s0001（docs/24 §2）。"""
    locator = ref.get("locator")
    if isinstance(locator, dict) and locator.get("kind") == "time":
        start, end = locator.get("start_ms"), locator.get("end_ms")
        if isinstance(start, int) and isinstance(end, int):
            return f"{_clock(start)}–{_clock(end)}"
    return None


def render_content_markdown(doc: dict) -> str:
    """v3 文档 → 可读 Markdown（preview.md 与 Digest 笔记正文共用）。

    只呈现内容与自然定位；机器信息（编号、哈希、来源版本）不进正文，
    缺口由程序文案说明，不猜模型语义。
    """
    lines: list[str] = []
    title = _clean_str(doc.get("title"), MAX_TITLE)
    lines.append(f"# {title}" if title else "# 未命名内容")
    lines.append("")

    summary = doc.get("summary") if isinstance(doc.get("summary"), str) else ""
    if summary.strip():
        lines.append(summary.strip())
        lines.append("")

    completeness = doc.get("completeness") if isinstance(doc.get("completeness"), dict) else {}
    references = doc.get("references") if isinstance(doc.get("references"), dict) else {}
    if completeness.get("state") != "complete":
        if not summary.strip():
            lines.append("> 本次加工只得到部分内容，完整摘要待重新整理后生成。")
            lines.append("")
        for gap in completeness.get("gaps") or []:
            if isinstance(gap, dict) and isinstance(gap.get("message"), str) and gap["message"]:
                lines.append(f"> 缺口：{gap['message']}")
        if completeness.get("gaps"):
            lines.append("")

    for section in doc.get("sections") or []:
        if not isinstance(section, dict):
            continue
        heading = section.get("heading")
        if isinstance(heading, str) and heading.strip():
            lines.append(f"## {heading.strip()}")
            lines.append("")
        for block in section.get("blocks") or []:
            if not isinstance(block, dict):
                continue
            text = (block.get("text") or "").strip()
            if not text:
                continue
            kind = block.get("kind")
            positions = "；".join(
                p for p in (
                    _ref_positions(references[key]) if isinstance(references.get(key), dict) else None
                    for key in (block.get("refs") or []) if isinstance(key, str)
                ) if p
            )
            suffix = f"（原文 {positions}）" if positions else ""
            if kind == "quote":
                lines.append(f"> {text}{suffix}")
            elif kind == "suggestion":
                lines.append(f"- AI 建议/待验证：{text}")
            elif kind == "claim":
                lines.append(f"- {text}{suffix}")
            else:
                lines.append(text)
        lines.append("")

    limitations = [t for t in (doc.get("limitations") or []) if isinstance(t, str) and t.strip()]
    if limitations:
        lines.append("### 局限")
        lines.append("")
        lines.extend(f"- {t.strip()}" for t in limitations)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
