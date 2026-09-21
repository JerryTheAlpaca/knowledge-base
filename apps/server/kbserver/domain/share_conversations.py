"""需求摘要、问答轮次、不可变历史与稳定前缀（docs/20 §6.1.1、§6.5.2、§6.5.6）。

缓存的前提是前缀逐字节稳定：system 与材料包在一个 context_epoch 内不变，
轮次号、当前时间、最新摘要这些业务状态只作为尾部新消息，不插到开头。
本模块只做纯计算与规则；写库与幂等提交在 repositories/shares.py。
"""
from __future__ import annotations

import hashlib
import json
from typing import Iterable

from ..providers.llm import ConversationMessage
from .sharing import BRIEF_FIELDS, normalize_brief


def canonical_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def prefix_hash(messages: Iterable[ConversationMessage]) -> str:
    """稳定前缀哈希：只用于发现前缀漂移，不是供应商缓存的替代品，也不当身份用。"""
    digest = hashlib.sha256()
    for message in messages:
        digest.update(canonical_bytes([message.role, message.content]))
        digest.update(b"\x00")
    return digest.hexdigest()


def stable_prefix(*, system_prompt: str, pack_text: str, initial_request: str) -> list[ConversationMessage]:
    """一个 context_epoch 内固定的前三条消息（规则 → 材料包 → 用户初始要求）。"""
    return [
        ConversationMessage(role="system", content=system_prompt),
        ConversationMessage(role="user", content=pack_text),
        ConversationMessage(role="user", content=initial_request),
    ]


def request_messages(*, prefix: Iterable[ConversationMessage], history: Iterable[ConversationMessage],
                     tail: Iterable[ConversationMessage] = ()) -> list[ConversationMessage]:
    """装配本轮请求：固定前缀 + 原顺序历史 + 只在尾部追加的新内容。"""
    return [*prefix, *history, *tail]


def normalize_answer_text(round_doc: dict, answers: list[dict] | None, message: str | None) -> str:
    """把结构化回答与自由补充规范化成一条稳定 user 消息。

    同一个请求回放时必须得到完全相同的文字，否则前缀就漂了（§6.5.2 第 4 条）。
    """
    by_id = {q.get("id"): q for q in (round_doc.get("questions") or []) if isinstance(q, dict)}
    lines: list[str] = []
    for answer in answers or []:
        if not isinstance(answer, dict):
            continue
        question = by_id.get(answer.get("question_id"))
        if question is None:
            continue
        text = str(question.get("text") or "").strip()
        labels: list[str] = []
        options = {o.get("id"): o for o in (question.get("options") or []) if isinstance(o, dict)}
        for oid in answer.get("option_ids") or []:
            opt = options.get(oid)
            if opt:
                labels.append(str(opt.get("label") or oid))
        free = str(answer.get("text") or "").strip()
        parts = labels + ([free] if free else [])
        lines.append(f"关于「{text}」：{'；'.join(parts) if parts else '（未回答）'}")
    extra = str(message or "").strip()
    if extra:
        lines.append(f"补充：{extra}")
    return "\n".join(lines)


def unanswered_questions(round_doc: dict, answers: list[dict] | None) -> list[str]:
    """本轮里没答的问题保持原状态，不自动采用默认项。"""
    answered = {a.get("question_id") for a in (answers or []) if isinstance(a, dict)
                and (a.get("option_ids") or str(a.get("text") or "").strip())}
    return [q.get("id") for q in (round_doc.get("questions") or [])
            if isinstance(q, dict) and q.get("id") not in answered]


def blocking_questions(round_doc: dict) -> list[dict]:
    """实际阻断项：材料不可读、要求互相矛盾等，不能由「按你的建议生成」授权。"""
    return [q for q in (round_doc.get("questions") or [])
            if isinstance(q, dict) and q.get("required_for_generation") is True]


