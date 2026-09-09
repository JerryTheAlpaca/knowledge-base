"""假 FFmpeg：消费 stdin 管道后写出两段固定 20s 的 16kHz/单声道/16-bit WAV。

只用于集成测试验证 prepare→checkpoint→transcribe 的业务编排，
不代表真实解码行为（docs/11 §9.4：mock 结果不作为真实 ASR 质量证据）。
用法：python fake_ffmpeg.py <ffmpeg 参数...>（最后一个参数是输出模板 chunk-%04d.wav）
"""
from __future__ import annotations

import math
import struct
import sys
import wave

RATE = 16000


def write_wav(path: str, seconds: float) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        frames = bytearray()
        for i in range(int(seconds * RATE)):
            v = int(8000 * math.sin(2 * math.pi * 220 * i / RATE))
            frames += struct.pack("<h", v)
        w.writeframes(bytes(frames))


def main() -> None:
    # 消费全部 stdin（模拟管道输入 EOF），再按模板写两段各 20 秒
    sys.stdin.buffer.read()
    out_tpl = sys.argv[-1]
    write_wav(out_tpl % 0, 20.0)
    write_wav(out_tpl % 1, 20.0)


if __name__ == "__main__":
    main()
