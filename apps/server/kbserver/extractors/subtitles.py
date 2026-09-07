"""字幕格式解析与规范化（docs/04 §4.6）。

统一输出片段结构：
    {"segment_id": "s0001", "start_ms": int, "end_ms": int, "text": str,
     "source": "platform_subtitle" | "user_upload" | "tool_exported_srt",
     "original_record_index": int}

规则：
- 时间统一为整数毫秒，保留原始数值对应的定位。
- 负时间、结束早于开始、明显超过视频时长、空正文、结构变化进 warnings；
  原始文件另行留存，规范化层不修改原始数据。
- 字幕允许重叠或重复说话，不做按相同文字的全局去重。
- 不伪造无法解析的内容：解析失败抛 ValueError，由调用方按失败处理。
"""
from __future__ import annotations

import re

# 支持的时间格式：HH:MM:SS,mmm / HH:MM:SS.mmm / MM:SS.mmm / SS.mmm
_TS_PATTERN = re.compile(
    r"^(?:(?P<h>\d+):)?(?:(?P<m>\d{1,2}):)?(?P<s>\d{1,2})(?:[.,](?P<ms>\d{1,3}))?$"
)
_CUE_LINE = re.compile(
    r"^\s*(?P<from>[\d:.,]+)\s*-->\s*(?P<to>[\d:.,]+)"
)


def parse_timestamp(value: str) -> int:
    """把 SRT/VTT 时间戳解析为整数毫秒；非法输入抛 ValueError。"""
    m = _TS_PATTERN.match(value.strip())
    if m is None:
        raise ValueError(f"无法解析时间戳：{value!r}")
    hours = int(m.group("h") or 0)
    minutes = int(m.group("m") or 0)
    seconds = int(m.group("s") or 0)
    ms_text = m.group("ms") or ""
    millis = int(ms_text.ljust(3, "0")) if ms_text else 0
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


# ---- 原始记录：解析层的统一中间结构 ----
# {"start_s": float, "end_s": float, "text": str}

