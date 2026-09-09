"""AI 输出 Schema 校验与 Markdown 渲染（docs/02 §11.2、§11.3；docs/08 §3.2、§9）。

- 校验只认代码规则：数组长度、文本长度、evidence_ids 必须存在于来源片段、
  claim_id 格式与唯一性、摘录必须是原文片段。
- 新版 Schema 2.0 是单篇提炼契约；1.0 旧产物仍可读取（旧版读取兼容）。
- insights 必须是 ai_suggestion；workflow 的 accepted/rejected 必须有证据。
- 云端产物只保存结构化证据 ID，不生成 Obsidian 双链（docs/08 §3.2）。
- 校验失败不覆盖成品；错误结果仅作诊断留存。
"""
from __future__ import annotations

import re

MAX_TEXT = 2000
LIMITS = {
    "key_points": 12,
    "excerpts": 12,
    "methods": 10,
    "insights": 5,
    "limitations": 10,
}

CLAIM_ID_RE = re.compile(r"^c\d{4}$")

SCHEMA_VERSION = "2.0"
SUPPORTED_SCHEMAS = {"1.0", "2.0"}


def _text_errors(value, *, field: str, max_len: int = MAX_TEXT) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        return [f"{field} 必须是非空字符串"]
    if len(value) > max_len:
        return [f"{field} 超过 {max_len} 字符"]
    return []


def _evidence_check(item: dict, *, field: str, segment_ids: set[str], required: bool) -> list[str]:
    ids = item.get("evidence_ids") or item.get("basis_ids") or []
    errors: list[str] = []
    if required and not ids:
        errors.append(f"{field} 缺少 evidence_ids")
    for sid in ids:
        if sid not in segment_ids:
            errors.append(f"{field} 引用了不存在的片段 {sid}")
    if len(ids) > 20:
        errors.append(f"{field} evidence_ids 数量超过 20")
    return errors


def _normalize_for_quote(text: str) -> str:
    """摘录一致性比较：忽略空白差异，不忽略文字本身（docs/08 §6.1）。"""
    return re.sub(r"\s+", "", text)


def _excerpt_check(item: dict, *, field: str, segment_texts: dict[str, str]) -> list[str]:
    """摘录必须能在被引用的原文片段中逐字找到（docs/08 §3.2、§6.1）。"""
    errors: list[str] = []
    text = item.get("text")
    if not isinstance(text, str) or not text.strip():
        return [f"{field}.text 必须是非空字符串"]
    ids = item.get("evidence_ids") or []
    if not ids:
        return [f"{field} 缺少 evidence_ids"]
    target = _normalize_for_quote(text)
    for sid in ids:
        source = segment_texts.get(sid)
        if source is None:
            continue  # 片段不存在由 _evidence_check 报告
        if target and target in _normalize_for_quote(source):
            return []
    errors.append(f"{field} 的摘录未在被引用的原文片段中逐字出现")
    return errors


def _claim_id_errors(item: dict, *, field: str, seen: set[str]) -> list[str]:
    """claim_id 是本地整理与 Knowledge 证据链的稳定锚点（docs/08 §6.1）。"""
    cid = item.get("claim_id")
    if not isinstance(cid, str) or not CLAIM_ID_RE.match(cid):
        return [f"{field}.claim_id 必须是 c + 4 位数字（如 c0001）"]
    if cid in seen:
        return [f"{field}.claim_id 重复：{cid}"]
    seen.add(cid)
    return []


