"""通用音频准备（docs/13 §3、§7）：远程流与本地对象 → 磁盘短 WAV 段。

- 远程输入：受限 HTTP 整条落到本次 attempt 的临时文件 → FFmpeg 读取该文件。
- 对象输入：已授权的本地对象文件路径 → FFmpeg 直接读取。
- 两者只在「音频字节怎样变成本地文件」处不同，解码路径与输出 PreparedAudio
  完全一致；都不把整文件读成 bytes，也不经自身 HTTP 下载绕行。

先落盘再解码，是为了让输入可 seek：moov 在尾部的常见 M4A 不再因为管道不可
seek 而被判成不可解码（那一类失败原先是终态、不重试）。远程输入不做断点续传
（docs/11 §5.3）：中断即整条作废重下。

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
import time
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
# 解码期间轮询子进程以持续响应取消/续租/让出
DECODE_POLL_INTERVAL_S = 2.0
_FFMPEG_WAIT_S = 60
# 远程输入落到本次 attempt 目录的临时文件名；解码产物齐全后立即删除。
# 不带容器后缀，让 FFmpeg 按内容探测格式（与原先读管道时的行为一致）。
REMOTE_INPUT_FILE = "input.bin"


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


def _spawn(command: list[str]) -> subprocess.Popen:
    popen_kwargs: dict = {}
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True  # 独立进程组，便于整组回收
    return subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
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
              source_bytes: int, expected: float, stderr_tail: deque, returncode: int,
              *, strict_duration: bool) -> PreparedAudio:
    if returncode != 0:
        # 输入已完整落到本地文件，解码仍失败就是容器/编码本身不支持；
        # 原先「管道不可 seek」造成的那一类误判已经不存在
        raise AudioPrepareError(
            "audio_stream_unsupported",
            f"FFmpeg 无法解码该音频（exit {returncode}）："
            f"{_stderr_text(stderr_tail)[:200]}",
        )

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


def _download_guard(monitor):
    """下载进度回调：只做续租/让出/取消检查；超限判断在解码阶段按产出段数做。"""

    def on_progress(bytes_read: int) -> None:
        if monitor is None:
            return
        verdict = monitor(bytes_read)
        if verdict in ("yield", "cancel"):
            raise AudioPrepareAborted(verdict)

    return on_progress


def _download_to_file(inp: RemoteAudioInput, path: Path, limits: AudioLimits,
                      monitor) -> int:
    """受限 HTTP 把整条音频写进本地文件；返回实际字节数。

    不做断点续传：备选地址、以及重新解析出来的签名地址都可能是新的，按字节拼接
    两次获取的结果有把两份不同音频缝在一起的风险。中断即整条作废重下（docs/11
    §5.3）。主地址失败只依序尝试同一音轨的备选地址。
    """
    stream_error: SafeFetchError | None = None
    with open(path, "wb") as fh:
        for url in inp.urls:
            try:
                result = stream_to_sink(
                    url,
                    sink=fh.write,
                    max_bytes=limits.max_bytes,
                    timeout=30.0,
                    headers=inp.headers or None,
                    should_cancel=lambda: False,  # 取消统一走 monitor（顺带续租）
                    on_progress=_download_guard(monitor),
                )
                return result.bytes_read
            except SafeFetchError as exc:
                stream_error = exc
                if exc.code == "CANCELLED":
                    raise AudioPrepareAborted("cancel") from exc
                fh.seek(0)  # 换地址从头再写，不拼接两次获取的字节
                fh.truncate()
                continue
    raise AudioPrepareError("network_error", f"音频下载失败：{stream_error}")


def _decode_file(source_path: Path, attempt_dir: Path, limits: AudioLimits,
                 monitor, probe_limit: int) -> tuple[int, deque]:
    """本地音频文件 → FFmpeg 短 WAV 段（输入可 seek）；轮询子进程响应取消/让出。"""
    chunks_dir = attempt_dir / "chunks"
    proc = _spawn(_ffmpeg_command(limits, chunks_dir, str(source_path)))
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
                proc.wait(timeout=DECODE_POLL_INTERVAL_S)
            except subprocess.TimeoutExpired:
                pass
    finally:
        _drain_and_wait(proc, stderr_thread)

    if too_long is not None:
        raise too_long
    return proc.returncode, stderr_tail


def _prepare_remote(inp: RemoteAudioInput, attempt_dir: Path, limits: AudioLimits,
                    monitor, probe_limit: int) -> PreparedAudio:
    """远程输入：整条落到本次 attempt 的临时文件 → 解码 → 立即删掉临时文件。

    临时文件活不过本次准备：解码产物已经在 chunks/ 里，留着只是多占一份盘。
    """
    input_path = attempt_dir / REMOTE_INPUT_FILE
    chunks_dir = attempt_dir / "chunks"
    started = time.monotonic()
    try:
        source_bytes = _download_to_file(inp, input_path, limits, monitor)
        downloaded = time.monotonic()
        returncode, stderr_tail = _decode_file(
            input_path, attempt_dir, limits, monitor, probe_limit)
    finally:
        try:
            input_path.unlink(missing_ok=True)
        except OSError:
            pass
    decoded = time.monotonic()
    prepared = _finalize(attempt_dir, chunks_dir, limits, source_bytes,
                         inp.duration_hint or 0.0, stderr_tail, returncode,
                         strict_duration=inp.strict_duration)
    prepared.timings = {"input_kind": "remote",
                        "download_s": downloaded - started,
                        "decode_s": decoded - downloaded,
                        "finalize_s": time.monotonic() - decoded}
    return prepared


def _prepare_object(inp: ObjectAudioInput, attempt_dir: Path, limits: AudioLimits,
                    monitor, probe_limit: int) -> PreparedAudio:
    """上传原件：服务端解析出的已授权路径直接交给 FFmpeg；原件本身不动。"""
    if not Path(inp.path).exists():
        raise AudioPrepareError("network_error", "上传原件在对象存储中缺失，需重新上传。")
    started = time.monotonic()
    returncode, stderr_tail = _decode_file(
        Path(inp.path), attempt_dir, limits, monitor, probe_limit)
    decoded = time.monotonic()
    prepared = _finalize(attempt_dir, attempt_dir / "chunks", limits, inp.size_bytes,
                         inp.duration_hint or 0.0, stderr_tail, returncode,
                         strict_duration=False)
    prepared.timings = {"input_kind": "object",
                        "decode_s": decoded - started,
                        "finalize_s": time.monotonic() - decoded}
    return prepared


def _drain_and_wait(proc: subprocess.Popen, stderr_thread: threading.Thread) -> None:
    try:
        proc.wait(timeout=_FFMPEG_WAIT_S)
    except subprocess.TimeoutExpired:
        _terminate(proc)
    stderr_thread.join(timeout=2)
