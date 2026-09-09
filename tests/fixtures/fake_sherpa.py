"""假识别引擎：读取 WAV 时长，输出 sherpa-onnx-offline 风格的 JSON 行结果。

tokens 按 0.4s 均匀分布覆盖整段输入；用于验证合并的时间换算与 core 区间裁剪。
环境变量：
- KBI_FAKE_SHERPA_STATE：状态文件路径（记录调用次数，JSON）。
- KBI_FAKE_SHERPA_FAIL_AT：设为 "2" 时第 2 次调用以退出码 3 失败（测试退避恢复）。
"""
from __future__ import annotations

import json
import os
import sys
import wave


def _call_count(state_file: str) -> int:
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            return int(json.load(f).get("calls", 0))
    except (OSError, ValueError, json.JSONDecodeError):
        return 0


def main() -> None:
    state_file = os.environ.get("KBI_FAKE_SHERPA_STATE")
    if state_file:
        calls = _call_count(state_file) + 1
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump({"calls": calls}, f)
        fail_at = os.environ.get("KBI_FAKE_SHERPA_FAIL_AT")
        if fail_at and calls == int(fail_at):
            sys.exit(3)

    wav_path = sys.argv[-1]
    with wave.open(wav_path, "rb") as w:
        duration = w.getnframes() / w.getframerate()
    tokens = list("机器转写测试片段内容")
    n = len(tokens)
    timestamps = [round(duration * i / n, 2) for i in range(n)]
    print(json.dumps({
        "lang": "zh",
        "text": "".join(tokens),
        "tokens": tokens,
        "timestamps": timestamps,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