def validate_analysis(
    doc: dict,
    *,
    source_revision: int,
    segment_ids: set[str],
    segment_texts: dict[str, str] | None = None,
) -> list[str]:
    """返回错误列表；空列表表示通过。

    segment_texts 为可选片段正文映射：提供时额外校验 excerpts 逐字一致。
    """
    errors: list[str] = []
    schema = doc.get("schema_version")
    if schema not in SUPPORTED_SCHEMAS:
        errors.append("schema_version 必须是 1.0 或 2.0")
        return errors
    is_v2 = schema == "2.0"

    if doc.get("source_revision") != source_revision:
        errors.append(f"source_revision 必须是 {source_revision}")

    errors += _text_errors(doc.get("summary"), field="summary", max_len=500)

    texts = segment_texts or {}
    seen_claim_ids: set[str] = set()

    key_points = doc.get("key_points")
    if not isinstance(key_points, list):
        errors.append("key_points 必须是数组")
    else:
        if len(key_points) > LIMITS["key_points"]:
            errors.append(f"key_points 超过 {LIMITS['key_points']} 条")
        for i, kp in enumerate(key_points):
            errors += _text_errors(kp.get("text") if isinstance(kp, dict) else None,
                                   field=f"key_points[{i}].text", max_len=600)
            if isinstance(kp, dict):
                errors += _evidence_check(kp, field=f"key_points[{i}]",
                                          segment_ids=segment_ids, required=True)
                if is_v2:
                    errors += _claim_id_errors(kp, field=f"key_points[{i}]", seen=seen_claim_ids)
                    cond = kp.get("conditions")
                    if cond is not None and not isinstance(cond, str):
                        errors.append(f"key_points[{i}].conditions 必须是字符串或 null")

    if is_v2:
        excerpts = doc.get("excerpts")
        if excerpts is None:
            excerpts = []
        if not isinstance(excerpts, list):
            errors.append("excerpts 必须是数组")
        else:
            if len(excerpts) > LIMITS["excerpts"]:
                errors.append(f"excerpts 超过 {LIMITS['excerpts']} 条")
            for i, ex in enumerate(excerpts):
                if not isinstance(ex, dict):
                    errors.append(f"excerpts[{i}] 必须是对象")
                    continue
                errors += _evidence_check(ex, field=f"excerpts[{i}]",
                                          segment_ids=segment_ids, required=True)
                errors += _claim_id_errors(ex, field=f"excerpts[{i}]", seen=seen_claim_ids)
                if texts:
                    errors += _excerpt_check(ex, field=f"excerpts[{i}]", segment_texts=texts)

    methods = doc.get("methods")
    if methods is None:
        errors.append("methods 必须是数组（可以为空）")
    elif not isinstance(methods, list) or len(methods) > LIMITS["methods"]:
        errors.append(f"methods 必须是数组且不超过 {LIMITS['methods']} 条")
    else:
        for i, m in enumerate(methods):
            if not isinstance(m, dict):
                errors.append(f"methods[{i}] 必须是对象")
                continue
            errors += _text_errors(m.get("text"), field=f"methods[{i}].text")
            errors += _evidence_check(m, field=f"methods[{i}]",
                                      segment_ids=segment_ids, required=True)

    insights = doc.get("insights")
    if not isinstance(insights, list):
        errors.append("insights 必须是数组")
    else:
        if len(insights) > LIMITS["insights"]:
            errors.append(f"insights 超过 {LIMITS['insights']} 条")
        for i, ins in enumerate(insights):
            if not isinstance(ins, dict):
                errors.append(f"insights[{i}] 必须是对象")
                continue
            errors += _text_errors(ins.get("text"), field=f"insights[{i}].text")
            if ins.get("kind") != "ai_suggestion":
                errors.append(f"insights[{i}].kind 必须是 ai_suggestion")
            errors += _evidence_check(ins, field=f"insights[{i}]",
                                      segment_ids=segment_ids, required=False)

    if not is_v2:
        # 旧版才有 topics；新版云端不输出主题（docs/08 §3.2）
        topics = doc.get("topics")
        if not isinstance(topics, list) or len(topics) > 8 or not all(
            isinstance(t, str) and 0 < len(t) <= 100 for t in topics
        ):
            errors.append("topics 必须是字符串数组且不超过 8 个")

    limitations = doc.get("limitations")
    if not isinstance(limitations, list) or len(limitations) > LIMITS["limitations"] or not all(
        isinstance(t, str) and len(t) <= 1000 for t in limitations
    ):
        errors.append(f"limitations 必须是字符串数组且不超过 {LIMITS['limitations']} 条")

    workflow = doc.get("workflow")
    if workflow is not None:
        if not isinstance(workflow, dict):
            errors.append("workflow 必须是对象或 null")
        else:
            for i, d in enumerate(workflow.get("decisions") or []):
                if not isinstance(d, dict):
                    errors.append(f"workflow.decisions[{i}] 必须是对象")
                    continue
                if d.get("status") not in {"proposed", "accepted", "rejected", "unknown"}:
                    errors.append(f"workflow.decisions[{i}].status 非法")
                if d.get("status") in {"accepted", "rejected"}:
                    errors += _evidence_check(d, field=f"workflow.decisions[{i}]",
                                              segment_ids=segment_ids, required=True)

    return errors