def parse_platform_json(doc) -> list[dict]:
    """B 站字幕 JSON（body[].from/to/content，秒为单位的浮点数）。"""
    if not isinstance(doc, dict):
        raise ValueError("字幕 JSON 不是对象")
    body = doc.get("body")
    if not isinstance(body, list):
        raise ValueError("字幕 JSON 缺少 body 列表（结构变化）")
    records: list[dict] = []
    for i, entry in enumerate(body):
        if not isinstance(entry, dict):
            raise ValueError(f"body[{i}] 不是对象（结构变化）")
        try:
            start = float(entry["from"])
            end = float(entry["to"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"body[{i}] 缺少 from/to 或类型非法") from exc
        text = str(entry.get("content") or "").strip()
        records.append({"start_s": start, "end_s": end, "text": text})
    return records


def parse_srt(text: str) -> list[dict]:
    """SRT：序号行可省略，空行分块，`-->` 分隔起止时间。"""
    records: list[dict] = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n")):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        cue_idx = next((i for i, ln in enumerate(lines) if _CUE_LINE.match(ln)), None)
        if cue_idx is None:
            continue  # 纯序号/注释块
        m = _CUE_LINE.match(lines[cue_idx])
        try:
            start = parse_timestamp(m.group("from"))
            end = parse_timestamp(m.group("to"))
        except ValueError as exc:
            raise ValueError(f"SRT 时间行非法：{lines[cue_idx]!r}") from exc
        body = " ".join(ln.strip() for ln in lines[cue_idx + 1:])
        records.append({"start_s": start / 1000, "end_s": end / 1000, "text": body.strip()})
    if not records:
        raise ValueError("SRT 未解析出任何字幕块")
    return records


def parse_vtt(text: str) -> list[dict]:
    """WebVTT：跳过 WEBVTT 头、NOTE/STYLE 块与 cue 设置行。"""
    records: list[dict] = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n")):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines or lines[0].lstrip().startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        cue_idx = next((i for i, ln in enumerate(lines) if _CUE_LINE.match(ln)), None)
        if cue_idx is None:
            continue
        m = _CUE_LINE.match(lines[cue_idx])
        try:
            start = parse_timestamp(m.group("from"))
            end = parse_timestamp(m.group("to"))
        except ValueError as exc:
            raise ValueError(f"VTT 时间行非法：{lines[cue_idx]!r}") from exc
        body = " ".join(
            ln.strip() for ln in lines[cue_idx + 1:] if not ln.strip().startswith("NOTE")
        )
        records.append({"start_s": start / 1000, "end_s": end / 1000, "text": body.strip()})
    if not records:
        raise ValueError("VTT 未解析出任何字幕块")
    return records


# ---- 规范化 ----

def normalize_records(
    records: list[dict],
    *,
    source: str,
    video_duration_s: float | None = None,
) -> tuple[list[dict], list[str]]:
    """原始记录 → 统一 segments；异常进 warnings，不修改原始文件。

    video_duration_s 为视频时长（秒）；None 表示未知，跳过超长检查。
    """
    warnings: list[str] = []
    segments: list[dict] = []
    duration_ms = int(video_duration_s * 1000) if video_duration_s is not None else None
    for i, rec in enumerate(records):
        text = (rec.get("text") or "").strip()
        if not text:
            warnings.append(f"第 {i + 1} 条记录正文为空，已跳过。")
            continue
        start_ms = int(round(float(rec["start_s"]) * 1000))
        end_ms = int(round(float(rec["end_s"]) * 1000))
        if start_ms < 0:
            warnings.append(f"第 {i + 1} 条记录起始时间为负（{start_ms}ms），已按 0 处理。")
            start_ms = 0
        if end_ms < start_ms:
            warnings.append(f"第 {i + 1} 条记录结束早于开始（{start_ms}->{end_ms}ms），保留原文但时间异常。")
        if duration_ms is not None and start_ms > duration_ms + 5000:
            warnings.append(
                f"第 {i + 1} 条记录起始时间（{start_ms}ms）明显超过视频时长（{duration_ms}ms），保留但请核对。"
            )
        segments.append({
            "segment_id": f"s{len(segments) + 1:04d}",
            "start_ms": start_ms,
            "end_ms": max(end_ms, start_ms),
            "text": text,
            "source": source,
            "original_record_index": i,
        })
    if not segments:
        warnings.append("没有可用的字幕片段。")
    return segments, warnings


def parse_any(data: bytes, *, filename: str = "", mime: str = "") -> tuple[list[dict], str]:
    """按文件名/MIME/内容猜测格式并解析；返回 (records, kind)。

    kind: platform_subtitle_json | srt | vtt。全部失败抛 ValueError。
    """
    lower = (filename or "").lower()
    if lower.endswith(".vtt"):
        return parse_vtt(data.decode("utf-8-sig", errors="replace")), "vtt"
    if lower.endswith(".srt"):
        return parse_srt(data.decode("utf-8-sig", errors="replace")), "srt"
    if lower.endswith(".json") or "json" in (mime or ""):
        import json

        return parse_platform_json(json.loads(data.decode("utf-8-sig", errors="replace"))), "platform_subtitle_json"
    # 无扩展名：按内容嗅探
    head = data[:4096].decode("utf-8-sig", errors="replace")
    if "WEBVTT" in head:
        return parse_vtt(data.decode("utf-8-sig", errors="replace")), "vtt"
    if any(_CUE_LINE.match(ln) for ln in head.splitlines()):
        return parse_srt(data.decode("utf-8-sig", errors="replace")), "srt"
    import json

    return parse_platform_json(json.loads(data.decode("utf-8-sig", errors="replace"))), "platform_subtitle_json"


# ---- 渲染 ----

def _ms_to_srt_ts(ms: int) -> str:
    if ms < 0:
        ms = 0
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, msec = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{msec:03d}"


def segments_to_srt(segments: list[dict]) -> str:
    blocks = []
    for i, seg in enumerate(segments, start=1):
        blocks.append(
            f"{i}\n{_ms_to_srt_ts(seg['start_ms'])} --> {_ms_to_srt_ts(seg['end_ms'])}\n{seg['text']}\n"
        )
    return "\n".join(blocks)


def segments_to_normalized_md(segments: list[dict]) -> str:
    """带块 ID 的规范文字稿，格式与 worker 文本路径一致（`文本 ^s0001`）。"""
    return "".join(f"{seg['text']} ^{seg['segment_id']}\n" for seg in segments)
