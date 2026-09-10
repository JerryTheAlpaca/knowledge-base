"""假 FFmpeg（可控总时长与采样率）：按 KBI_FAKE_FFMPEG_SECONDS 生成等长 WAV 段。

用于验证时长硬限与段数（须标明是 mock，不代表真实解码行为）：
- 读 `-i <源>` 判断是否需要消费 stdin（pipe:0 时必须读完，避免上游 BrokenPipe）；
- 段长取 `-segment_time`；最后一段是余数；总时长按秒计算；
- KBI_FAKE_FFMPEG_RATE 控制采样率（默认 16000）。构造 10 小时边界时可用
  极小采样率让磁盘占用与真实 16k PCM 无关——本文件只验证时长/切段记账逻辑。
"""
from __future__ import annotations

import math
import os
import struct
import sys
import wave

DEFAULT_RATE = 16000


def write_wav(path: str, seconds: float, rate: int) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(int(seconds * rate)):
            v = int(8000 * math.sin(2 * math.pi * 220 * i / rate)) if rate else 0
            frames += struct.pack("<h", v)
        w.writeframes(bytes(frames))


def _segment_time(argv: list[str]) -> int:
    if "-segment_time" in argv:
        return int(argv[argv.index("-segment_time") + 1])
    return 20


def main() -> None:
    argv = sys.argv[1:]
    if "-i" in argv and argv[argv.index("-i") + 1] == "pipe:0":
        sys.stdin.buffer.read()  # 消费管道输入至 EOF
    total = float(os.environ.get("KBI_FAKE_FFMPEG_SECONDS", "40"))
    rate = int(os.environ.get("KBI_FAKE_FFMPEG_RATE", str(DEFAULT_RATE)))
    seg = _segment_time(argv)
    out_tpl = argv[-1]
    written = 0.0
    index = 0
    while written < total - 1e-6:
        length = min(float(seg), total - written)
        write_wav(out_tpl % index, length, rate)
        written += length
        index += 1


if __name__ == "__main__":
    main()
