"""B 站 ASR 探测工具（docs/11 §8 第一步、§9.4 第 2 条）。

复用生产读取与转写代码路径，对单条样本做端到端探测：
  B 站链接 → 播放接口音频流 → 受限 HTTP→FFmpeg 管道 → 短 WAV 段
  → sherpa-onnx-offline 逐段识别 → 合并 transcript + 脱敏指标 JSON

用法示例（在目标服务器上）：
  python scripts/probe_bilibili_asr.py \
      --url "https://www.bilibili.com/video/BV..." [--part 2] \
      [--model dolphin|sense_voice] [--out /tmp/asr-probe]

  python scripts/probe_bilibili_asr.py --wav sample.wav --model dolphin

- 不从命令行接收 Cookie：如需登录态，设置环境变量 KB_PROBE_SESSDATA。
- 输出 report.json：字节数、耗时、RTF、子进程峰值内存（Linux getrusage）、
  段级耗时与文本预览；临时签名 URL 与凭据不写入任何输出。
- 资源限额由外部施加（如 systemd-run --scope -p MemoryMax=640M 或容器 cgroup）；
  本工具只负责如实报告实测值，不模拟容器限额。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import wave
from pathlib import Path

# 允许从仓库根直接运行：把 apps/server 加入 sys.path
SERVER_DIR = Path(__file__).resolve().parents[1] / "apps" / "server"
sys.path.insert(0, str(SERVER_DIR))

from kbserver.config import get_settings  # noqa: E402
from kbserver.extractors import bilibili as bili  # noqa: E402
from kbserver.extractors import bilibili_audio as baudio  # noqa: E402
from kbserver.workers import asr as asr_mod  # noqa: E402


def _wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / (w.getframerate() or 1)


def _child_peak_mib() -> float | None:
    """累计子进程峰值 RSS（Linux/macOS）；Windows 不可用返回 None。"""
    try:
        import resource

        return resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024.0
    except (ImportError, AttributeError):
        return None


def probe_wav_dir(wav_paths: list[Path], model_alias: str, out_dir: Path,
                  engine_bin: str | None, chunk_results: list) -> dict:
    """对已准备的 WAV 段逐段识别；只跑引擎，不做业务编排。"""
    settings = get_settings()
    engine_bin = engine_bin or settings.asr_engine_bin
    model_d = asr_mod.model_dir(model_alias)
    results = []
    for i, wav in enumerate(wav_paths):
        cmd = asr_mod._engine_command(settings, model_alias, model_d, wav)
        cmd[0] = engine_bin if engine_bin else cmd[0]
        t0 = time.monotonic()
        proc = subprocess.run(cmd, capture_output=True)
        elapsed = time.monotonic() - t0
        doc = asr_mod._parse_engine_output(proc.stdout) if proc.returncode == 0 else None
        duration = _wav_duration(wav)
        results.append({
            "chunk": i, "wav": wav.name, "duration_s": round(duration, 2),
            "engine_seconds": round(elapsed, 2),
            "rtf": round(elapsed / duration, 3) if duration else None,
            "returncode": proc.returncode,
            "text_preview": (doc or {}).get("text", "")[:120],
        })
        chunk_results.append(doc or {})
        if proc.returncode != 0:
            results[-1]["error"] = "engine failed（stderr 已省略）"
            break
    return {"chunks": results}


def main() -> int:
    parser = argparse.ArgumentParser(description="B 站 ASR 单条探测（复用生产代码路径）")
    parser.add_argument("--url", help="B 站视频链接（含 b23.tv 短链）")
    parser.add_argument("--part", type=int, default=None, help="分 P 编号")
    parser.add_argument("--wav", type=Path, help="本地 WAV 文件（跳过音频获取）")
    parser.add_argument("--model", default=None, choices=sorted(asr_mod.ASR_MODELS),
                        help="模型别名；默认取 ASR_MODEL 配置")
    parser.add_argument("--out", type=Path, default=Path("./asr-probe-out"), help="输出目录")
    parser.add_argument("--engine-bin", default=None, help="sherpa-onnx-offline 路径覆盖")
    parser.add_argument("--keep-pcm", action="store_true", help="保留解码的 WAV 段（默认结束后清理）")
    args = parser.parse_args()

    settings = get_settings()
    model_alias = args.model or settings.asr_model
    if model_alias not in asr_mod.ASR_MODELS:
        print(f"未知模型别名：{model_alias}", file=sys.stderr)
        return 2
    if not asr_mod.model_available(model_alias):
        print(f"模型未部署：{asr_mod.ASR_MODELS[model_alias]}（期望目录 {asr_mod.model_dir(model_alias)}）",
              file=sys.stderr)
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    report: dict = {
        "model_alias": model_alias,
        "model_id": asr_mod.ASR_MODELS[model_alias],
        "engine_bin": args.engine_bin or settings.asr_engine_bin,
        "chunk_seconds": settings.asr_chunk_seconds,
    }

    t_start = time.monotonic()
    if args.wav:
        wav_paths = [args.wav.resolve()]
        report["source"] = {"kind": "local_wav", "path": args.wav.name}
    else:
        if not args.url:
            parser.error("需要 --url 或 --wav")
        sessdata = None  # 仅从环境变量读取，不落命令行/输出
        import os

        sessdata = os.environ.get("KB_PROBE_SESSDATA") or None
        ref = bili.resolve_share_url(args.url)
        if args.part:
            ref.explicit_part = args.part
        page = bili.resolve_video_part(ref, settings.subtitle_download_limit)
        report["source"] = {"kind": "bilibili", "bvid": ref.bvid, "aid": ref.aid,
                            "part": page["page"], "cid": page["cid"],
                            "title": page.get("title"), "duration_s": page.get("page_duration_s")}
        print(f"目标：{report['source'].get('title')} P{page['page']} cid={page['cid']}")
        stream = baudio.resolve_audio_stream(ref, page, sessdata=sessdata,
                                             max_bytes=settings.subtitle_download_limit)
        report["audio_stream"] = {"stream_id": stream.stream_id, "codec": stream.codec,
                                  "bandwidth": stream.bandwidth,
                                  "duration_s": stream.duration_s}
        attempt_dir = args.out / f"attempt-{int(time.time() * 1000)}"
        print("读取音频流并解码（受限流 → FFmpeg 管道）…")
        t0 = time.monotonic()
        prepared = baudio.prepare_audio(
            stream, attempt_dir,
            ffmpeg_bin=settings.asr_ffmpeg_bin,
            chunk_seconds=settings.asr_chunk_seconds,
            max_bytes=settings.asr_max_input_bytes,
            max_duration_s=settings.asr_max_duration_seconds,
        )
        report["prepare"] = {
            "seconds": round(time.monotonic() - t0, 1),
            "source_bytes": prepared.source_bytes,
            "total_duration_s": round(prepared.total_duration, 1),
            "chunk_count": len(prepared.chunks),
        }
        print(f"准备完成：{len(prepared.chunks)} 段 / {prepared.total_duration:.0f}s "
              f"/ {prepared.source_bytes / 1e6:.1f}MB / {report['prepare']['seconds']}s")
        chunks_base = attempt_dir / "chunks"
        wav_paths = [chunks_base / Path(c.path).name for c in prepared.chunks]

    chunk_results: list = []
    asr_stats = probe_wav_dir(wav_paths, model_alias, args.out, args.engine_bin, chunk_results)
    report["asr"] = asr_stats
    ok = [c for c in asr_stats["chunks"] if c.get("returncode") == 0]
    if ok:
        total_audio = sum(c["duration_s"] for c in ok)
        total_engine = sum(c["engine_seconds"] for c in ok)
        report["asr_summary"] = {
            "ok_chunks": len(ok), "failed_chunks": len(asr_stats["chunks"]) - len(ok),
            "audio_seconds": round(total_audio, 1), "engine_seconds": round(total_engine, 1),
            "effective_rtf": round(total_engine / total_audio, 3) if total_audio else None,
            "child_peak_mib": round(_child_peak_mib(), 1) if _child_peak_mib() else None,
        }
    report["total_seconds"] = round(time.monotonic() - t_start, 1)

    # 完整转写文本（report.json 只存 120 字预览；全文单独落盘供人工对照，
    # 段偏移按各段音频时长累加，即段在原视频中的起始秒）。
    if chunk_results:
        lines, offset = [], 0.0
        for res, doc in zip(asr_stats["chunks"], chunk_results):
            mm, ss = divmod(int(offset), 60)
            lines.append(f"[{mm:02d}:{ss:02d}] {(doc or {}).get('text', '')}")
            offset += res.get("duration_s") or 0.0
        transcript_path = args.out / "transcript.txt"
        transcript_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"转写全文已写入 {transcript_path}")

    report["note"] = ("峰值内存为 getrusage(RUSAGE_CHILDREN) 累计值；"
                      "容器内限额验收需结合 cgroup.peak，不以此值单独宣布满足 1GB 合计限制。")

    out_json = args.out / "report.json"
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report.get("asr_summary") or report["asr"], ensure_ascii=False, indent=2))
    print(f"报告已写入 {out_json}")

    if not args.keep_pcm and not args.wav:
        for c in args.out.glob("attempt-*"):
            shutil.rmtree(c, ignore_errors=True)  # 只清理本工具创建的 attempt 目录
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
