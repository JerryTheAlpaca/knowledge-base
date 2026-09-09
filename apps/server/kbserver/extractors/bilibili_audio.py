"""B 站播放接口音频流获取与受限管道解码（docs/11 §5）。

职责（docs/11 §9.1 契约）：
- resolve_audio_stream：解析当前分 P 播放接口，选普通 AAC 独立音轨
  （较低带宽即可满足识别），校验时长归属；临时地址只存在于本次执行。
- prepare_audio：把响应字节经有界 HTTP 流写进 FFmpeg stdin，边读边解码为
  16kHz/单声道/16-bit 短 WAV 段；不落完整压缩音频，不一次性读入内存。

接口依据（以 yt-dlp 锁定版本的 B 站实现为参考，2026-09 内部路径）：
- 登录态走 /x/player/wbi/playurl（WBI 签名 + buvid3），匿名走 /x/player/playurl；
  fnval=16 请求 DASH。data.dash.audio[] 为普通音轨；Dolby/FLAC 在
  data.dash.dolby / data.dash.flac，一律不取。
- 音频 CDN 不携带 Cookie（docs/05 §3.2 同规则）；下载需要 UA + Referer 防盗链。

失败状态区分（不统一标 no_track，docs/11 §5.3）：
- audio_stream_unsupported：无普通独立音轨，或该格式无法经不可 seek 的管道解码。
- video_truncated：接口音频时长明显短于视频（试看截断）。
- duration_mismatch：解码总时长与接口时长不符（静默截断）。
- login_required / blocked / network_error：与字幕适配器同语义。

准备阶段不做任意字节 Range 断点：中断后由调用方删除 attempt 目录重做（§5.3）。
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import threading
import wave
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode

from ..security.safe_fetch import SafeFetchError, stream_to_sink
from . import bilibili as bili

EXTRACTOR_VERSION = "bilibili_audio-1.0.0"

_PLAY_URL_WBI = "https://api.bilibili.com/x/player/wbi/playurl"
_PLAY_URL_ANON = "https://api.bilibili.com/x/player/playurl"


class BilibiliAudioError(Exception):
    """音频获取/准备失败。status 由调用方区分重试、让出或进入补充材料。"""

    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class AudioPrepareAborted(Exception):
    """准备被外部中止（让出或取消），不是错误；reason 为 yield|cancel。"""

    def __init__(self, reason: str):
        super().__init__(f"音频准备被中止（{reason}）")
        self.reason = reason


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


@dataclass
class PreparedChunk:
    """一个已解码 WAV 段；时间为原视频绝对秒。"""

    index: int
    path: str          # 相对工作目录
    core_start: float
    core_end: float
    duration: float    # 按实际采样数计算
    bytes: int
    sha256: str


@dataclass
class PreparedAudio:
    """完整准备结果：原子提交清单前不得作为转写输入（docs/11 §9.1）。"""

    chunks_dir: str
    chunks: list[PreparedChunk]
    total_duration: float
    expected_duration: float
    source_bytes: int
    meta: dict = field(default_factory=dict)  # 归属信息（bvid/cid/音轨标识等）


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


def _terminate(proc: subprocess.Popen) -> None:
    """回收解码子进程；POSIX 杀整个进程组，避免孤儿进程继续吃内存（docs/11 §6.3）。"""
    if proc.poll() is not None:
        return
    try:
        if os.name == "posix":
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        else:
            proc.terminate()
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        frames = w.getnframes()
        rate = w.getframerate()
    return frames / rate if rate else 0.0


def prepare_audio(
    stream: AudioStream,
    attempt_dir: Path,
    *,
    ffmpeg_bin: str,
    chunk_seconds: int,
    max_bytes: int,
    max_duration_s: float,
    monitor=None,
) -> PreparedAudio:
    """受限 HTTP → FFmpeg 管道 → 磁盘短 WAV 段（docs/11 §5.2）。

    monitor(bytes_read) 由调用方提供，用于续租/取消/让出检查；返回 "yield"
    或 "cancel" 时立即中止（抛 AudioPrepareAborted）。主地址失败只依序尝试
    同一音轨的备选地址；全部失败向上抛错，由调用方决定是否重新解析一次。
    """
    if stream.duration_s > max_duration_s:
        raise BilibiliAudioError(
            "audio_too_long",
            f"音频时长（{stream.duration_s / 60:.0f} 分钟）超过单条上限（{max_duration_s / 60:.0f} 分钟）。",
        )

    chunks_dir = attempt_dir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    for old in chunks_dir.glob("chunk-*.wav"):
        old.unlink()  # 同一 attempt 重试时清掉旧段，不混用两次获取的 PCM

    command = [
        ffmpeg_bin,
        "-hide_banner", "-loglevel", "warning", "-nostdin",
        "-i", "pipe:0",
        "-vn", "-map", "0:a:0",
        "-ac", "1", "-ar", "16000", "-sample_fmt", "s16",
        "-f", "segment", "-segment_time", str(chunk_seconds),
        "-segment_format", "wav", "-reset_timestamps", "1",
        str(chunks_dir / "chunk-%04d.wav"),
    ]
    popen_kwargs = {}
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True  # 独立进程组，便于整组回收
    proc = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, **popen_kwargs,
    )
    stderr_tail: deque[bytes] = deque(maxlen=40)
    stderr_thread = threading.Thread(target=_drain_stderr, args=(proc, stderr_tail), daemon=True)
    stderr_thread.start()

    def _check(bytes_read: int):
        if monitor is not None:
            verdict = monitor(bytes_read)
            if verdict in ("yield", "cancel"):
                _terminate(proc)
                raise AudioPrepareAborted(verdict)

    source_bytes = 0
    stream_error: SafeFetchError | None = None
    try:
        for url in [stream.base_url, *stream.backup_urls]:
            try:
                # FFmpeg stdin 满时 write 阻塞 → HTTP 读取暂停：管道背压限流
                result = stream_to_sink(
                    url,
                    sink=lambda chunk: _write_or_die(proc, chunk),
                    max_bytes=max_bytes,
                    timeout=30.0,
                    headers=bili._browser_headers(url),
                    should_cancel=lambda: False,  # 取消统一走 monitor（顺带续租）
                    on_progress=_check,
                )
                source_bytes = result.bytes_read
                stream_error = None
                break
            except AudioPrepareAborted:
                raise
            except BrokenPipeError:
                # FFmpeg 提前退出：按不可管道解码处理，不换地址重试
                _terminate(proc)
                tail = _stderr_text(stderr_tail)
                raise BilibiliAudioError(
                    "audio_stream_unsupported",
                    f"FFmpeg 管道提前退出：{tail[:200] or '(无 stderr)'}",
                )
            except SafeFetchError as exc:
                stream_error = exc
                if exc.code == "CANCELLED":
                    raise AudioPrepareAborted("cancel") from exc
                continue  # 只试同一音轨的备选地址
        if stream_error is not None:
            raise stream_error
    finally:
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            _terminate(proc)
        stderr_thread.join(timeout=2)

    if proc.returncode != 0:
        tail = _stderr_text(stderr_tail)
        if stream_error is None:
            # 输入完整送达但解码失败：按不可管道解码处理，绝不回退整文件下载
            raise BilibiliAudioError(
                "audio_stream_unsupported",
                f"FFmpeg 无法经管道解码该音轨（exit {proc.returncode}）：{tail[:200]}",
            )
        raise BilibiliAudioError("network_error", f"音频流读取失败：{stream_error}")

    chunk_paths = sorted(chunks_dir.glob("chunk-*.wav"))
    if not chunk_paths:
        raise BilibiliAudioError("empty_audio", "音频流解码后没有产生任何片段（空流或无音轨）。")

    chunks: list[PreparedChunk] = []
    total = 0.0
    for i, path in enumerate(chunk_paths):
        duration = _wav_duration(path)
        data_len = path.stat().st_size
        sha = _file_sha256(path)
        chunks.append(PreparedChunk(
            index=i, path=f"chunks/{path.name}",
            core_start=round(total, 3), core_end=round(total + duration, 3),
            duration=duration, bytes=data_len, sha256=sha,
        ))
        total += duration

    expected = stream.duration_s
    if expected > 0:
        tolerance = max(2.0, expected * 0.02)
        if abs(total - expected) > tolerance:
            raise BilibiliAudioError(
                "duration_mismatch",
                f"解码总时长（{total:.0f}s）与接口时长（{expected:.0f}s）不符，可能被静默截断。",
            )

    return PreparedAudio(
        chunks_dir="chunks", chunks=chunks, total_duration=total,
        expected_duration=expected, source_bytes=source_bytes,
    )


def _write_or_die(proc: subprocess.Popen, chunk: bytes) -> None:
    """写 FFmpeg stdin；进程已死时抛 BrokenPipeError 由上层归类。"""
    try:
        proc.stdin.write(chunk)
    except BrokenPipeError:
        raise
    except (AttributeError, ValueError) as exc:  # stdin 已关闭
        raise BrokenPipeError("FFmpeg stdin 已关闭") from exc


def _drain_stderr(proc: subprocess.Popen, tail: deque) -> None:
    """持续消费 stderr，防管道死锁；只保留尾部若干行（脱敏后使用）。"""
    try:
        for line in iter(proc.stderr.readline, b""):
            tail.append(line.strip()[:300])
    except Exception:
        pass


def _stderr_text(tail: deque) -> str:
    text = b"\n".join(tail).decode("utf-8", errors="replace")
    # 临时签名 URL 不进日志：只保留不含 http 的行
    return "\n".join(ln for ln in text.splitlines() if "http" not in ln.lower())


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()
