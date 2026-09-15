"""片段 → 自然段落的阅读层分组（证据粒度与阅读粒度分离）。

背景：extractors 产出的 segments 同时被当作证据锚点（越细越准）和给人读的正文，
直接按段渲染就成了一句一行。这里只在**呈现层**加一层 paragraphs：

- segments 不动，仍是可引用的最小单位，块 ID `^s0001` 保持逐句定位；
- paragraphs 由相邻片段合并而成，块 ID `^p0001`，供 Obsidian 与 Web 阅读；
- 段落只是合并相邻原文，不改写、不重排、不生成新内容。

两类合并规则（按片段是否带时间区分）：

- 时序类（字幕 / ASR）：句末标点 + 说话停顿 + 长度上限；
- 文章类（网页 / 公众号）：优先尊重原文自己的块分段；当检测到排版器
  「一行一块」的软换行时，再按句末标点与长度合并成语义段。
"""
from __future__ import annotations

# 句末标点：中英文句号、问号、感叹、省略、分号，以及常见收尾引号/括号
_SENTENCE_END_CHARS = set("。！？…；!?.")
_SENTENCE_END_TAIL = set("”’」』）)】》\"'")

# 时序类（字幕/ASR）阈值
# 2026-09-15：110 会让"几乎不歇气"的语流每攒够 110 字碰到句号就断
# （实测一条视频 24 个断点 23 个由此触发，说话人段内停顿全部 ≤0.5s），
# 提高到 180：段落按更大的句群收束，stop 停顿优先规则不变。
_TIMED_TARGET_CHARS = 180  # 到句末标点且不少于这个长度就断段
_TIMED_HARD_CHARS = 380  # 没遇到标点也强制断段
_TIMED_MIN_GAP_CHARS = 45  # 靠停顿断段时的最小长度
_TIMED_GAP_MS = 1800  # 说话停顿超过这个间隔视为换段

# 文章类阈值
_ARTICLE_SOFT_WRAP_AVG = 35  # 平均块长低于它且块够多，判定为排版器软换行
_ARTICLE_SOFT_WRAP_MIN_BLOCKS = 12
_ARTICLE_TARGET_CHARS = 120
_ARTICLE_HARD_CHARS = 420

_HEADING_KINDS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def _text(seg: dict) -> str:
    return (seg.get("text") or "").strip()


def _ends_sentence(text: str) -> bool:
    """句末标点结尾（允许后面跟收尾引号/括号）。"""
    t = text.rstrip()
    while t and t[-1] in _SENTENCE_END_TAIL:
        t = t[:-1].rstrip()
    return bool(t) and t[-1] in _SENTENCE_END_CHARS


def _needs_space(left: str, right: str) -> bool:
    """拼接两段文字时是否补空格：只在中英混排的 ASCII 词边界补。"""
    if not left or not right:
        return False
    return left[-1].isascii() and left[-1].isalnum() and right[0].isascii() and right[0].isalnum()


def _join_texts(texts: list[str]) -> str:
    out = ""
    for t in texts:
        t = t.strip()
        if not t:
            continue
        if not out:
            out = t
        elif _needs_space(out, t):
            out = f"{out} {t}"
        else:
            out += t
    return out


def _is_timed(segments: list[dict]) -> bool:
    return any(seg.get("start_ms") is not None for seg in segments)


def _is_heading(seg: dict) -> bool:
    return seg.get("kind") == "heading"


def _close(cur: list[dict], out: list[list[dict]], kind: str = "paragraph") -> None:
    if cur:
        out.append((list(cur), kind))
        cur.clear()


