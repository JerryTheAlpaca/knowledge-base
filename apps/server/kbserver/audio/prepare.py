"""通用音频准备（docs/13 §3、§7）：远程流与本地对象 → 磁盘短 WAV 段。

- 远程输入：受限 HTTP → FFmpeg stdin 管道（不可 seek，B 站 DASH 独立音轨）。
- 对象输入：已授权的本地对象文件路径 → FFmpeg 直接读取（可 seek，兼容
  moov 在尾部的常见 M4A）；不把整文件读成 bytes，也不经自身 HTTP 下载绕行。
- 两种输入只在「如何提供音频字节」处不同，输出 PreparedAudio 完全一致。

时长约束三层落实（docs/13 §7.1）：
1. 已知可信 duration_hint 提前拒绝；
2. 解码过程中按产出段数监测（超限即终止，不多解码到无限）；
3. 最终提交按实际采样数再次校验，超限即报 audio_too_long，绝不截掉
   10 小时之后的内容再宣称完整转写。
"""
from __future__ import annotations

import hashlib
import math
import os
import subprocess
import threading
import wave
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from ..security.safe_fetch import SafeFetchError, stream_to_sink
from .types import (
    AudioPrepareAborted,
    AudioPrepareError,
    ObjectAudioInput,
    PreparedAudio,
    PreparedChunk,
    RemoteAudioInput,
)

# 时长上限的绝对容差（秒）：吸收 WAV 段边界对齐误差，不放大成百分比容差
DURATION_EPSILON_S = 0.5
# 对象输入无字节回调：轮询子进程以持续响应取消/续租/让出
OBJECT_POLL_INTERVAL_S = 2.0
_FFMPEG_WAIT_S = 60


@dataclass
class AudioLimits:
    """一次准备使用的资源上限与工具配置。"""

    ffmpeg_bin: str
    chunk_seconds: int
    max_bytes: int
    max_duration_s: float


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


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


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
    except Exception:  # noqa: BLE001 —— 读取线程不得影响主流程
        pass


def _stderr_text(tail: deque) -> str:
    text = b"\n".join(tail).decode("utf-8", errors="replace")
    # 临时签名 URL 不进日志：只保留不含 http 的行
    return "\n".join(ln for ln in text.splitlines() if "http" not in ln.lower())


def _ffmpeg_command(limits: AudioLimits, chunks_dir: Path, source_arg: str) -> list[str]:
    return [
        limits.ffmpeg_bin,
        "-hide_banner", "-loglevel", "warning", "-nostdin",
        "-i", source_arg,
        "-vn", "-map", "0:a:0",
        "-ac", "1", "-ar", "16000", "-sample_fmt", "s16",
        "-f", "segment", "-segment_time", str(limits.chunk_seconds),
        "-segment_format", "wav", "-reset_timestamps", "1",
        str(chunks_dir / "chunk-%04d.wav"),
    ]


def _spawn(command: list[str], *, use_stdin: bool) -> subprocess.Popen:
    popen_kwargs: dict = {}
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True  # 独立进程组，便于整组回收
    return subprocess.Popen(
        command,
        stdin=subprocess.PIPE if use_stdin else subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        **popen_kwargs,
    )


