"""分享作品的 Schema、来源快照与校验（docs/20 §5、§6.2、§6.3）。

原则：
- 快照固定当次输入：创建任务时把每条材料的 item_id／source_revision／正文片段
  钉死，生成过程中不再读「最新版本」。
- 模型只用逻辑标识（source_key、seg0001、asset_id），拿不到 storage_key 与真实路径。
- 跨篇引用必须带命名空间（s1:seg0001）：c0001 在不同材料里会重复，不能单独作全局 ID。
- 校验只认代码规则：ID 是否存在、摘录是否逐字来自被引片段、每篇是否交代用途。
  语义忠实仍需内容核对，不把 ID 通过校验当作结论正确。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .analysis import normalize_for_quote

SCHEMA_VERSION = "1.0"
SHARE_RECIPE_VERSION = "share-v1"
PROMPT_VERSION = "share-prompt-v1"

# 对象引用角色（docs/20 §11.4）：默认 private，只有白名单产物可被公开路由读取
ARTIFACT_ROLES = frozenset({
    "input_snapshot", "conversation_message", "brief", "clarification_round",
    "synthesis", "page_source", "html", "public_references", "asset",
    "check_report", "screenshot",
})
PUBLIC_ARTIFACT_ROLES = frozenset({"html", "public_references"})

CLAIM_KINDS = ("source_claim", "synthesis", "illustration")
NEXT_ACTIONS = ("ask_user", "confirm_brief")
CONFIRMATION_KINDS = ("confirm", "delegate_preferences", "explicit_modify")
BRIEF_FIELDS = (
    "goal", "audience", "content_priorities", "presentation_preferences",
    "must_keep", "must_avoid", "assumptions",
)

SEGMENT_ID_RE = re.compile(r"^seg\d{4}$")
CITATION_RE = re.compile(r"^(s\d{1,3}):([a-z0-9_]+):?(seg\d{4})?$")
SECTION_ID_RE = re.compile(r"^sec\d{1,3}$")
CLAIM_ID_RE = re.compile(r"^k\d{1,4}$")
REF_ID_RE = re.compile(r"^ref\d{1,4}$")
OPTION_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


@dataclass
class SourceSpec:
    """一条材料在快照中的固定内容（由 workers/share.py 从库里读出）。"""

    source_key: str
    item_id: str
    source_revision: int
    title: str
    coverage: str
    segments: list[dict] = field(default_factory=list)
    claims: list[dict] = field(default_factory=list)
    assets: list[dict] = field(default_factory=list)
    bundle_revision: int | None = None
    author: str | None = None
    canonical_url: str | None = None
    source_label: str | None = None


def citation_id(source_key: str, segment_id: str) -> str:
    return f"{source_key}:segment:{segment_id}"


def parse_citation(value: str) -> tuple[str, str] | None:
    """解析 `s1:segment:seg0001`；返回 (source_key, segment_id) 或 None。"""
    if not isinstance(value, str):
        return None
    match = CITATION_RE.match(value.strip())
    if not match:
        return None
    return match.group(1), match.group(3) or ""


def build_source_pack(specs: list[SourceSpec], *, include_storage: bool = False) -> dict:
    """私有 source_pack.json（docs/20 §5.1）。

    include_storage 只用于编排器自己的 input_manifest 侧车；交给模型的快照不带
    storage_key，模型不能决定对象路径。
    """
    sources = []
    for spec in specs:
        entry: dict = {
            "source_key": spec.source_key,
            "item_id": spec.item_id,
            "source_revision": spec.source_revision,
            "bundle_revision": spec.bundle_revision,
            "title": spec.title,
            "author": spec.author,
            "canonical_url": spec.canonical_url,
            "source_label": spec.source_label,
            "coverage": spec.coverage,
            "segments": [
                {"id": s["id"], "text": s["text"]} for s in spec.segments
            ],
            "claims": [
                {"id": c["id"], "text": c["text"], "segment_ids": list(c.get("segment_ids") or [])}
                for c in spec.claims
            ],
            "assets": [
                {k: v for k, v in a.items() if include_storage or k != "storage_key"}
                for a in spec.assets
            ],
        }
        sources.append(entry)
    return {"schema_version": SCHEMA_VERSION, "sources": sources}


def pack_segment_texts(pack: dict) -> dict[str, dict[str, str]]:
    return {
        s["source_key"]: {seg["id"]: seg.get("text") or "" for seg in s.get("segments") or []}
        for s in pack.get("sources") or []
    }


def pack_source_keys(pack: dict) -> set[str]:
    return {s["source_key"] for s in pack.get("sources") or []}


def validate_source_pack(pack: dict) -> list[str]:
    errors: list[str] = []
    if pack.get("schema_version") != SCHEMA_VERSION:
        errors.append("source_pack.schema_version 必须是 1.0")
    sources = pack.get("sources")
    if not isinstance(sources, list) or not sources:
        errors.append("source_pack.sources 必须是非空数组")
        return errors
    seen: set[str] = set()
    for i, source in enumerate(sources):
        if not isinstance(source, dict):
            errors.append(f"sources[{i}] 必须是对象")
            continue
        key = source.get("source_key")
        if not isinstance(key, str) or not re.match(r"^s\d{1,3}$", key):
            errors.append(f"sources[{i}].source_key 必须是 s + 数字")
        elif key in seen:
            errors.append(f"source_key 重复：{key}")
        else:
            seen.add(key)
        if not isinstance(source.get("item_id"), str) or not source.get("item_id"):
            errors.append(f"sources[{i}].item_id 必填")
        if not isinstance(source.get("source_revision"), int) or source["source_revision"] < 1:
            errors.append(f"sources[{i}].source_revision 必须是正整数")
        segs = source.get("segments")
        if not isinstance(segs, list) or not segs:
            errors.append(f"sources[{i}] 没有可读片段：材料不可用时应在选择阶段就提示")
            continue
        seg_ids: set[str] = set()
        for j, seg in enumerate(segs):
            sid = seg.get("id") if isinstance(seg, dict) else None
            if not isinstance(sid, str) or not SEGMENT_ID_RE.match(sid):
                errors.append(f"sources[{i}].segments[{j}].id 必须是 seg + 4 位数字")
                continue
            if sid in seg_ids:
                errors.append(f"sources[{i}].segments 片段 ID 重复：{sid}")
            seg_ids.add(sid)
            if not isinstance(seg.get("text"), str) or not seg["text"].strip():
                errors.append(f"sources[{i}].segments[{j}].text 必须非空")
        for j, claim in enumerate(source.get("claims") or []):
            for sid in (claim.get("segment_ids") or []) if isinstance(claim, dict) else []:
                if sid not in seg_ids:
                    errors.append(f"sources[{i}].claims[{j}] 引用了不存在的片段 {sid}")
    return errors


# ---- 需求对话（docs/20 §6.1.1）----


def normalize_brief(raw: dict | None) -> dict:
    """需求摘要：字符串字段归一为 None/文本，数组字段归一为字符串列表。"""
    raw = raw if isinstance(raw, dict) else {}
    brief: dict = {}
    for name in BRIEF_FIELDS:
        value = raw.get(name)
        if name in ("goal", "audience"):
            brief[name] = value.strip() if isinstance(value, str) and value.strip() else None
        else:
            brief[name] = [v.strip() for v in (value or []) if isinstance(v, str) and v.strip()][:20] \
                if isinstance(value, list) else []
    return brief


def validate_clarification(doc: dict, *, max_questions: int = 3) -> list[str]:
    errors: list[str] = []
    if not isinstance(doc, dict):
        return ["clarification 顶层必须是对象"]
    if doc.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version 必须是 1.0")
    understanding = doc.get("understanding")
    if not isinstance(understanding, str) or not understanding.strip():
        errors.append("understanding 必须是一句简短的中文理解")
    elif len(understanding) > 500:
        errors.append("understanding 超过 500 字符")
    if not isinstance(doc.get("brief"), dict):
        errors.append("brief 必须是对象（未知项用 null 或空数组，不要省略）")
    action = doc.get("next_action")
    if action not in NEXT_ACTIONS:
        errors.append("next_action 只允许 ask_user／confirm_brief（模型不能自行宣布用户已确认）")
    questions = doc.get("questions")
    if not isinstance(questions, list):
        errors.append("questions 必须是数组（可以为空）")
        return errors
    if len(questions) > max_questions:
        errors.append(f"questions 每轮最多 {max_questions} 个")
    if action == "confirm_brief" and questions:
        errors.append("next_action=confirm_brief 时 questions 必须为空")
    seen_q: set[str] = set()
    for i, q in enumerate(questions):
        if not isinstance(q, dict):
            errors.append(f"questions[{i}] 必须是对象")
            continue
        qid = q.get("id")
        if not isinstance(qid, str) or not qid.strip():
            errors.append(f"questions[{i}].id 必填")
        elif qid in seen_q:
            errors.append(f"questions[{i}].id 在本轮内重复：{qid}")
        else:
            seen_q.add(qid)
        text = q.get("text")
        if not isinstance(text, str) or not text.strip():
            errors.append(f"questions[{i}].text 必须是具体问题")
        elif len(text) > 400:
            errors.append(f"questions[{i}].text 超过 400 字符")
        if q.get("reason") is not None and not isinstance(q.get("reason"), str):
            errors.append(f"questions[{i}].reason 必须是字符串或 null")
        if not isinstance(q.get("required_for_generation"), bool):
            errors.append(f"questions[{i}].required_for_generation 必须是布尔值")
        options = q.get("options")
        if options is None:
            continue
        if not isinstance(options, list) or len(options) > 6:
            errors.append(f"questions[{i}].options 最多 6 项")
            continue
        seen_o: set[str] = set()
        for j, opt in enumerate(options):
            oid = opt.get("id") if isinstance(opt, dict) else None
            label = opt.get("label") if isinstance(opt, dict) else None
            if not isinstance(oid, str) or not OPTION_ID_RE.match(oid or ""):
                errors.append(f"questions[{i}].options[{j}].id 必须是短横线小写标识")
                continue
            if oid in seen_o:
                errors.append(f"questions[{i}].options 内 ID 重复：{oid}")
            seen_o.add(oid)
            if not isinstance(label, str) or not label.strip() or len(label) > 80:
                errors.append(f"questions[{i}].options[{j}].label 必须是 1..80 字符")
    return errors


# ---- 整合稿（docs/20 §6.2）----


def validate_synthesis(doc: dict, *, pack: dict) -> list[str]:
    """校验整合稿：引用必须能在固定快照里定位，每篇材料都要交代用途。"""
    errors: list[str] = []
    if not isinstance(doc, dict):
        return ["synthesis 顶层必须是对象"]
    if doc.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version 必须是 1.0")
    texts = pack_segment_texts(pack)
    sources = pack_source_keys(pack)

    for name, limit in (("title", 200), ("reader_goal", 500)):
        value = doc.get(name)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{name} 必须是简短的非空文本")
        elif len(value) > limit:
            errors.append(f"{name} 超过 {limit} 字符")

    sections = doc.get("sections")
    if not isinstance(sections, list) or not sections:
        errors.append("sections 必须是非空数组（结构自由，但不允许整篇没有内容）")
        sections = []
    seen_sec: set[str] = set()
    for i, sec in enumerate(sections):
        if not isinstance(sec, dict):
            errors.append(f"sections[{i}] 必须是对象")
            continue
        sid = sec.get("id")
        if not isinstance(sid, str) or not SECTION_ID_RE.match(sid):
            errors.append(f"sections[{i}].id 必须是 sec + 数字")
        elif sid in seen_sec:
            errors.append(f"sections[{i}].id 重复：{sid}")
        else:
            seen_sec.add(sid)
        body = sec.get("body")
        if not isinstance(body, str) or not body.strip():
            errors.append(f"sections[{i}].body 必须是完整正文")
        if not isinstance(sec.get("heading"), str) or not sec["heading"].strip():
            errors.append(f"sections[{i}].heading 必填")

    claims = doc.get("claims")
    if not isinstance(claims, list):
        errors.append("claims 必须是数组")
        claims = []
    seen_claim: set[str] = set()
    for i, claim in enumerate(claims):
        if not isinstance(claim, dict):
            errors.append(f"claims[{i}] 必须是对象")
            continue
        cid = claim.get("id")
        if not isinstance(cid, str) or not CLAIM_ID_RE.match(cid):
            errors.append(f"claims[{i}].id 必须是 k + 数字")
        elif cid in seen_claim:
            errors.append(f"claims[{i}].id 重复：{cid}")
        else:
            seen_claim.add(cid)
        if claim.get("kind") not in CLAIM_KINDS:
            errors.append(f"claims[{i}].kind 只允许 {'/'.join(CLAIM_KINDS)}")
        text = claim.get("text")
        if not isinstance(text, str) or not text.strip():
            errors.append(f"claims[{i}].text 必填")
        elif len(text) > 2000:
            errors.append(f"claims[{i}].text 超过 2000 字符")
        errors += _citation_errors(claim.get("citations"), field=f"claims[{i}]",
                                   texts=texts, sources=sources,
                                   required=claim.get("kind") in ("source_claim", "synthesis"))
        if claim.get("conditions") is not None and not isinstance(claim.get("conditions"), str):
            errors.append(f"claims[{i}].conditions 必须是字符串或 null")

    usage = doc.get("source_usage")
    if not isinstance(usage, list):
        errors.append("source_usage 必须是数组：每篇材料都要交代用途")
    else:
        covered: set[str] = set()
        for i, item in enumerate(usage):
            key = item.get("source_key") if isinstance(item, dict) else None
            if key not in sources:
                errors.append(f"source_usage[{i}].source_key 不在快照里：{key}")
                continue
            if key in covered:
                errors.append(f"source_usage[{i}] 重复交代同一篇：{key}")
            covered.add(key)
            if not isinstance(item.get("use"), str) or not item["use"].strip():
                errors.append(f"source_usage[{i}].use 必须说明这篇被怎么用了")
            omitted = item.get("omitted_reason")
            if omitted is not None and (not isinstance(omitted, str) or len(omitted) > 500):
                errors.append(f"source_usage[{i}].omitted_reason 过长")
        missing = sources - covered
        if missing:
            errors.append(f"source_usage 没有交代：{', '.join(sorted(missing))}")

    intents = doc.get("visual_intents")
    if not isinstance(intents, list):
        errors.append("visual_intents 必须是数组（可以为空：没有必要时以文字为主）")
    else:
        for i, intent in enumerate(intents):
            if not isinstance(intent, dict):
                errors.append(f"visual_intents[{i}] 必须是对象")
                continue
            if not isinstance(intent.get("question"), str) or not intent["question"].strip():
                errors.append(f"visual_intents[{i}].question 必须说明这张图解释什么")
            if not isinstance(intent.get("interactive"), bool):
                errors.append(f"visual_intents[{i}].interactive 必须是布尔值")
            errors += _citation_errors(intent.get("citations"), field=f"visual_intents[{i}]",
                                       texts=texts, sources=sources, required=False)

    refs = doc.get("public_references")
    if not isinstance(refs, list):
        errors.append("public_references 必须是数组")
    else:
        seen_ref: set[str] = set()
        for i, ref in enumerate(refs):
            if not isinstance(ref, dict):
                errors.append(f"public_references[{i}] 必须是对象")
                continue
            rid = ref.get("ref_id")
            if not isinstance(rid, str) or not REF_ID_RE.match(rid):
                errors.append(f"public_references[{i}].ref_id 必须是 ref + 数字")
            elif rid in seen_ref:
                errors.append(f"public_references[{i}].ref_id 重复：{rid}")
            else:
                seen_ref.add(rid)
            if ref.get("source_key") not in sources:
                errors.append(f"public_references[{i}].source_key 不在快照里")
            if not isinstance(ref.get("title"), str) or not ref["title"].strip():
                errors.append(f"public_references[{i}].title 必填")
            url = ref.get("url")
            if url is not None and (not isinstance(url, str) or not url.startswith(("http://", "https://"))):
                errors.append(f"public_references[{i}].url 必须是 http(s) 链接或 null")
            citation = ref.get("citation")
            if citation is not None:
                errors += _citation_errors([citation], field=f"public_references[{i}]",
                                           texts=texts, sources=sources, required=True)
                quote = ref.get("quote")
                if isinstance(quote, str) and quote.strip():
                    parsed = parse_citation(citation)
                    original = ""
                    if parsed:
                        original = texts.get(parsed[0], {}).get(parsed[1], "")
                    if normalize_for_quote(quote) not in normalize_for_quote(original):
                        errors.append(f"public_references[{i}].quote 不是被引片段里的原文")

    limitations = doc.get("limitations")
    if not isinstance(limitations, list) or len(limitations) > 12 or not all(
        isinstance(x, str) and 0 < len(x) <= 500 for x in limitations
    ):
        errors.append("limitations 必须是不超过 12 条的字符串数组")
    return errors


def _citation_errors(value, *, field: str, texts: dict, sources: set[str], required: bool) -> list[str]:
    errors: list[str] = []
    if value is None:
        if required:
            errors.append(f"{field} 缺少 citations：来源观点与综合归纳都要给出依据")
        return errors
    if not isinstance(value, list) or len(value) > 30:
        errors.append(f"{field}.citations 必须是不超过 30 项的数组")
        return errors
    for cit in value:
        parsed = parse_citation(cit)
        if not parsed:
            errors.append(f"{field}.citations 里的 {cit!r} 不是 s<n>:segment:seg<n> 形式")
            continue
        source_key, segment_id = parsed
        if source_key not in sources:
            errors.append(f"{field}.citations 引用了快照外的材料：{source_key}")
        elif segment_id not in texts.get(source_key, {}):
            errors.append(f"{field}.citations 引用了不存在的片段：{cit}")
    return errors


# ---- 页面源文件（docs/20 §6.3）----

PAGE_SOURCE_FIELDS = ("schema_version", "title", "html_body", "css", "javascript",
                      "dependencies", "asset_ids", "reference_ids", "interactions")
DENIED_JS_MARKERS = (
    "fetch(", "XMLHttpRequest", "WebSocket", "EventSource", "import(", "eval(",
    "new Function", "document.cookie", "localStorage", "sessionStorage",
    "navigator.serviceWorker", "sendBeacon", "window.open", "location.href",
    "createElement('script'", 'createElement("script"', "postMessage",
)
DENIED_HTML_MARKERS = ("<script", "<iframe", "<object", "<embed", "<base", "javascript:")


def validate_page_source(doc: dict, *, allowed_imports: set[str], known_asset_ids: set[str],
                         known_reference_ids: set[str], max_html_chars: int = 400_000,
                         max_css_chars: int = 200_000, max_js_chars: int = 300_000,
                         max_interactions: int = 8) -> list[str]:
    """服务端预检：只挡明显违约的输入，真正的语法与资源检查在 runner 里做。"""
    errors: list[str] = []
    if not isinstance(doc, dict):
        return ["page_source 顶层必须是对象"]
    for key in doc:
        if key not in PAGE_SOURCE_FIELDS:
            errors.append(f"page_source 不允许字段 {key}（构建配置与服务器代码由系统固定）")
    if doc.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version 必须是 1.0")
    for name, limit in (("title", 200), ("html_body", max_html_chars), ("css", max_css_chars),
                        ("javascript", max_js_chars)):
        value = doc.get(name)
        if not isinstance(value, str):
            errors.append(f"{name} 必须是字符串")
            continue
        if len(value) > limit:
            errors.append(f"{name} 超过 {limit} 字符，请精简后重试")
    if isinstance(doc.get("html_body"), str) and not doc["html_body"].strip():
        errors.append("html_body 不能为空")
    html = doc.get("html_body") if isinstance(doc.get("html_body"), str) else ""
    lowered = html.lower()
    for marker in DENIED_HTML_MARKERS:
        if marker in lowered:
            errors.append(f"html_body 出现 {marker}：交互写在 javascript 字段，用事件监听注册")
    js = doc.get("javascript") if isinstance(doc.get("javascript"), str) else ""
    for marker in DENIED_JS_MARKERS:
        if marker in js:
            errors.append(f"javascript 出现 {marker}：页面不能联网、导航或执行任意代码")
    deps = doc.get("dependencies")
    if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
        errors.append("dependencies 必须是字符串数组")
    else:
        for dep in deps:
            if dep not in allowed_imports:
                errors.append(f"dependencies 含未登记的库：{dep}（只用运行手册列出的库）")
    for name, known in (("asset_ids", known_asset_ids), ("reference_ids", known_reference_ids)):
        value = doc.get(name, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            errors.append(f"{name} 必须是字符串数组")
            continue
        for v in value:
            if v not in known:
                errors.append(f"{name} 引用了未知 ID：{v}")
    interactions = doc.get("interactions", [])
    if not isinstance(interactions, list):
        errors.append("interactions 必须是数组")
    elif len(interactions) > max_interactions:
        errors.append(f"interactions 最多 {max_interactions} 项")
    return errors


def public_reference_view(synthesis: dict, pack: dict) -> list[dict]:
    """公开来源清单：只含最终页面需要展示的来源，不含原文包与私人备注（docs/20 §5.4）。"""
    by_source = {s["source_key"]: s for s in pack.get("sources") or []}
    out: list[dict] = []
    for ref in synthesis.get("public_references") or []:
        source = by_source.get(ref.get("source_key")) or {}
        out.append({
            "ref_id": ref.get("ref_id"),
            "title": ref.get("title") or source.get("title"),
            "author": ref.get("author") or source.get("author"),
            "source_label": source.get("source_label"),
            "url": ref.get("url") or source.get("canonical_url"),
            "quote": ref.get("quote"),
            "revision": source.get("source_revision"),
        })
    return out


def coverage_notes(pack: dict) -> list[str]:
    """来源覆盖说明：如实交代每篇读到什么程度。"""
    notes: list[str] = []
    for source in pack.get("sources") or []:
        coverage = source.get("coverage") or "unknown"
        title = source.get("title") or source.get("source_key")
        if coverage == "full_text":
            notes.append(f"《{title}》已读取全部已取得文字（{len(source.get('segments') or [])} 段）")
        else:
            notes.append(f"《{title}》覆盖为 {coverage}，未取得部分不在本作品中补全")
    return notes


# ---- 状态文案（docs/20 §3.5）----

STATUS_TEXT = {
    "queued": "等待处理",
    "preparing": "正在读取所选材料",
    "clarifying": "正在理解你的需求",
    "waiting_user": "有几个问题想和你确认",
    "awaiting_confirmation": "请确认这次的制作方向",
    "synthesizing": "正在整合内容",
    "generating": "正在制作页面",
    "packaging": "正在检查页面",
    "awaiting_runner": "正在检查页面",
    "checking": "正在检查页面",
    "repairing": "正在调整页面",
    "waiting_resources": "等待空闲资源",
    "waiting_key": "请检查模型配置",
    "unknown_outcome": "本次生成结果未确认，可重新尝试",
    "failed": "未能完成，已保留输入和已有版本",
    "cancelled": "已停止",
    "succeeded": "可以预览",
    "running": "正在处理",
    "ready": "可以预览",
}
# 阶段优先于状态：running 时用户看到的是它正在做哪一步
RUNNING_STAGES = ("preparing", "clarifying", "synthesizing", "generating", "packaging",
                  "awaiting_runner", "repairing")
REASON_TEXT = {
    "material_unreadable": "有材料还没有可读正文，请补充材料或取消选择后重试",
    "item_unavailable": "选中的材料已不可用",
    "source_changed": "选中的材料在这之后被重新提取过，请按当前材料重新创建作品",
    "source_too_large": "所选材料太长，请缩小材料范围后重试",
    "too_many_items": "一次选择的材料数量超过上限",
    "key_rejected": "模型凭据被拒绝或不可用，更新后可以从这里重试",
    "no_profile": "还没有可用的模型配置",
    "model_output_invalid": "模型输出没有通过检查，可重试或调整要求",
    "page_source_invalid": "生成的页面没有通过检查，可重试",
    "check_failed": "页面检查没有通过，已保留之前的可用版本",
    "runner_timeout": "页面构建与检查超时没有返回结果",
    "outcome_unknown": "上次请求结果未确认，可从这里重试",
}


def status_text(state: str, stage: str = "") -> str:
    """只显示当前阶段，不编造进度百分比。"""
    if state in ("running",) and stage in RUNNING_STAGES:
        return STATUS_TEXT.get(stage, STATUS_TEXT["running"])
    if state == "waiting_key":
        return STATUS_TEXT["waiting_key"]
    return STATUS_TEXT.get(state, STATUS_TEXT.get(stage, "处理中"))


def reason_text(reason_code: str) -> str | None:
    return REASON_TEXT.get(reason_code)
