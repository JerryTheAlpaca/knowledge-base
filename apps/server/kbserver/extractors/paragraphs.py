"""片段 → 自然段落的阅读层分组（证据粒度与阅读粒度分离）。

背景：extractors 产出的 segments 同时被当作证据锚点（越细越准）和给人读的正文，
直接按段渲染就成了一句一行。这里只在**呈现层**加一层 paragraphs：

- segments 不动，仍是可引用的最小单位，块 ID `^s0001` 保持逐句定位；
- paragraphs 由相邻片段合并而成，块 ID `^p0001`，供 Obsidian 与 Web 阅读；
- 段落只是合并相邻原文，不改写、不重排、不生成新内容。

两类合并规则（按片段是否带时间区分）：

- 时序类（字幕 / ASR）：语义话题边界（词汇重叠谷）+ 说话人强停顿，
  硬顶仅防单段爆炸；
- 文章类（网页 / 公众号）：优先尊重原文自己的块分段；当检测到排版器
  「一行一块」的软换行时，再按句末标点与长度合并成语义段。
"""
from __future__ import annotations

import math

# 词汇重叠计算时剔除的标点与格式字符（bigram 只看实文）
_PUNCT_ALL = set("，。、！？…；：,.!?;:（）()【】《》<>「」『』“”‘’\"'\u3000—…·-_=+*/\\|[]{}#&@~^$%")

# 句末标点：中英文句号、问号、感叹、省略、分号，以及常见收尾引号/括号
_SENTENCE_END_CHARS = set("。！？…；!?.")
_SENTENCE_END_TAIL = set("”’」』）)】》\"'")

# 时序类（字幕/ASR）阈值
# 2026-09-15（三次调整）：删除固定字数目标——段落应按语义切分。话题边界
# 用 TextTiling 思想的词汇重叠谷检测（字符 bigram 相似度曲线，无分词依赖）；
# 说话人强停顿（≥1.8s）仍是直接断段信号；硬顶仅防单段爆炸，正常内容触不到。
_TIMING_TILE_WINDOW = 6       # 相似度曲线的左右窗口（句数）
_TIMING_MIN_PARA_SENTS = 4    # 相邻话题断点的最小句距
_TIMING_MIN_DEPTH = 0.01      # 谷深噪声下限（主筛选靠相对最大谷深的比例）
_TIMING_DEPTH_RATIO = 0.4     # 谷深须达全局最大谷深的比例
_TIMED_HARD_CHARS = 600       # 兜底：无谷无停顿时防止单段无限增长
_TIMED_GAP_MS = 1800          # 说话停顿超过这个间隔视为换段
_TIMED_MIN_GAP_CHARS = 20     # 停顿断段的最小长度（防时间戳抖动的假停顿）

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


def _timed_gap_ms(a: dict, b: dict) -> float | None:
    if a.get("end_ms") is None or b.get("start_ms") is None:
        return None
    return int(b["start_ms"]) - int(a["end_ms"])


def _tile_tokens(text: str) -> set[str]:
    """字符 bigram 集合：中文无需分词的词汇重叠度量；标点与空白不计。"""
    chars = [c.lower() for c in text if not c.isspace() and c not in _PUNCT_ALL]
    if len(chars) < 2:
        return set(chars)
    return {chars[i] + chars[i + 1] for i in range(len(chars) - 1)}


def _tile_breaks(segments: list[dict]) -> set[int]:
    """TextTiling 式话题边界：返回应断段的缝隙下标（句 i 与 i+1 之间）。

    相似度曲线 = 左右各 w 句的 tf-idf 加权余弦（字符 bigram；几乎每句都
    出现的功能字组 idf 趋零，由内容词主导）；曲线的显著深谷即话题转换点。
    谷深要求同时满足绝对下限与相对全局最大深度的比例，且相邻断点之间
    至少相距 MIN_PARA_SENTS 句。
    """
    n = len(segments)
    if n < 2 * _TIMING_TILE_WINDOW:
        return set()
    toks = [_tile_tokens(_text(s)) for s in segments]
    df: dict[str, int] = {}
    for t in toks:
        for g in t:
            df[g] = df.get(g, 0) + 1
    idf = {g: math.log(n / c) for g, c in df.items()}

    def vec(lo: int, hi: int) -> dict[str, float]:
        out: dict[str, float] = {}
        for t in toks[lo:hi]:
            for g in t:
                out[g] = out.get(g, 0.0) + idf.get(g, 0.0)
        return out

    def cos(a: dict[str, float], b: dict[str, float]) -> float | None:
        na = math.sqrt(sum(v * v for v in a.values()))
        nb = math.sqrt(sum(v * v for v in b.values()))
        if not na or not nb:
            return None
        small, big = (a, b) if len(a) <= len(b) else (b, a)
        dot = sum(v * big.get(g, 0.0) for g, v in small.items())
        return dot / (na * nb)

    w = _TIMING_TILE_WINDOW
    sims: list[float | None] = [None] * (n - 1)
    for i in range(n - 1):
        lo, hi = i - w + 1, i + 1
        if lo < 0 or hi + w > n:
            continue
        sims[i] = cos(vec(lo, hi), vec(hi, hi + w))
    cands: list[tuple[int, float]] = []
    for i, s in enumerate(sims):
        if s is None:
            continue
        lpeak = max((x for x in sims[max(0, i - w):i] if x is not None), default=None)
        rpeak = max((x for x in sims[i + 1:i + w] if x is not None), default=None)
        if lpeak is None or rpeak is None or s > lpeak or s > rpeak:
            continue  # 只取局部极小值
        depth = (lpeak + rpeak) / 2 - s
        if depth >= _TIMING_MIN_DEPTH:
            cands.append((i, depth))
    if not cands:
        return set()
    limit = max(d for _, d in cands) * _TIMING_DEPTH_RATIO
    out: set[int] = set()
    for i, d in sorted(cands, key=lambda x: -x[1]):
        if d < limit or any(abs(i - b) < _TIMING_MIN_PARA_SENTS for b in out):
            continue
        out.add(i)
    return out


def _group_timed(segments: list[dict]) -> list[tuple[list[dict], str]]:
    n = len(segments)
    breaks = _tile_breaks(segments)
    groups: list[tuple[list[dict], str]] = []
    cur: list[dict] = []
    chars = 0
    for i, seg in enumerate(segments):
        cur.append(seg)
        chars += len(_text(seg))
        is_break = False
        if i < n - 1:
            if i in breaks:
                is_break = True  # 话题边界（词汇重叠谷）
            else:
                gap = _timed_gap_ms(seg, segments[i + 1])
                if gap is not None and gap >= _TIMED_GAP_MS and chars >= _TIMED_MIN_GAP_CHARS:
                    is_break = True  # 说话人强停顿
        if i == n - 1 or is_break or chars >= _TIMED_HARD_CHARS:
            _close(cur, groups)
            chars = 0
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