def _group_timed(segments: list[dict]) -> list[tuple[list[dict], str]]:
    groups: list[tuple[list[dict], str]] = []
    cur: list[dict] = []
    chars = 0  # 当前组累计字符数（增量维护，避免每片重算全组，审查 C-11）
    for i, seg in enumerate(segments):
        cur.append(seg)
        chars += len(_text(seg))
        nxt = segments[i + 1] if i + 1 < len(segments) else None
        gap_ms = 0
        if nxt is not None and nxt.get("start_ms") is not None and seg.get("end_ms") is not None:
            gap_ms = int(nxt["start_ms"]) - int(seg["end_ms"])
        text = _text(seg)
        if _ends_sentence(text) and chars >= _TIMED_TARGET_CHARS:
            _close(cur, groups)
            chars = 0
        elif chars >= _TIMED_HARD_CHARS:
            _close(cur, groups)
            chars = 0
        elif gap_ms >= _TIMED_GAP_MS and chars >= _TIMED_MIN_GAP_CHARS:
            _close(cur, groups)
            chars = 0
    _close(cur, groups)
    return groups


def _group_article(segments: list[dict]) -> list[tuple[list[dict], str]]:
    texts = [_text(s) for s in segments]
    avg = (sum(len(t) for t in texts) / len(texts)) if texts else 0
    soft_wrap = len(texts) >= _ARTICLE_SOFT_WRAP_MIN_BLOCKS and avg < _ARTICLE_SOFT_WRAP_AVG

    groups: list[tuple[list[dict], str]] = []
    cur: list[dict] = []
    chars = 0  # 增量维护（审查 C-11）
    for seg, text in zip(segments, texts):
        if _is_heading(seg):
            _close(cur, groups)
            _close([seg], groups, kind="heading")
            chars = 0
            continue
        cur.append(seg)
        chars += len(text)
        if soft_wrap:
            if _ends_sentence(text) and chars >= _ARTICLE_TARGET_CHARS:
                _close(cur, groups)
                chars = 0
            elif chars >= _ARTICLE_HARD_CHARS:
                _close(cur, groups)
                chars = 0
        elif _ends_sentence(text) or chars >= _ARTICLE_HARD_CHARS:
            # 原文自己的分段：块以句末标点结尾就是一段
            _close(cur, groups)
            chars = 0
    _close(cur, groups)
    return groups


def group_paragraphs(segments: list[dict]) -> list[dict]:
    """segments → paragraphs。

    返回按原文顺序排列的段落，每段：
        {"paragraph_id": "p0001", "segment_ids": [...], "kind": "paragraph"|"heading",
         "start_ms": int|None, "end_ms": int|None, "char_count": int, "text": str}

    text 是拼接后的段落正文（渲染用，不改写原文）。段为空时返回空列表。
    """
    usable = [s for s in segments if s.get("segment_id") and _text(s)]
    if not usable:
        return []
    groups = _group_timed(usable) if _is_timed(usable) else _group_article(usable)

    paragraphs: list[dict] = []
    for idx, (group, kind) in enumerate(groups, start=1):
        starts = [s.get("start_ms") for s in group if s.get("start_ms") is not None]
        ends = [s.get("end_ms") for s in group if s.get("end_ms") is not None]
        text = _join_texts([_text(s) for s in group])
        paragraphs.append({
            "paragraph_id": f"p{idx:04d}",
            "segment_ids": [s["segment_id"] for s in group],
            "kind": kind,
            "start_ms": min(starts) if starts else None,
            "end_ms": max(ends) if ends else None,
            "char_count": len(text),
            "text": text,
        })
    return paragraphs


def segment_paragraph_map(paragraphs: list[dict]) -> dict[str, str]:
    """segment_id → paragraph_id，供证据引用定位到所属段落。"""
    return {
        sid: p["paragraph_id"]
        for p in paragraphs
        for sid in p.get("segment_ids", [])
    }


def paragraphs_to_readable_md(paragraphs: list[dict]) -> str:
    """段落 → 可读 Markdown：一个段落一个块，块 ID `^p0001`。

    标题段渲染为 `## 标题`，保留原文章结构；正文段落为普通段落。
    """
    blocks: list[str] = []
    for p in paragraphs:
        text = (p.get("text") or "").strip()
        if not text:
            continue
        prefix = "## " if p.get("kind") == "heading" else ""
        blocks.append(f"{prefix}{text} ^{p['paragraph_id']}\n")
    return "\n".join(blocks)
