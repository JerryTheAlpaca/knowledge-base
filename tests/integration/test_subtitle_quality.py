"""无标点字幕轨判定（docs/04 §4.6：不改写原文，只决定是否降级转写）。

B 站 AI 字幕轨常输出无标点文本，读起来是一串断不开的字。判定结果用于：
自动转写可用时降级 ASR（拿带标点转写稿）；不可用时保留原文并如实标注。
"""
from __future__ import annotations

from kbserver.extractors.subtitles import looks_unpunctuated


def _segs(text: str) -> list[dict]:
    return [{"segment_id": "s0001", "text": text}]


def test_punctuated_subtitle_is_normal():
    """正常 AI 字幕（标点密度 ~9%）不算无标点。"""
    text = "大家好，我是陪你一起看世界的王骁。本期我们就来聊一聊6月的世界局势。" * 10
    assert looks_unpunctuated(_segs(text)) is False


def test_unpunctuated_subtitle_detected():
    """无标点口语长文判定为无标点。"""
    text = "当然现在这个视频你看不到了但是在当年它是一个现象级的作品后来因为种种原因下架了" * 6
    assert looks_unpunctuated(_segs(text)) is True


def test_short_text_not_judged():
    """全文太短时不判定（样本不足，按有标点处理）。"""
    assert looks_unpunctuated(_segs("当然现在这个视频你看不到了")) is False


def test_mixed_light_punctuation_still_counts():
    """偶尔一两个标点仍判为无标点（密度阈值 1%）。"""
    text = ("这里是一段完全没有标点的口语转写内容" * 40) + "。"
    assert looks_unpunctuated(_segs(text)) is True