def evidence_map(doc: dict) -> dict[str, dict]:
    """结构化证据映射：claim_id -> {text, conditions, evidence_ids}（docs/08 §6.1）。

    云端只保存 ID 映射，供本地插件与网页定位；不渲染 Obsidian 双链。
    """
    out: dict[str, dict] = {}
    for kp in doc.get("key_points") or []:
        if not isinstance(kp, dict):
            continue
        cid = kp.get("claim_id")
        if not isinstance(cid, str) or not cid:
            continue
        out[cid] = {
            "kind": "key_point",
            "text": kp.get("text"),
            "conditions": kp.get("conditions"),
            "evidence_ids": list(kp.get("evidence_ids") or []),
        }
    for ex in doc.get("excerpts") or []:
        if not isinstance(ex, dict):
            continue
        cid = ex.get("claim_id")
        if not isinstance(cid, str) or not cid:
            continue
        entry = out.setdefault(cid, {"kind": "excerpt", "text": None,
                                     "conditions": None, "evidence_ids": []})
        entry["excerpt"] = ex.get("text")
        entry["excerpt_evidence_ids"] = list(ex.get("evidence_ids") or [])
    return out


def _evidence_ref(ids: list, base: str) -> str:
    """渲染为指向原文块的链接文本；base 为空时不生成链接（网页纯文本展示用）。"""
    parts = []
    for sid in ids:
        parts.append(f"[[{base}#^{sid}|{sid}]]" if base else str(sid))
    return "；".join(parts)


def render_preview_md(doc: dict, *, user_note: str | None, normalized_path: str = "") -> str:
    """渲染单篇提炼 Markdown（不含分区标记本身，由调用方包裹）。

    对应 Digest 的 `kb:cloud-digest` 区（docs/08 §3.2）：
    一句话总结 / 核心观点与证据 / 值得保留的原文摘录 / 方法、适用条件与局限 /
    AI 候选启发。不输出知识关联、晋升、主题链接和标签。
    """
    lines: list[str] = []
    lines.append("## 一句话总结")
    lines.append("")
    lines.append(doc.get("summary") or "")
    lines.append("")

    lines.append("## 核心观点与证据")
    lines.append("")
    kps = doc.get("key_points") or []
    if kps:
        for kp in kps:
            cid = kp.get("claim_id")
            prefix = f"[{cid}] " if cid else ""
            ev = _evidence_ref(kp.get("evidence_ids") or [], normalized_path)
            cond = kp.get("conditions")
            suffix = f"（适用条件：{cond}）" if cond else ""
            lines.append(f"- {prefix}{kp.get('text', '')}{suffix}" + (f"（{ev}）" if ev else ""))
    else:
        lines.append("材料不足以提取核心观点。")
    lines.append("")

    lines.append("## 值得保留的原文摘录")
    lines.append("")
    excerpts = doc.get("excerpts") or []
    if excerpts:
        for ex in excerpts:
            cid = ex.get("claim_id")
            prefix = f"[{cid}] " if cid else ""
            ev = _evidence_ref(ex.get("evidence_ids") or [], normalized_path)
            lines.append(f"> {prefix}{ex.get('text', '')}" + (f"（{ev}）" if ev else ""))
    else:
        lines.append("没有合适的逐字摘录。")
    lines.append("")

    lines.append("## 方法、适用条件与局限")
    lines.append("")
    methods = doc.get("methods") or []
    if methods:
        for m in methods:
            ev = _evidence_ref(m.get("evidence_ids") or [], normalized_path)
            lines.append(f"- {m.get('text', '')}" + (f"（{ev}）" if ev else ""))
            for step in m.get("steps") or []:
                lines.append(f"  - {step}")
            if m.get("conditions"):
                lines.append(f"  - 适用条件：{m['conditions']}")
    else:
        lines.append("原文未提供。")
    limitations = doc.get("limitations") or []
    if limitations:
        lines.append("")
        for lim in limitations:
            lines.append(f"- 局限：{lim}")
    lines.append("")

    lines.append("## AI 候选启发")
    lines.append("")
    insights = doc.get("insights") or []
    if insights:
        for ins in insights:
            lines.append(f"- {ins.get('text', '')}（AI 推测，未经原文证明）")
    else:
        lines.append("暂无。")
    lines.append("")

    if user_note:
        lines.append("> 采集备注：")
        lines.append(f"> {user_note}")
        lines.append("")

    return "\n".join(lines)
