"""阅读层段落分组：把细片段合并成自然段落（证据粒度不变）。"""
from __future__ import annotations

import pytest

from kbserver.extractors import paragraphs as parafmt


def timed_segments(texts, *, gap_ms=300, step_ms=2000, gaps=None):
    """构造带时间的片段（字幕/ASR）；gaps 指定某些位置后的停顿毫秒。"""
    gaps = gaps or {}
    segments = []
    cursor = 0
    for i, text in enumerate(texts, start=1):
        end = cursor + step_ms
        segments.append({
            "segment_id": f"s{i:04d}",
            "start_ms": cursor,
            "end_ms": end,
            "text": text,
        })
        cursor = end + gaps.get(i, gap_ms)
    return segments


def article_segments(texts, kinds=None):
    return [
        {
            "segment_id": f"s{i:04d}",
            "text": t,
            "kind": (kinds[i - 1] if kinds else "paragraph"),
        }
        for i, t in enumerate(texts, start=1)
    ]


def test_article_respects_original_paragraphs():
    """原文自己就是长段落：按原分段，不额外合并。"""
    texts = ["这是一段完整的原文段落，讲了一件完整的事情，长度足够。" * 3 for _ in range(4)]
    paras = parafmt.group_paragraphs(article_segments(texts))
    assert [p["segment_ids"] for p in paras] == [["s0001"], ["s0002"], ["s0003"], ["s0004"]]


def test_article_merges_soft_wrapped_lines():
    """排版器一行一块：合并成语义段，不再一句一行。"""
    texts = ["这是排版器切出来的一行短句。" for _ in range(20)]
    paras = parafmt.group_paragraphs(article_segments(texts))
    assert len(paras) < 5
    assert [s for p in paras for s in p["segment_ids"]] == [f"s{i:04d}" for i in range(1, 21)]
    assert all(p["char_count"] >= 100 for p in paras[:-1])


def test_heading_block_stays_alone():
    """小标题独立成段，不与相邻正文合并。"""
    texts = ["一、前期准备"] + ["这是排版器切出来的一行短句。" for _ in range(14)]
    kinds = ["heading"] + ["paragraph"] * 14
    paras = parafmt.group_paragraphs(article_segments(texts, kinds))
    assert paras[0]["kind"] == "heading"
    assert paras[0]["segment_ids"] == ["s0001"]
    assert paras[0]["text"] == "一、前期准备"
    assert all(p["kind"] == "paragraph" for p in paras[1:])


def test_timed_breaks_on_sentence_end_and_gap():
    """字幕按句末标点累计断段，长停顿提前断段。"""
    texts = ["这是字幕里的一句话内容。" for _ in range(12)]
    segments = timed_segments(texts, gap_ms=300, step_ms=1500, gaps={5: 2500, 9: 2500})
    paras = parafmt.group_paragraphs(segments)
    assert [p["segment_ids"][0] for p in paras] == ["s0001", "s0006", "s0010"]
    assert paras[0]["start_ms"] == 0
    assert paras[0]["end_ms"] == segments[4]["end_ms"]
    assert paras[-1]["end_ms"] == segments[-1]["end_ms"]


def test_timed_breaks_on_topic_lexical_shift():
    """语义切分：词汇域切换处断段，不依赖固定字数与停顿。"""
    topic_finance = [
        "复利是银行存款利息滚动的计算方法，本金和利率共同决定增长。",
        "存款利率越高，复利增长的速度就越快，长期持有收益明显。",
        "银行理财产品的复利收益需要时间沉淀，短期赎回会打断滚动。",
        "指数基金的复利效应同样依赖时间，分红再投入是关键。",
        "所以复利的核心是把收益再投入本金，让利息继续生息。",
        "理解了复利，再看贷款时就明白等额本息的真实成本。",
        "房贷的利息总额往往接近本金，提前还款可以减少滚动负债。",
    ]
    topic_cooking = [
        "火锅底料要先炒香再加高汤炖煮，味道才够厚。",
        "毛肚和鸭肠在火锅里烫几秒就能吃，久了会老。",
        "吃火锅最好搭配香油蒜泥蘸料，降温又提味。",
        "火锅店的味道关键在底料配方和食材新鲜度。",
        "麻辣火锅讲究花椒和辣椒的比例，麻而不燥。",
        "清汤锅底适合涮蔬菜和海鲜，保留原味。",
        "一顿火锅吃到最后，煮几根面条收尾最舒服。",
    ]
    segments = timed_segments(topic_finance + topic_cooking)
    paras = parafmt.group_paragraphs(segments)
    starts = [p["segment_ids"][0] for p in paras]
    assert starts[0] == "s0001" and "s0008" in starts
    # 两个词汇域内部不再被字数规则拆碎
    assert len(paras) <= 3


def test_join_adds_space_only_between_ascii_words():
    paras = parafmt.group_paragraphs(timed_segments(["Hello", "world"]))
    assert paras[0]["text"] == "Hello world"
    paras = parafmt.group_paragraphs(timed_segments(["你好", "世界"]))
    assert paras[0]["text"] == "你好世界"


def test_segment_paragraph_map_covers_every_segment():
    segments = timed_segments(["这是字幕里的一句话内容。" for _ in range(6)])
    paras = parafmt.group_paragraphs(segments)
    mapping = parafmt.segment_paragraph_map(paras)
    assert set(mapping) == {s["segment_id"] for s in segments}
    assert set(mapping.values()) == {p["paragraph_id"] for p in paras}


def test_readable_md_layout():
    texts = ["小标题"] + ["这是排版器切出来的一行短句。" for _ in range(14)]
    paras = parafmt.group_paragraphs(article_segments(texts, ["heading"] + ["paragraph"] * 14))
    md = parafmt.paragraphs_to_readable_md(paras)
    first, second = md.split("\n")[:2]
    assert first == "## 小标题 ^p0001"
    assert second == ""
    assert md.startswith("## 小标题 ^p0001\n\n")
    assert " ^p0002\n" in md


def test_empty_input():
    assert parafmt.group_paragraphs([]) == []
    assert parafmt.group_paragraphs([{"segment_id": "s0001", "text": "  "}]) == []


@pytest.mark.parametrize("text", ["这句话结束了。", "真的吗？", "太好了！", "他说完了。”", "end."])
def test_sentence_end_detection(text):
    assert parafmt._ends_sentence(text)


@pytest.mark.parametrize("text", ["还在说，", "没有结束", "end"])
def test_not_sentence_end(text):
    assert not parafmt._ends_sentence(text)
