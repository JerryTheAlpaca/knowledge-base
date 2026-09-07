"""AI 输出 Schema 校验与 Markdown 预览渲染（docs/02 §11.2、§11.3）。

- 校验只认代码规则：数组长度、文本长度、evidence_ids 必须存在于来源片段。
- insights 必须是 ai_suggestion；workflow 的 accepted/rejected 必须有证据。
- 校验失败不覆盖成品；错误结果仅作诊断留存。
"""
from __future__ import annotations

MAX_TEXT = 2000
LIMITS = {
    "key_points": 12,
    "methods": 10,
    "insights": 5,
    "topics": 8,
    "limitations": 10,
}


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


def validate_analysis(doc: dict, *, source_revision: int, segment_ids: set[str]) -> list[str]:
    """返回错误列表；空列表表示通过。"""
    errors: list[str] = []

    if doc.get("schema_version") != "1.0":
        errors.append("schema_version 必须是 1.0")
    if doc.get("source_revision") != source_revision:
        errors.append(f"source_revision 必须是 {source_revision}")

    errors += _text_errors(doc.get("summary"), field="summary", max_len=500)

    key_points = doc.get("key_points")
    if not isinstance(key_points, list):
        errors.append("key_points 必须是数组")
    else:
        if len(key_points) > LIMITS["key_points"]:
            errors.append(f"key_points 超过 {LIMITS['key_points']} 条")
        for i, kp in enumerate(key_points):
            errors += _text_errors(kp.get("text") if isinstance(kp, dict) else None, field=f"key_points[{i}].text", max_len=600)
            if isinstance(kp, dict):
                errors += _evidence_check(kp, field=f"key_points[{i}]", segment_ids=segment_ids, required=True)

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
            errors += _evidence_check(m, field=f"methods[{i}]", segment_ids=segment_ids, required=True)

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
            errors += _evidence_check(ins, field=f"insights[{i}]", segment_ids=segment_ids, required=False)

    topics = doc.get("topics")
    if not isinstance(topics, list) or len(topics) > LIMITS["topics"] or not all(
        isinstance(t, str) and 0 < len(t) <= 100 for t in topics
    ):
        errors.append(f"topics 必须是字符串数组且不超过 {LIMITS['topics']} 个")

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
                    errors += _evidence_check(d, field=f"workflow.decisions[{i}]", segment_ids=segment_ids, required=True)

    return errors


def _evidence_links(ids: list, base: str) -> str:
    """渲染为指向 normalized.md 块的链接列表。"""
    parts = []
    for sid in ids:
        parts.append(f"[[{base}#{sid}|{sid}]]")
    return "；".join(parts)


def render_preview_md(doc: dict, *, user_note: str | None, normalized_path: str = "normalized") -> str:
    """渲染生成区 Markdown（不含 kb:generated 标记本身，由调用方包裹）。"""
    lines: list[str] = []
    lines.append("## 一句话摘要")
    lines.append("")
    lines.append(doc.get("summary") or "")
    lines.append("")

    lines.append("## 核心观点")
    lines.append("")
    kps = doc.get("key_points") or []
    if kps:
        for kp in kps:
            ev = _evidence_links(kp.get("evidence_ids") or [], normalized_path)
            lines.append(f"- {kp.get('text', '')}" + (f"（{ev}）" if ev else ""))
    else:
        lines.append("材料不足以提取核心观点。")
    lines.append("")

    lines.append("## 方法与适用条件")
    lines.append("")
    methods = doc.get("methods") or []
    if methods:
        for m in methods:
            ev = _evidence_links(m.get("evidence_ids") or [], normalized_path)
            lines.append(f"- {m.get('text', '')}" + (f"（{ev}）" if ev else ""))
            for step in m.get("steps") or []:
                lines.append(f"  - {step}")
            if m.get("conditions"):
                lines.append(f"  - 适用条件：{m['conditions']}")
    else:
        lines.append("原文未提供。")
    lines.append("")

    lines.append("## 候选启发（AI 生成）")
    lines.append("")
    insights = doc.get("insights") or []
    if insights:
        for ins in insights:
            lines.append(f"- {ins.get('text', '')}")
    else:
        lines.append("暂无。")
    lines.append("")

    lines.append("## 完整性与限制")
    lines.append("")
    limitations = doc.get("limitations") or []
    if limitations:
        for lim in limitations:
            lines.append(f"- {lim}")
    else:
        lines.append("未记录明显限制。")
    lines.append("")

    if user_note:
        lines.append("## 用户备注")
        lines.append("")
        lines.append(f"> {user_note}")
        lines.append("")

    return "\n".join(lines)
