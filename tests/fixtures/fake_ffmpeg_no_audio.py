"""假 FFmpeg：只有画面、没有音轨的视频。

真实 FFmpeg 在 `-map 0:a:0` 找不到音频流时打印 "Stream map '0:a:0' matches no
streams." 并以非零码退出。本桩复现这一句与退出码，用来验证界面把它说成「这个文件
里没有声音轨」，而不是当成可重试的解码失败（docs/13 §6.2）。
"""
from __future__ import annotations

import sys

sys.stderr.write("ffmpeg version fake\nStream map '0:a:0' matches no streams.\n")
sys.stderr.flush()
sys.exit(1)
