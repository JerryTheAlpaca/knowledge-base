"""B 站播放接口音频流获取（docs/11 §5、docs/13 §4.1）。

职责：
- resolve_audio_stream：解析当前分 P 播放接口，选普通 AAC 独立音轨
  （较低带宽即可满足识别），校验时长归属；临时地址只存在于本次执行。
- bilibili_audio_input：把解析结果包装成通用 RemoteAudioInput（来源= B 站，
  获取方式= player_audio_stream），供 audio.prepare 统一解码，不复制解码逻辑。

接口依据（以 yt-dlp 锁定版本的 B 站实现为参考，2026-09 内部路径）：
- 登录态走 /x/player/wbi/playurl（WBI 签名 + buvid3），匿名走 /x/player/playurl；
  fnval=16 请求 DASH。data.dash.audio[] 为普通音轨；Dolby/FLAC 在
  data.dash.dolby / data.dash.flac，一律不取。
- 音频 CDN 不携带 Cookie（docs/05 §3.2 同规则）；下载需要 UA + Referer 防盗链。

失败状态区分（不统一标 no_track，docs/11 §5.3）：
- audio_stream_unsupported：无普通独立音轨，或该音轨的编码 FFmpeg 解不了。
- video_truncated：接口音频时长明显短于视频（试看截断）。
- duration_mismatch：解码总时长与接口时长不符（静默截断）。
- login_required / blocked / network_error：与字幕适配器同语义。

准备阶段把整条音轨落到本次 attempt 的临时文件后再解码（输入可 seek），但不做
任意字节 Range 断点续传：签名地址每次重新解析，拼接两次获取的字节有把两份不同
音频缝在一起的风险。中断后由调用方删除 attempt 目录重做（§5.3）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode

from ..audio.prepare import AudioLimits, prepare_audio as _prepare_audio
from ..audio.types import (
    ACQ_PLAYER_STREAM,
    AudioPrepareError,
    AudioSource,
    AudioSourceError,
    ObjectAudioInput,  # noqa: F401 —— 便于调用方从本模块拿到通用输入类型
    PreparedAudio,
    RemoteAudioInput,
)
from ..security.safe_fetch import stream_to_sink  # noqa: F401 —— 兼容既有测试替换点
from . import bilibili as bili

EXTRACTOR_VERSION = "bilibili_audio-1.1.0"
ADAPTER_ID = "bilibili_audio"

_PLAY_URL_WBI = "https://api.bilibili.com/x/player/wbi/playurl"
_PLAY_URL_ANON = "https://api.bilibili.com/x/player/playurl"


class BilibiliAudioError(AudioSourceError):
    """B 站音频获取/准备失败（通用 AudioSourceError 的 B 站子类）。"""


@dataclass
class AudioStream:
    """一次播放接口解析出的音轨；临时地址只在本对象存活期间有效。"""

    stream_id: int
    codec: str
    bandwidth: int
    duration_s: float
    base_url: str
    backup_urls: list[str] = field(default_factory=list)
    size_hint: int | None = None


def _backup_list(entry: dict) -> list[str]:
    raw = entry.get("backup_url")
    if isinstance(raw, str):
        return [raw] if raw else []
    if isinstance(raw, list):
        return [u for u in raw if isinstance(u, str) and u]
    return []


def resolve_audio_stream(ref: "bili.VideoRef", page: dict, *, sessdata: str | None,
                         max_bytes: int) -> AudioStream:
    """解析当前分 P 播放接口，选最低带宽的普通 AAC 独立音轨（docs/11 §5.1）。"""
    params = {"cid": page["cid"], "fnval": "16", "qn": "16"}
    if ref.bvid:
        params["bvid"] = ref.bvid
    else:
        params["aid"] = ref.aid or ""
    if sessdata:
        device_id = bili._device_fingerprint(max_bytes)
        play_url = _PLAY_URL_WBI + "?" + urlencode(
            bili._wbi_sign(params, bili._wbi_mix_key(sessdata, max_bytes))
        )
    else:
        device_id = None
        play_url = _PLAY_URL_ANON + "?" + urlencode(params)
    try:
        data = bili._api(bili._fetch_json(play_url, max_bytes=max_bytes,
                                          sessdata=sessdata, device_id=device_id))
    except bili.BilibiliError:
        raise

    dash = data.get("dash") or {}
    audio_list = dash.get("audio") or []
    if not isinstance(audio_list, list) or not audio_list:
        # Dolby/FLAC 属于另外的字段，明确不作为普通音轨替代（docs/11 §5.1）
        raise BilibiliAudioError(
            "audio_stream_unsupported",
            "播放接口没有普通独立音频轨（Dolby/高解析音轨不用于转写）。",
        )

    expected = float(dash.get("duration") or 0)
    page_dur = page.get("page_duration_s") or page.get("duration_s")
    if page_dur and expected > 0 and expected < page_dur * 0.9:
        raise BilibiliAudioError(
            "video_truncated",
            f"接口音频时长（{expected:.0f}s）明显短于视频时长（{page_dur:.0f}s），疑似试看截断，已拒绝。",
        )

    best = min(audio_list, key=lambda a: int(a.get("bandwidth") or 0))
    base = best.get("base_url") or best.get("baseUrl") or ""
    if not base:
        raise BilibiliAudioError("audio_stream_unsupported", "音轨缺少可下载地址。")
    return AudioStream(
        stream_id=int(best.get("id") or 0),
        codec=str(best.get("codecs") or ""),
        bandwidth=int(best.get("bandwidth") or 0),
        duration_s=expected,
        base_url=base,
        backup_urls=_backup_list(best),
        size_hint=int(best.get("size") or 0) or None,
    )


def bilibili_audio_input(ref: "bili.VideoRef", page: dict, stream: AudioStream) -> RemoteAudioInput:
    """把 B 站音轨包装成通用远程输入：来源 B 站，受限请求策略只用于本次获取。

    media CDN 需要 UA + Referer 防盗链；通用准备层不得默认给所有站点附 B 站
    Referer，也不能把 Cookie 传播到普通网页域名（docs/13 §4.1）。
    """
    canonical = page.get("canonical_url")
    source = AudioSource(
        platform="bilibili",
        media_kind="video",
        adapter_id=ADAPTER_ID,
        adapter_version=EXTRACTOR_VERSION,
        original_url=canonical,
        canonical_url=canonical,
        title=page.get("title"),
        author=page.get("author"),
        published_at=page.get("published_at"),
        duration_hint=stream.duration_s or None,
        acquisition=ACQ_PLAYER_STREAM,
        source_locator={
            "type": "bilibili_video",
            "bvid": ref.bvid,
            "aid": ref.aid,
            "cid": page.get("cid"),
            "part": page.get("page"),
            "pages_count": page.get("pages_count"),
        },
    )
    return RemoteAudioInput(
        source=source,
        urls=[stream.base_url, *stream.backup_urls],
        headers=bili._browser_headers(stream.base_url),
        duration_hint=stream.duration_s or None,
        stream_meta={
            "stream_id": stream.stream_id,
            "codec": stream.codec,
            "bandwidth": stream.bandwidth,
        },
        # 接口时长用于识别静默截断，声明可信 → 保留一致性校验
        strict_duration=True,
    )


def source_fingerprint(ref: "bili.VideoRef", page: dict, stream: AudioStream) -> str:
    """B 站输入指纹：BV/aid + cid + 音轨稳定标识（不用带时效签名的 URL）。"""
    bvid = ref.bvid or f"av{ref.aid}"
    return f"bilibili:{bvid}:P{page.get('page')}:cid{page.get('cid')}:a{stream.stream_id}:b{stream.bandwidth}"


def prepare_audio(stream, attempt_dir: Path, *, ffmpeg_bin: str, chunk_seconds: int,
                  max_bytes: int, max_duration_s: float, monitor=None) -> PreparedAudio:
    """兼容旧调用：单音轨字节流 → 通用准备（保持既有签名）。"""
    from ..audio.types import AudioSource as _AS

    source = _AS(platform="bilibili", media_kind="video", adapter_id=ADAPTER_ID,
                 adapter_version=EXTRACTOR_VERSION, duration_hint=stream.duration_s or None,
                 acquisition=ACQ_PLAYER_STREAM)
    inp = RemoteAudioInput(source=source, urls=[stream.base_url, *stream.backup_urls],
                           headers=bili._browser_headers(stream.base_url),
                           duration_hint=stream.duration_s or None, strict_duration=True)
    return _prepare_audio(
        inp, attempt_dir,
        limits=AudioLimits(ffmpeg_bin=ffmpeg_bin, chunk_seconds=chunk_seconds,
                           max_bytes=max_bytes, max_duration_s=max_duration_s),
        monitor=monitor,
    )


__all__ = [
    "ADAPTER_ID",
    "AudioPrepareError",
    "AudioStream",
    "BilibiliAudioError",
    "EXTRACTOR_VERSION",
    "PreparedAudio",
    "RemoteAudioInput",
    "bilibili_audio_input",
    "prepare_audio",
    "resolve_audio_stream",
    "source_fingerprint",
    "stream_to_sink",
]