def merge_brief(*, previous_brief: dict | None, previous_provenance: dict | None,
                model_brief: dict | None, source_message_id: str | None = None) -> tuple[dict, dict]:
    """合并需求摘要：用户已定的项不被模型改写，本轮由用户回答推动的项记为用户。

    返回 (brief, provenance)。provenance[field] = {"by": "user"|"ai", "message_id": ...}
    """
    brief = normalize_brief(previous_brief)
    prov: dict = dict(previous_provenance or {})
    incoming = normalize_brief(model_brief)
    for field in BRIEF_FIELDS:
        value = incoming.get(field)
        empty = value is None or value == [] or value == ""
        if empty:
            continue
        if prov.get(field, {}).get("by") == "user" and brief.get(field) != value:
            # 用户说过的话优先；模型想改动时保留原值，冲突由界面如实显示
            continue
        brief[field] = value
        prov[field] = ({"by": "user", "message_id": source_message_id} if source_message_id
                       else {"by": "ai", "message_id": None})
    return brief, prov


def delegate_assumptions(brief: dict, provenance: dict) -> tuple[dict, dict]:
    """用户明确「按你的建议生成」：未定的普通偏好固化为 AI 假设。"""
    out = dict(brief)
    prov = dict(provenance)
    for field in BRIEF_FIELDS:
        value = out.get(field)
        if value is None or value == []:
            if field in ("goal", "audience"):
                out[field] = "由 AI 按材料决定"
            else:
                out[field] = ["由 AI 按材料决定"]
            prov[field] = {"by": "ai", "message_id": None, "delegated": True}
    return out, prov


def brief_is_empty(brief: dict) -> bool:
    return all(brief.get(f) in (None, [], "") for f in BRIEF_FIELDS if f != "assumptions")


def render_brief_lines(brief: dict, provenance: dict) -> list[dict]:
    """界面「目前的需求」：普通语言 + 标明哪些是 AI 暂定。"""
    labels = {
        "goal": "作品目的", "audience": "给谁看", "content_priorities": "内容重点",
        "presentation_preferences": "呈现偏好", "must_keep": "必须保留",
        "must_avoid": "不希望展开", "assumptions": "AI 暂定",
    }
    lines: list[dict] = []
    for field in BRIEF_FIELDS:
        value = brief.get(field)
        if value is None or value == []:
            continue
        lines.append({
            "field": field,
            "label": labels[field],
            "value": value if isinstance(value, list) else [value],
            "by": provenance.get(field, {}).get("by", "ai"),
        })
    return lines


def estimate_messages_tokens(messages: Iterable[ConversationMessage]) -> int:
    """粗估输入规模（中文按字符数近似）：只用于压缩判断，不作为精确 Token 统计展示。"""
    from .templates import estimate_tokens

    return sum(estimate_tokens(m.content) + 4 for m in messages)


def should_compact(*, estimated_input_tokens: int, context_tokens: int, reserve_output_tokens: int,
                   ratio: float) -> bool:
    """接近可用输入窗口才压缩：先给输出留位置，再按阈值判断（§14.1）。"""
    if context_tokens <= 0:
        return False
    available = max(1, context_tokens - reserve_output_tokens)
    return estimated_input_tokens >= int(available * ratio)


def compact_summary_messages(*, confirmed_constraints: list[str], user_quotes: list[str],
                             evidence_index: list[str], recent: Iterable[ConversationMessage]) -> list[ConversationMessage]:
    """开启新 context_epoch 时的起点：保留已确认约束、关键原话、证据索引与最近消息。

    这是一次有代价的重启（可能多一次摘要调用并冷启动），必须记录原因，
    不能每轮自动摘要来「省上下文」却不断破坏缓存。
    """
    body = ["【上下文续接】以下是本会话已确认的需求与依据，后续按它继续："]
    for line in confirmed_constraints:
        body.append(f"- 已确认：{line}")
    for quote in user_quotes:
        body.append(f"- 用户原话：{quote}")
    for index in evidence_index:
        body.append(f"- 证据索引：{index}")
    return [ConversationMessage(role="user", content="\n".join(body)), *recent]