def prepare_audio(audio_input, attempt_dir: Path, *, limits: AudioLimits,
                  monitor=None) -> PreparedAudio:
    """把一条音频准备成磁盘短 WAV 段；返回 PreparedAudio（未提交清单）。

    monitor(bytes_read) 由调用方提供，用于续租/取消/让出检查；返回 "yield"
    或 "cancel" 时立即中止（抛 AudioPrepareAborted）。远程主地址失败只依序
    尝试同一音轨的备选地址；全部失败向上抛错。
    """
    hint = audio_input.duration_hint
    if hint and hint > limits.max_duration_s + DURATION_EPSILON_S:
        raise AudioPrepareError(
            "audio_too_long",
            f"音频时长（{hint / 60:.0f} 分钟）超过单条上限（{limits.max_duration_s / 60:.0f} 分钟）。",
        )

    chunks_dir = attempt_dir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    for old in chunks_dir.glob("chunk-*.wav"):
        old.unlink()  # 同一 attempt 重试时清掉旧段，不混用两次获取的 PCM

    # 允许最多多解码一个探测段用于发现超限；不得无限解码无 duration 的输入
    max_chunks = max(1, math.ceil(limits.max_duration_s / max(1, limits.chunk_seconds)))
    probe_limit = max_chunks + 1

    if isinstance(audio_input, ObjectAudioInput):
        return _prepare_object(audio_input, attempt_dir, limits, monitor, probe_limit)
    return _prepare_remote(audio_input, attempt_dir, limits, monitor, probe_limit)


def _finalize(attempt_dir: Path, chunks_dir: Path, limits: AudioLimits,
              source_bytes: int, expected: float, stream_error: Exception | None,
              stderr_tail: deque, returncode: int,
              *, strict_duration: bool) -> PreparedAudio:
    if returncode != 0:
        tail = _stderr_text(stderr_tail)
        if stream_error is None:
            # 输入完整送达但解码失败：按不可管道解码处理，绝不回退整文件下载
            raise AudioPrepareError(
                "audio_stream_unsupported",
                f"FFmpeg 无法解码该音频（exit {returncode}）：{tail[:200]}",
            )
        raise AudioPrepareError("network_error", f"音频流读取失败：{stream_error}")

    chunk_paths = sorted(chunks_dir.glob("chunk-*.wav"))
    if not chunk_paths:
        raise AudioPrepareError("empty_audio", "音频解码后没有产生任何片段（空流或无音轨）。")

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

    # 硬上限：按实际采样数核算（第 3 层）
    if total > limits.max_duration_s + DURATION_EPSILON_S:
        raise AudioPrepareError(
            "audio_too_long",
            f"解码总时长（{total:.0f}s）超过单条上限"
            f"（{limits.max_duration_s:.0f}s）；未截断，请拆分后重试。",
        )
    if strict_duration and expected > 0:
        tolerance = max(2.0, expected * 0.02)
        if abs(total - expected) > tolerance:
            raise AudioPrepareError(
                "duration_mismatch",
                f"解码总时长（{total:.0f}s）与来源声明时长（{expected:.0f}s）不符，可能被静默截断。",
            )

    return PreparedAudio(
        chunks_dir="chunks", chunks=chunks, total_duration=total,
        expected_duration=expected, source_bytes=source_bytes,
    )


