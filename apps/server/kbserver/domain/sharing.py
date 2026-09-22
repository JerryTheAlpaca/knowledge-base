"""分享作品的固定快照、公开引用与页面预检（docs/24 §9、docs/23 §7.3）。

原则：
- 快照固定当次输入：创建任务时把每条材料的 item_id／source_revision／正文片段
  钉死，生成过程中不再读「最新版本」。
- 内容阶段直接用 ContentDocument v3：模型只写内容主体，引用只填程序给出的 `R` 编号。
  `sec1`/`k1` 这类模型自编的编号、`s1:segment:seg0001` 这类第二套 citation 语法都
  退出协议；页面锚点（`ref_id`）在组装之后由程序按文档引用表顺序分配。
- 模型只拿到按 `R` 打包的阅读单元与来源标题，拿不到 UUID、storage_key 与真实路径。
- 公开页面只出现来源标题、允许公开的 URL 与已确认引用。私有来源没有公开链接时
  如实说明，不生成指向私有 API 的链接。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import content_v3

SCHEMA_VERSION = "1.0"  # 分享对象结构（澄清稿、页面源文件、runner 信封），不是内容协议版本
SHARE_RECIPE_VERSION = "share-content-v3"
PROMPT_VERSION = "share-prompt-v3"
CONTENT_TASK = "share_synthesis"

# 对象引用角色（docs/20 §11.4）：默认 private，只有白名单产物可被公开路由读取
ARTIFACT_ROLES = frozenset({
    "input_snapshot", "conversation_message", "brief", "clarification_round",
    "synthesis", "page_source", "html", "public_references", "asset",
    "check_report", "screenshot",
})
PUBLIC_ARTIFACT_ROLES = frozenset({"html", "public_references"})

NEXT_ACTIONS = ("ask_user", "confirm_brief")
CONFIRMATION_KINDS = ("confirm", "delegate_preferences", "explicit_modify")
BRIEF_FIELDS = (
    "goal", "audience", "content_priorities", "presentation_preferences",
    "must_keep", "must_avoid", "assumptions",
)

OPTION_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
PUBLIC_URL_PREFIXES = ("http://", "https://")


@dataclass
class SourceSpec:
    """一条材料在快照中的固定内容（由 workers/share.py 从库里读出）。"""

    item_id: str
    source_revision: int
    title: str
    coverage: str
    segments: list[dict] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)
    assets: list[dict] = field(default_factory=list)
    bundle_revision: int | None = None
    author: str | None = None
    canonical_url: str | None = None
    source_label: str | None = None


def build_source_pack(specs: list[SourceSpec], *, include_storage: bool = False) -> dict:
    """私有 source_pack.json（docs/20 §5.1）：本轮固定输入，不直接交给模型。

    片段沿用来源里的真实 `segment_id`（docs/24 §2 的引用范围就是这些片段）；
    include_storage 只用于编排器自己的 input_manifest 侧车，交给 runner 的素材
    目录不带 storage_key。
    """
    sources = []
    for spec in specs:
        entry: dict = {
            "item_id": spec.item_id,
            "source_revision": spec.source_revision,
            "bundle_revision": spec.bundle_revision,
            "title": spec.title,
            "author": spec.author,
            "canonical_url": spec.canonical_url,
            "source_label": spec.source_label,
            "coverage": spec.coverage,
            "segments": [dict(s) for s in spec.segments],
            "hints": list(spec.hints),
            "assets": [
                {k: v for k, v in a.items() if include_storage or k != "storage_key"}
                for a in spec.assets
            ],
        }
        sources.append(entry)
    return {"schema_version": SCHEMA_VERSION, "sources": sources}


def materials_of(pack: dict) -> list[content_v3.Material]:
    """固定快照 → `build_ref_table` 的输入单元：一条材料一个来源版本。"""
    return [
        content_v3.Material(
            item_id=source["item_id"],
            source_revision=source["source_revision"],
            segments=list(source.get("segments") or []),
        )
        for source in pack.get("sources") or []
    ]


def material_pack_for_model(pack: dict, ref_table: content_v3.RefTable) -> dict:
    """模型看到的材料清单：`R` 编号 + 阅读单元原文 + 来源标题与覆盖。

    这是内容会话固定前缀的第二条消息，只含用户自己选中的材料与来源标题；
    UUID、来源版本、存储键与哈希都留在私有快照里（docs/23 §4.1）。
    """
    by_revision = {
        (s["item_id"], s["source_revision"]): s for s in pack.get("sources") or []
    }
    sources: list[dict] = []
    view_of: dict[tuple, dict] = {}
    material = []
    for key, entry in ref_table.entries():
        material.append({"ref": key, "text": entry.text})
        ident = (entry.item_id, entry.source_revision)
        view = view_of.get(ident)
        if view is None:
            source = by_revision.get(ident) or {}
            view = {"title": source.get("title") or "未命名材料",
                    "coverage": source.get("coverage") or "unknown",
                    "hints": list(source.get("hints") or []), "refs": []}
            view_of[ident] = view
            sources.append(view)
        view["refs"].append(key)
    return {"format_version": content_v3.CONTENT_FORMAT_VERSION, "sources": sources,
            "material": material}


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


# ---- 整合稿：ContentDocument v3（docs/24 §1–§4、§9）----


def assemble_synthesis(model_output: dict | str, *, pack: dict,
                       ref_table: content_v3.RefTable, run_id: str,
                       repair_calls: int = 0) -> tuple[dict | None, content_v3.AssemblyReport]:
    """把模型输出组装成 `kind="synthesis"` 的 v3 文档。

    引用是否存在、摘录是否逐字、块级失败全部由组装器判定：分享侧不再维护第二套
    编号与引用校验。整合稿不绑定单一条目，来源版本由引用表写进 provenance。
    """
    sources = pack.get("sources") or []
    return content_v3.assemble_content_document(
        model_output,
        ref_table=ref_table,
        document_id=f"syn-{run_id}",
        kind="synthesis",
        revision=1,
        item_id="",
        source_revision=0,
        task=CONTENT_TASK,
        recipe_version=content_v3.CONTENT_RECIPE_VERSION,
        repair_calls=repair_calls,
        source_title=sources[0].get("title") if len(sources) == 1 else None,
    )


def completeness_state(report: content_v3.AssemblyReport) -> str:
    return str((report.completeness or {}).get("state") or "failed")


def assembly_problems(report: content_v3.AssemblyReport) -> list[str]:
    """缺口说明：直接用组装器写好的中文文案，这里不再判一遍。"""
    return [str(gap.get("message") or gap.get("code"))
            for gap in (report.completeness or {}).get("gaps") or []]


def confirmed_quotes(document: dict) -> dict[str, str]:
    """文档里已过逐字核对的摘录：`e` 键 → 原文引用（页面只公开这些）。"""
    quotes: dict[str, str] = {}
    for section in document.get("sections") or []:
        for block in section.get("blocks") or []:
            if not isinstance(block, dict) or block.get("kind") != "quote":
                continue
            for key in block.get("refs") or []:
                if isinstance(key, str) and block.get("text"):
                    quotes.setdefault(key, block["text"])
    return quotes


def public_references(document: dict, pack: dict) -> tuple[list[dict], list[str]]:
    """程序分配的公开引用清单（docs/24 §9）：锚点按文档引用表出现顺序编号。

    只暴露来源标题、允许公开的 URL 与已确认摘录；对不上本轮固定快照的引用会报
    出来并阻断发布，不会凭空生成条目或链接。
    """
    sources = {(s["item_id"], s["source_revision"]): s for s in pack.get("sources") or []}
    quotes = confirmed_quotes(document)
    refs: list[dict] = []
    problems: list[str] = []
    for index, (key, ref) in enumerate((document.get("references") or {}).items(), start=1):
        source = sources.get((ref.get("item_id"), ref.get("source_revision")))
        if source is None:
            problems.append(f"引用 {key} 的来源不在本轮固定快照里，没有生成公开条目")
            continue
        url = source.get("canonical_url")
        refs.append({
            "ref_id": f"ref{index}",
            "title": source.get("title") or "未命名来源",
            "author": source.get("author"),
            "source_label": source.get("source_label"),
            "url": url if isinstance(url, str) and url.startswith(PUBLIC_URL_PREFIXES) else None,
            "quote": quotes.get(key),
            "revision": ref.get("source_revision"),
        })
    return refs, problems


def page_content_view(document: dict) -> dict:
    """交给页面步骤的内容：v3 主体 + 程序分配的锚点，不含内部引用表。

    `e` 键与 `public_references` 的 `ref_id` 同源同序，模型因此只看得到 `ref1`
    这种最终锚点，看不到来源 UUID、片段范围与哈希。
    """
    anchors = {key: f"ref{i}"
               for i, key in enumerate((document.get("references") or {}), start=1)}
    sections = []
    for section in document.get("sections") or []:
        blocks = [{
            "kind": block.get("kind"),
            "text": block.get("text"),
            "refs": [anchors.get(key, "") for key in block.get("refs") or []],
        } for block in section.get("blocks") or [] if isinstance(block, dict)]
        sections.append({"heading": section.get("heading"), "blocks": blocks})
    return {"format_version": document.get("format_version"), "title": document.get("title"),
            "summary": document.get("summary"), "sections": sections,
            "limitations": list(document.get("limitations") or [])}


def usage_notes(*, task_extras: dict, ref_table: content_v3.RefTable,
                document: dict, pack: dict) -> list[dict]:
    """模型按 `R` 交代的素材用途：换成公开锚点与来源标题后再交给页面步骤。"""
    titles = {(s["item_id"], s["source_revision"]): s.get("title")
              for s in pack.get("sources") or []}
    anchor_of: dict[tuple, str] = {
        (ref.get("item_id"), ref.get("source_revision"), tuple(ref.get("segment_ids") or [])):
            f"ref{index}"
        for index, ref in enumerate((document.get("references") or {}).values(), start=1)
    }
    out: list[dict] = []
    for item in (task_extras or {}).get("material_usage") or []:
        entry = ref_table.get(item.get("ref")) if isinstance(item, dict) else None
        if entry is None:
            continue
        anchor = anchor_of.get(entry.key())
        if anchor is None:
            continue  # 该阅读单元最终没有进入文档引用表，不凭空造一条用途
        out.append({"ref_id": anchor,
                    "title": titles.get((entry.item_id, entry.source_revision)),
                    "note": item.get("note")})
    return out


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


def public_link_notes(references: list[dict]) -> list[str]:
    """没有公开链接的来源：如实说明页面只列名称与已确认引用（docs/23 §7.3）。

    这里只写允许公开的措辞，不生成任何指向私有 API 的路径或访问方式。
    """
    return [
        f"《{ref.get('title')}》没有可公开的原文链接，页面只列出来源名称与已确认引用。"
        for ref in references if not ref.get("url")
    ]


def coverage_notes(pack: dict) -> list[str]:
    """来源覆盖说明：如实交代每篇读到什么程度。"""
    notes: list[str] = []
    for source in pack.get("sources") or []:
        coverage = source.get("coverage") or "unknown"
        title = source.get("title") or "未命名材料"
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
    "partial_result": "整合内容没有全部通过核实（引用或摘录对不上原文），未生成页面，可调整后重试",
    "source_mismatch": "整合内容与本轮固定快照不一致，未生成页面，请按当前材料重新创建作品",
    "content_invalid": "已保存的整合内容记录不完整，未生成页面，可重新生成",
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