def _prepare_remote(inp: RemoteAudioInput, attempt_dir: Path, limits: AudioLimits,
                    monitor, probe_limit: int) -> PreparedAudio:
    chunks_dir = attempt_dir / "chunks"
    proc = _spawn(_ffmpeg_command(limits, chunks_dir, "pipe:0"), use_stdin=True)
    stderr_tail: deque = deque(maxlen=40)
    stderr_thread = threading.Thread(target=_drain_stderr, args=(proc, stderr_tail), daemon=True)
    stderr_thread.start()

    state = {"abort": None}

    def _check(bytes_read: int):
        produced = len(list(chunks_dir.glob("chunk-*.wav")))
        if produced > probe_limit:
            state["abort"] = AudioPrepareError(
                "audio_too_long",
                f"音频超过单条上限（{limits.max_duration_s / 60:.0f} 分钟），已停止解码。",
            )
            _terminate(proc)
            raise state["abort"]
        if monitor is not None:
            verdict = monitor(bytes_read)
            if verdict in ("yield", "cancel"):
                _terminate(proc)
                raise AudioPrepareAborted(verdict)

    source_bytes = 0
    stream_error: SafeFetchError | None = None
    try:
        for url in inp.urls:
            try:
                # FFmpeg stdin 满时 write 阻塞 → HTTP 读取暂停：管道背压限流
                result = stream_to_sink(
                    url,
                    sink=lambda chunk: _write_or_die(proc, chunk),
                    max_bytes=limits.max_bytes,
                    timeout=30.0,
                    headers=inp.headers or None,
                    should_cancel=lambda: False,  # 取消统一走 monitor（顺带续租）
                    on_progress=_check,
                )
                source_bytes = result.bytes_read
                stream_error = None
                break
            except BrokenPipeError:
                # FFmpeg 提前退出：按不可管道解码处理，不换地址重试
                _terminate(proc)
                if state["abort"] is not None:
                    raise state["abort"]
                tail = _stderr_text(stderr_tail)
                raise AudioPrepareError(
                    "audio_stream_unsupported",
                    f"FFmpeg 管道提前退出：{tail[:200] or '(无 stderr)'}",
                ) from None
            except SafeFetchError as exc:
                stream_error = exc
                if exc.code == "CANCELLED":
                    raise AudioPrepareAborted("cancel") from exc
                continue  # 只试同一音频的备选地址
        if stream_error is not None:
            # 统一成通用准备错误：调用方按 status=network_error 做有限退避
            raise AudioPrepareError("network_error", f"音频流读取失败：{stream_error}")
    finally:
        _close_stdin(proc)
        _drain_and_wait(proc, stderr_thread)

    return _finalize(attempt_dir, chunks_dir, limits, source_bytes,
                     inp.duration_hint or 0.0, stream_error, stderr_tail, proc.returncode,
                     strict_duration=inp.strict_duration)


def _prepare_object(inp: ObjectAudioInput, attempt_dir: Path, limits: AudioLimits,
                    monitor, probe_limit: int) -> PreparedAudio:
    """本地对象 → FFmpeg（可 seek）；无字节回调，轮询子进程响应取消/让出。"""
    chunks_dir = attempt_dir / "chunks"
    if not Path(inp.path).exists():
        raise AudioPrepareError("network_error", "上传原件在对象存储中缺失，需重新上传。")
    proc = _spawn(_ffmpeg_command(limits, chunks_dir, str(inp.path)), use_stdin=False)
    stderr_tail: deque = deque(maxlen=40)
    stderr_thread = threading.Thread(target=_drain_stderr, args=(proc, stderr_tail), daemon=True)
    stderr_thread.start()

    too_long: AudioPrepareError | None = None
    try:
        while proc.poll() is None:
            produced = len(list(chunks_dir.glob("chunk-*.wav")))
            if produced > probe_limit:
                too_long = AudioPrepareError(
                    "audio_too_long",
                    f"音频超过单条上限（{limits.max_duration_s / 60:.0f} 分钟），已停止解码。",
                )
                _terminate(proc)
                break
            if monitor is not None:
                verdict = monitor(produced * limits.chunk_seconds)
                if verdict in ("yield", "cancel"):
                    _terminate(proc)
                    raise AudioPrepareAborted(verdict)
            try:
                proc.wait(timeout=OBJECT_POLL_INTERVAL_S)
            except subprocess.TimeoutExpired:
                pass
    finally:
        _close_stdin(proc)
        _drain_and_wait(proc, stderr_thread)

    if too_long is not None:
        raise too_long
    return _finalize(attempt_dir, chunks_dir, limits, inp.size_bytes,
                     inp.duration_hint or 0.0, None, stderr_tail, proc.returncode,
                     strict_duration=False)


def _close_stdin(proc: subprocess.Popen) -> None:
    try:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
    except OSError:
        pass


def _drain_and_wait(proc: subprocess.Popen, stderr_thread: threading.Thread) -> None:
    try:
        proc.wait(timeout=_FFMPEG_WAIT_S)
    except subprocess.TimeoutExpired:
        _terminate(proc)
    stderr_thread.join(timeout=2)
