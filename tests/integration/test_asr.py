"""本地 ASR 集成测试（docs/11 §8 第二步、§9.4）。

覆盖：设置开关、触发幂等与用户隔离、no_track 自动入队（部署/用户开关）、
音频准备（假 FFmpeg + 假流：清单提交、摘要校验）、逐段转写检查点与恢复、
忙碌让出不消耗失败额度、普通任务不被 ASR 饥饿、取消、模型切换独立 recipe、
发布机器转写标记与 asr 元数据、补充正文后旧 ASR 作废。

网络与解码用假流/假 FFmpeg/假引擎；不把 mock 结果当真实 ASR 质量证据。
B 站接口数据全部为脱敏合成内容。
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import auth

from kbserver.config import get_settings
from kbserver.extractors import bilibili as bili
from kbserver.extractors import bilibili_audio as baudio
from kbserver.models import User as KbUser
from kbserver.security.safe_fetch import StreamResult
from kbserver.workers import asr as asr_mod
from kbserver.workers import worker

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
BV = "BV1xxASRTest"


@pytest.fixture(autouse=True)
def _reset_bili_throttle():
    """重置 Worker 的 B 站任务间节流状态（审查 C-13 引入）：本文件不测节流，
    避免前序测试留下的时间戳让 extract 在 _drain 里让出后滞留队列。"""
    worker._last_bili_task_at = None
    yield
    worker._last_bili_task_at = None


# ---- 门与包装器 ----

class AlwaysAllowGate:
    """测试用：跳过空闲准入（本机无 /proc 指标）。"""

    def can_start(self, settings, *, normal_busy=False):
        return True, ""

    def can_start_io(self, settings, *, normal_busy=False):
        return True, ""

    def check_running(self, settings):
        return True

    def check_running_io(self, settings):
        return True

    def note_busy(self):
        pass


class BusyGate(AlwaysAllowGate):
    """运行中永远报告忙碌：触发让出。"""

    def __init__(self):
        self.notified = False

    def check_running(self, settings):
        return False

    def note_busy(self):
        self.notified = True


def _make_wrapper(directory: Path, name: str, target: Path) -> Path:
    """跨平台命令包装器：把单字符串可执行位变成 `python target 参数...`。

    Windows 下 .cmd 由 cmd.exe 按 OEM 代码页解析：非 ASCII 路径（如中文工作区）
    必须按 mbcs 写，否则会读到乱码路径导致「找不到文件」。
    """
    if os.name == "nt":
        path = directory / f"{name}.cmd"
        path.write_text(f'@"{sys.executable}" "{target}" %*\r\n', encoding="mbcs")
    else:
        path = directory / name
        path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{target}" "$@"\n', encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


# ---- 假网络与假流 ----

class FakeStreamToSink:
    """替换 stream_to_sink：不触网，把固定字节写进 sink（模拟受限流读取）。"""

    def __init__(self, total_bytes: int = 1_000_000, fail_urls: set[str] | None = None):
        self.total_bytes = total_bytes
        self.fail_urls = fail_urls or set()
        self.calls: list[str] = []

    def __call__(self, url, *, sink, max_bytes, timeout=20.0, headers=None,
                 should_cancel=None, on_progress=None):
        self.calls.append(url)
        if url in self.fail_urls:
            from kbserver.security.safe_fetch import SafeFetchError

            raise SafeFetchError("NETWORK_ERROR", f"流读取失败：{url}")
        chunk = b"x" * 65536
        sent = 0
        while sent < self.total_bytes:
            sink(chunk)
            sent += len(chunk)
            if on_progress:
                on_progress(sent)
        return StreamResult(url=url, status_code=200, bytes_read=sent)


@pytest.fixture()
def fresh_queue(session_factory):
    """清空共享队列中的历史排队任务：单步 run_once 的领取顺序才可预测。"""
    with session_factory() as db:
        for j in db.query(worker.Job).filter(worker.Job.state.in_(("queued", "retry_wait"))).all():
            j.state = "cancelled"
        db.commit()
    return session_factory


@pytest.fixture()
def asr_env(monkeypatch):
    """打开部署开关 + 假模型可用 + 假 FFmpeg/引擎包装器（经环境变量注入）。"""
    monkeypatch.setenv("ASR_ENABLED", "true")
    monkeypatch.setattr(asr_mod, "model_available", lambda alias: True)
    wrapper_dir = Path(tempfile.mkdtemp(prefix="asr-wrap-"))
    monkeypatch.setenv(
        "ASR_FFMPEG_BIN", str(_make_wrapper(wrapper_dir, "fake-ffmpeg", FIXTURES / "fake_ffmpeg.py")))
    monkeypatch.setenv(
        "ASR_ENGINE_BIN", str(_make_wrapper(wrapper_dir, "fake-sherpa", FIXTURES / "fake_sherpa.py")))
    state_file = wrapper_dir / "sherpa-state.json"
    monkeypatch.setenv("KBI_FAKE_SHERPA_STATE", str(state_file))
    monkeypatch.delenv("KBI_FAKE_SHERPA_FAIL_AT", raising=False)

    def install(stream: baudio.AudioStream | None = None,
                streamer: FakeStreamToSink | None = None):
        stream = stream or _fake_audio_stream()
        monkeypatch.setattr(baudio, "resolve_audio_stream", lambda ref, page, **kw: stream)
        sink = streamer or FakeStreamToSink()
        # 受限流读取现在位于通用音频层 audio/prepare；两处都替换，覆盖新旧调用点
        from kbserver.audio import prepare as audio_prepare_mod

        monkeypatch.setattr(audio_prepare_mod, "stream_to_sink", sink)
        monkeypatch.setattr(baudio, "stream_to_sink", sink)
        # resolve_video_part 触网（view API）：桩为固定 page 元数据
        monkeypatch.setattr(bili, "resolve_video_part", lambda ref, limit: {
            "canonical_url": f"https://www.bilibili.com/video/{BV}/",
            "title": "无字幕测试视频", "author": "测试UP主", "published_at": None,
            "duration_s": 40.0, "pages_count": 1, "page": 1, "cid": "999",
            "page_duration_s": 40.0, "part_note": None, "view_subtitle_tracks": [],
        })
        # extract 阶段一律快速走 no_track（用户开关默认关 → needs_input），
        # 避免未 stub 的提取器真实触网；ASR 编排由手动触发或 opt-in 驱动
        def _no_track_extract(url, *, share_text=None, sessdata=None):
            raise bili.BilibiliError("no_track", "B 站对这个视频没有提供字幕轨。")
        monkeypatch.setattr(bili, "extract", _no_track_extract)
        return stream

    return SimpleNamespace(install=install, state_file=state_file)


def _fake_audio_stream(duration_s: float = 40.0) -> baudio.AudioStream:
    return baudio.AudioStream(
        stream_id=30216, codec="mp4a.40.2", bandwidth=64000,
        duration_s=duration_s, base_url="https://upos.example.com/audio.m4s",
        backup_urls=["https://upos.example.com/audio-backup.m4s"],
    )


# ---- 测试辅助 ----

def _capture_bili_url(client, token, key: str, url: str | None = None):
    return client.post(
        "/v1/captures",
        json={"client_capture_id": f"{key}-1111-2222-3333-444444444444",
              "input_kind": "url", "original_url": url or f"https://www.bilibili.com/video/{BV}/"},
        headers={**auth(token), "Idempotency-Key": key},
    )


def _capture_text(client, token, key: str, text: str):
    return client.post(
        "/v1/captures",
        json={"client_capture_id": f"{key}-1111-2222-3333-444444444444",
              "input_kind": "text", "text": text},
        headers={**auth(token), "Idempotency-Key": key},
    )


def _drain(session_factory, gate=None, max_rounds=60):
    for _ in range(max_rounds):
        if not worker.run_once(session_factory, gate):
            break


def _session_factory():
    from kbserver.db import get_session_factory

    return get_session_factory()


def _get_run(db, item_id):
    return db.query(asr_mod.AsrRun).filter(asr_mod.AsrRun.item_id == item_id).one_or_none()


def _item_job(db, item_id, stage):
    return db.query(worker.Job).filter(
        worker.Job.item_id == item_id, worker.Job.stage == stage).one()


def _stub_no_track(monkeypatch):
    def _extract(url, *, share_text=None, sessdata=None):
        raise bili.BilibiliError("no_track", "B 站对这个视频没有提供字幕轨。")

    monkeypatch.setattr(bili, "extract", _extract)


# ---- 设置与触发 ----

def test_asr_settings_roundtrip(client, user_a):
    r = client.get("/v1/asr-settings", headers=auth(user_a["desktop"]["token"]))
    assert r.status_code == 200
    assert r.json()["auto_when_no_track"] is False
    assert r.json()["deployment_enabled"] is False  # 测试环境默认关
    r = client.put("/v1/asr-settings", json={"auto_when_no_track": True},
                   headers=auth(user_a["desktop"]["token"]))
    assert r.status_code == 200 and r.json()["auto_when_no_track"] is True
    r = client.get("/v1/asr-settings", headers=auth(user_a["desktop"]["token"]))
    assert r.json()["auto_when_no_track"] is True


def test_trigger_requires_bilibili_and_isolation(client, user_a, user_b, asr_env):
    asr_env.install()
    r = _capture_text(client, user_a["phone"]["token"], "asrplain", "普通文字条目")
    item_id = r.json()["item_id"]
    r = client.post(f"/v1/items/{item_id}/asr", json={},
                    headers=auth(user_a["desktop"]["token"]))
    assert r.status_code == 422  # 非 B 站条目
    r = client.post(f"/v1/items/{item_id}/asr", json={},
                    headers=auth(user_b["desktop"]["token"]))
    assert r.status_code == 404  # 他人条目
    r = client.post("/v1/items/does-not-exist/asr", json={},
                    headers=auth(user_a["desktop"]["token"]))
    assert r.status_code == 404


def test_trigger_idempotent_same_recipe(client, user_a, asr_env):
    asr_env.install()
    r = _capture_bili_url(client, user_a["phone"]["token"], "asridem")
    item_id = r.json()["item_id"]
    r1 = client.post(f"/v1/items/{item_id}/asr", json={},
                     headers=auth(user_a["desktop"]["token"]))
    assert r1.status_code == 200
    run_id = r1.json()["run_id"]
    assert r1.json()["created"] is True
    r2 = client.post(f"/v1/items/{item_id}/asr", json={},
                     headers=auth(user_a["desktop"]["token"]))
    assert r2.status_code == 200 and r2.json()["run_id"] == run_id
    assert r2.json()["created"] is False
    with _session_factory()() as db:
        assert db.query(asr_mod.AsrRun).filter(asr_mod.AsrRun.item_id == item_id).count() == 1
        assert db.query(worker.Job).filter(
            worker.Job.item_id == item_id, worker.Job.stage == "asr_prepare").count() == 1


def test_model_switch_creates_new_recipe(client, user_a, asr_env):
    asr_env.install()
    r = _capture_bili_url(client, user_a["phone"]["token"], "asrmodel")
    item_id = r.json()["item_id"]
    r1 = client.post(f"/v1/items/{item_id}/asr", json={},
                     headers=auth(user_a["desktop"]["token"]))
    run1 = r1.json()["run_id"]
    r2 = client.post(f"/v1/items/{item_id}/asr", json={"retry_model": "sense_voice"},
                     headers=auth(user_a["desktop"]["token"]))
    assert r2.status_code == 200
    run2 = r2.json()["run_id"]
    assert run1 != run2  # 新 recipe 新 run：两种模型输出不混作同一次识别
    with _session_factory()() as db:
        runs = db.query(asr_mod.AsrRun).filter(asr_mod.AsrRun.item_id == item_id).all()
        assert len(runs) == 2
        assert len({r.recipe_hash for r in runs}) == 2


# ---- extract no_track 自动入队 ----

def test_no_track_user_opt_out_needs_input(client, user_a, monkeypatch, asr_env):
    asr_env.install()
    _stub_no_track(monkeypatch)
    r = _capture_bili_url(client, user_a["phone"]["token"], "asrntrackoff")
    item_id = r.json()["item_id"]
    _drain(_session_factory())
    with _session_factory()() as db:
        assert _get_run(db, item_id) is None  # 用户开关没开 → 不自动入队
        it = db.get(worker.Item, item_id)
        assert it.pipeline_state == "needs_input"


def test_no_track_auto_enqueues_when_user_opted_in(client, user_a, monkeypatch, asr_env):
    asr_env.install()
    _stub_no_track(monkeypatch)
    client.put("/v1/asr-settings", json={"auto_when_no_track": True},
               headers=auth(user_a["desktop"]["token"]))
    r = _capture_bili_url(client, user_a["phone"]["token"], "asrntrackon")
    item_id = r.json()["item_id"]
    _drain(_session_factory(), gate=AlwaysAllowGate())
    with _session_factory()() as db:
        run = _get_run(db, item_id)
        assert run is not None and run.requested_by == "auto"
        # gate=None 时 ASR 不被领取；allow gate 下 prepare 已领取执行
        job = _item_job(db, item_id, "asr_prepare")
        assert job.state in ("queued", "running", "succeeded")
        it = db.get(worker.Item, item_id)
        assert it.pipeline_state != "needs_input"


# ---- prepare / transcribe 全链路 ----

def test_prepare_and_transcribe_end_to_end(client, user_a, asr_env, fresh_queue):
    asr_env.install()
    r = _capture_bili_url(client, user_a["phone"]["token"], "asre2e")
    item_id = r.json()["item_id"]
    client.post(f"/v1/items/{item_id}/asr", json={},
                headers=auth(user_a["desktop"]["token"]))
    _drain(_session_factory(), gate=AlwaysAllowGate())

    with _session_factory()() as db:
        run = _get_run(db, item_id)
        assert run.state == "succeeded"
        assert run.chunk_count == 2 and run.next_chunk_index == 2
        it = db.get(worker.Item, item_id)
        assert it.pipeline_state == "waiting_key"  # 发布后 enrich 无凭据等待
        enrich_job = _item_job(db, item_id, "enrich")
        assert enrich_job is not None

    r = client.get(f"/v1/items/{item_id}", headers=auth(user_a["desktop"]["token"]))
    doc = r.json()
    m = client.get(
        f"/v1/items/{item_id}/bundles/{doc['bundle_revision']}/manifest",
        headers=auth(user_a["desktop"]["token"])).json()
    assert any("机器转写，未经人工校对" in w for w in m["warnings"])
    paths = [f["relative_path"] for f in m["files"]]
    assert "asr/asr_raw.json" in paths and "asr/asr_manifest.json" in paths
    assert "transcript.srt" in paths and "normalized.md" in paths
    assert doc["coverage"] == "full_text"
    with _session_factory()() as db:
        run = _get_run(db, item_id)
        # 发布成功后 PCM 工作目录清理
        assert not (get_settings().tmp_dir / run.work_dir).exists()


def test_asr_finish_stores_platform_subtitle_ref(client, user_a, asr_env, fresh_queue,
                                                 monkeypatch):
    """收尾时抓到平台字幕 → Bundle 存 asr/subtitle_ref.json（听写校对参考）。"""
    asr_env.install()
    # 覆盖 install 的 no_track 桩：该视频实际存在平台字幕
    monkeypatch.setattr(bili, "extract", lambda url, sessdata=None: SimpleNamespace(
        segments=[
            {"segment_id": "x1", "start_ms": 1000, "end_ms": 8000,
             "text": "探讨是否应该取消英语的主科地位"},
            {"segment_id": "x2", "start_ms": 8000, "end_ms": 15000,
             "text": "如果中高考不考英语了学生会轻松吗"},
        ]))
    r = _capture_bili_url(client, user_a["phone"]["token"], "asrsubref")
    item_id = r.json()["item_id"]
    client.post(f"/v1/items/{item_id}/asr", json={},
                headers=auth(user_a["desktop"]["token"]))
    _drain(_session_factory(), gate=AlwaysAllowGate())

    with _session_factory()() as db:
        run = _get_run(db, item_id)
        assert run.state == "succeeded"
    token = user_a["desktop"]["token"]
    it = client.get(f"/v1/items/{item_id}", headers=auth(token)).json()
    m = client.get(f"/v1/items/{item_id}/bundles/{it['bundle_revision']}/manifest",
                   headers=auth(token)).json()
    paths = {f["relative_path"]: f["file_id"] for f in m["files"]}
    assert "asr/subtitle_ref.json" in paths
    ref = json.loads(client.get(
        f"/v1/items/{item_id}/bundles/{it['bundle_revision']}/files/{paths['asr/subtitle_ref.json']}",
        headers=auth(token)).content)
    assert ref["schema"] == "asr-subtitle-ref-v1"
    assert [r["text"] for r in ref["records"]] == [
        "探讨是否应该取消英语的主科地位", "如果中高考不考英语了学生会轻松吗"]


def test_prepare_chunk_tamper_fails_final(client, user_a, monkeypatch, asr_env, fresh_queue):
    """清单提交后被外部篡改 → 段摘要校验失败，终态失败不静默续跑。"""
    asr_env.install()
    r = _capture_bili_url(client, user_a["phone"]["token"], "asrsha")
    item_id = r.json()["item_id"]
    client.post(f"/v1/items/{item_id}/asr", json={},
                headers=auth(user_a["desktop"]["token"]))
    sf = _session_factory()
    worker.run_once(sf, AlwaysAllowGate())  # extract（no_track → needs_input）
    worker.run_once(sf, AlwaysAllowGate())  # prepare
    with sf() as s:
        run = _get_run(s, item_id)
        assert run.state == "transcribing"
        manifest = json.loads((get_settings().tmp_dir / run.work_dir / "manifest.json").read_bytes())
        chunk_path = (get_settings().tmp_dir / run.work_dir / manifest["chunks_dir"]
                      / "chunk-0000.wav")
        data = bytearray(chunk_path.read_bytes())
        data[-1] ^= 0xFF
        chunk_path.write_bytes(bytes(data))
    worker.run_once(sf, AlwaysAllowGate())  # transcribe：摘要不匹配 → failed
    with sf() as s:
        run = _get_run(s, item_id)
        assert run.state == "failed"
        assert _item_job(s, item_id, "asr_transcribe").state == "failed"
        work_dir = get_settings().tmp_dir / run.work_dir
    assert not work_dir.exists()  # 终态之后这批 PCM 再也用不上，留着就是泄漏


def test_terminal_prepare_failure_leaves_no_pcm(client, user_a, asr_env, fresh_queue):
    """解码跑完才发现时长对不上：此刻盘上已经是一整套 PCM。

    16k 单声道约 115MB/小时、单条上限 10 小时，终态失败不回收就会随每次失败
    累积到写满盘（retention sweep 只管对象存储，不管 tmp 工作目录）。
    """
    asr_env.install(stream=_fake_audio_stream(duration_s=3600.0))  # 声明 1 小时，假 FFmpeg 只出 40s
    item_id = _capture_bili_url(client, user_a["phone"]["token"], "asrdiscard").json()["item_id"]
    client.post(f"/v1/items/{item_id}/asr", json={},
                headers=auth(user_a["desktop"]["token"]))
    sf = _session_factory()
    worker.run_once(sf, AlwaysAllowGate())  # extract（no_track → needs_input）
    with sf() as s:
        work_dir = get_settings().tmp_dir / _get_run(s, item_id).work_dir
    worker.run_once(sf, AlwaysAllowGate())  # prepare → duration_mismatch 终态
    with sf() as s:
        run = _get_run(s, item_id)
        assert run.state == "failed"
        assert run.last_error and "时长" in run.last_error
    assert not work_dir.exists()


def test_busy_yield_preserves_attempt_and_resumes(client, user_a, monkeypatch, asr_env, fresh_queue):
    """运行中变忙：终止当前段 → paused(resource_busy)，attempt 不消耗，恢复后继续。"""
    monkeypatch.setenv("ASR_BUSY_COOLDOWN_SECONDS", "0")  # 恢复阶段不等待冷却
    monkeypatch.setattr(asr_mod, "LEASE_REFRESH_S", 0.2)  # 让 wait 循环尽快检查
    asr_env.install()
    # 用慢命令替换引擎：执行 3 秒，保证让出检查发生在子进程运行中
    original_engine_command = asr_mod._engine_command
    monkeypatch.setattr(asr_mod, "_engine_command",
                        lambda settings, alias, model_d, wav: [
                            sys.executable, "-c", "import time; time.sleep(3)", str(wav)])
    r = _capture_bili_url(client, user_a["phone"]["token"], "asryield")
    item_id = r.json()["item_id"]
    client.post(f"/v1/items/{item_id}/asr", json={},
                headers=auth(user_a["desktop"]["token"]))
    sf = _session_factory()
    worker.run_once(sf, AlwaysAllowGate())  # extract（no_track → needs_input）
    worker.run_once(sf, AlwaysAllowGate())  # prepare
    with sf() as s:
        run = _get_run(s, item_id)
        assert run.state == "transcribing"

    gate = BusyGate()
    worker.run_once(sf, gate)  # transcribe 第 1 段：执行中让出
    with sf() as s:
        run = _get_run(s, item_id)
        assert run.state == "paused" and run.pause_reason == "resource_busy"
        assert run.next_chunk_index == 0  # 未提交的段不计入进度
        job = _item_job(s, item_id, "asr_transcribe")
        assert job.state == "queued" and job.attempt == 0  # 不消耗失败额度
    assert gate.notified
    # 恢复（快引擎）：从第 0 段继续
    monkeypatch.setattr(asr_mod, "_engine_command", original_engine_command)
    worker.run_once(sf, AlwaysAllowGate())
    worker.run_once(sf, AlwaysAllowGate())
    with sf() as s:
        run = _get_run(s, item_id)
        assert run.state == "succeeded"


def test_transcribe_failure_backoff_then_succeed(client, user_a, monkeypatch, asr_env, fresh_queue):
    """段失败走 retry_wait，恢复后只重算失败段，不重复已提交段。"""
    monkeypatch.setattr(asr_mod, "FAIL_BACKOFF_BASE_S", 0)  # 测试不等待退避
    monkeypatch.setenv("KBI_FAKE_SHERPA_FAIL_AT", "2")  # 第 2 次引擎调用失败
    asr_env.install()
    r = _capture_bili_url(client, user_a["phone"]["token"], "asrfail")
    item_id = r.json()["item_id"]
    client.post(f"/v1/items/{item_id}/asr", json={},
                headers=auth(user_a["desktop"]["token"]))
    sf = _session_factory()
    worker.run_once(sf, AlwaysAllowGate())  # extract（no_track → needs_input）
    worker.run_once(sf, AlwaysAllowGate())  # prepare
    worker.run_once(sf, AlwaysAllowGate())  # transcribe 段 0 成功
    with sf() as s:
        run = _get_run(s, item_id)
        assert run.next_chunk_index == 1
    worker.run_once(sf, AlwaysAllowGate())  # transcribe 段 1：失败 → retry_wait
    with sf() as s:
        run = _get_run(s, item_id)
        assert run.failed_count == 1 and run.next_chunk_index == 1
        assert _item_job(s, item_id, "asr_transcribe").state == "retry_wait"
    worker.run_once(sf, AlwaysAllowGate())  # 重试段 1 成功 → 发布
    with sf() as s:
        run = _get_run(s, item_id)
        assert run.state == "succeeded"
    with open(asr_env.state_file, encoding="utf-8") as f:
        assert json.load(f)["calls"] == 3  # 段0 + 段1失败 + 段1重试；段0 不重复


def test_claim_filters_by_stage(client, user_a, fresh_queue):
    """claim 按 stage 过滤：ASR job 更早到期也不被普通领取抢走，反之亦然。"""
    sf = _session_factory()
    with sf() as db:
        user = db.query(KbUser).first()
        capture = worker.Capture(id="asr-claim-test", user_id=user.id,
                                 client_capture_id="asr-claim-test",
                                 request_hash="x", input_json={})
        db.add(capture)
        db.flush()
        item = worker.Item(user_id=user.id, capture_id="asr-claim-test",
                           source_revision=1, pipeline_state="queued")
        db.add(item)
        db.flush()
        asr_job = worker.Job(user_id=user.id, item_id=item.id, source_revision=1,
                             stage="asr_prepare", recipe_hash="r1", state="queued")
        normal_job = worker.Job(user_id=user.id, item_id=item.id, source_revision=1,
                                stage="extract", recipe_hash="r1", state="queued")
        db.add(asr_job)
        db.add(normal_job)
        db.commit()
        # ASR job 更早到期
        earlier = worker.utcnow()
        asr_job.not_before = earlier
        normal_job.not_before = earlier
        db.commit()
        job_ids = {asr_job.id, normal_job.id}
    got_normal = worker.claim_job(sf, worker.NORMAL_STAGES)
    assert got_normal is not None
    assert got_normal.id == normal_job.id  # ASR job 更早到期，普通领取仍只拿普通 job
    got_asr = worker.claim_job(sf, worker.ASR_STAGES)
    assert got_asr is not None and got_asr.stage == "asr_prepare"


def test_normal_busy_blocks_asr_start():
    """有普通任务运行中 → gate 不允许启动 ASR（普通任务优先）。"""
    from kbserver.workers.idle import AsrGate

    gate = AsrGate(sampler=lambda: None)  # 指标采样不影响本用例
    allowed, reason = gate.can_start(get_settings(), normal_busy=True)
    assert not allowed and reason == "normal_jobs_active"


def test_cancel_stops_run(client, user_a, asr_env):
    asr_env.install()
    r = _capture_bili_url(client, user_a["phone"]["token"], "asrcancel")
    item_id = r.json()["item_id"]
    client.post(f"/v1/items/{item_id}/asr", json={},
                headers=auth(user_a["desktop"]["token"]))
    r = client.post(f"/v1/items/{item_id}/asr/cancel", json={},
                    headers=auth(user_a["desktop"]["token"]))
    assert r.status_code == 200 and r.json()["cancelled"] is True
    with _session_factory()() as s:
        run = _get_run(s, item_id)
        assert run.state == "cancelled"
        assert _item_job(s, item_id, "asr_prepare").state == "cancelled"


def test_supplement_invalidates_finished_asr(client, user_a, asr_env, fresh_queue):
    """转写完成前用户补充正文（新 revision）→ 旧 ASR 结果作废，不覆盖新材料。"""
    asr_env.install()
    r = _capture_bili_url(client, user_a["phone"]["token"], "asrsupp")
    item_id = r.json()["item_id"]
    client.post(f"/v1/items/{item_id}/asr", json={},
                headers=auth(user_a["desktop"]["token"]))
    sf = _session_factory()
    worker.run_once(sf, AlwaysAllowGate())  # prepare
    with sf() as s:
        rev = s.get(worker.Item, item_id).source_revision
    client.post(f"/v1/items/{item_id}/supplements",
                json={"expected_source_revision": rev, "text": "我自己补充的正文内容。"},
                headers=auth(user_a["desktop"]["token"]))
    _drain(sf, gate=AlwaysAllowGate())
    with sf() as s:
        run = _get_run(s, item_id)
        it = s.get(worker.Item, item_id)
        # 不发布覆盖补充内容；条目里是用户补充的正文路径
        assert run.state in ("cancelled", "failed")
        assert it.pipeline_state != "extracting"


def test_engine_command_flags():
    """dolphin 与 sense_voice 的 CLI 参数按 §2 固定；互不套用对方参数。"""
    settings = get_settings()
    model_d = asr_mod.model_dir("dolphin")
    cmd = asr_mod._engine_command(settings, "dolphin", model_d, Path("x.wav"))
    assert any("--dolphin-model=" in c for c in cmd)
    assert any("--decoding-method=greedy_search" in c for c in cmd)
    assert not any("sense-voice" in c for c in cmd)
    cmd2 = asr_mod._engine_command(settings, "sense_voice", asr_mod.model_dir("sense_voice"),
                                   Path("x.wav"))
    assert any("--sense-voice-model=" in c for c in cmd2)
    assert any("--sense-voice-use-itn=1" in c for c in cmd2)
    assert not any("dolphin" in c for c in cmd2)
    assert any("--num-threads=1" in c for c in cmd + cmd2)


# ---- 合并逻辑（_merge_results：跨段续组 / 标点优先断句 / 安全上限）----

def _chunk_result(core_start, core_end, tokens, *, input_start=None):
    """构造引擎段结果：tokens 为 (绝对秒, token) 列表，换算成引擎本地时间戳。"""
    if input_start is None:
        input_start = core_start
    return {
        "core_start": core_start, "core_end": core_end,
        "input_start": input_start,
        "text": "".join(t for _ts, t in tokens),
        "tokens": [t for _ts, t in tokens],
        "timestamps": [round(ts - input_start, 2) for ts, _t in tokens],
    }


def test_merge_results_continues_group_across_chunk_boundary():
    """连续语音跨 20s 段边界（边界处 gap < 1.2s）不再被腰斩成两条记录。"""
    results = [
        _chunk_result(0.0, 20.0, [(18.6, "所"), (18.9, "以"), (19.5, "没")],
                      input_start=0.0),
        # 第 2 段输入含 1s 上下文：abs 19.0-20.0 的上下文 token 被裁掉，不产生重复
        _chunk_result(20.0, 40.0, [(19.2, "没"), (19.9, "哈"),
                                   (20.3, "必"), (20.8, "要"), (21.3, "学"),
                                   (21.4, "。")],
                      input_start=19.0),
    ]
    records, silence = asr_mod._merge_results({}, results)
    assert silence == []
    assert records == [
        {"start_s": 18.6, "end_s": 21.4 + 0.5, "text": "所以没必要学。"},
    ]


def test_merge_results_breaks_on_pause():
    """token 间隔 > 1.2s 视为说话停顿，断成两条记录。"""
    results = [_chunk_result(0.0, 20.0,
                             [(1.0, "前半句"), (4.5, "后半句"), (4.6, "。")],
                             input_start=0.0)]
    records, _ = asr_mod._merge_results({}, results)
    assert [r["text"] for r in records] == ["前半句", "后半句。"]


def test_merge_results_prefers_sentence_punctuation():
    """连续语流按句末标点断句，不再等 token 硬上限。"""
    toks = []
    t = 10.0
    for ch in "今天天气很好。我们去爬山。":
        toks.append((round(t, 2), ch))
        t += 0.1
    records, _ = asr_mod._merge_results(
        {}, [_chunk_result(0.0, 20.0, toks, input_start=0.0)])
    assert [r["text"] for r in records] == ["今天天气很好。", "我们去爬山。"]


def test_merge_results_caps_runaway_group():
    """无停顿无标点的连续语流到达安全上限强制断，防止记录无界增长。"""
    toks = [(round(1.0 + i * 0.05, 2), "字")
            for i in range(asr_mod.GROUP_MAX_TOKENS + 30)]
    records, _ = asr_mod._merge_results(
        {}, [_chunk_result(0.0, 20.0, toks, input_start=0.0)])
    assert [len(r["text"]) for r in records] == [asr_mod.GROUP_MAX_TOKENS, 30]


def test_merge_results_drops_boundary_pseudo_period():
    """段边界伪句号：句末标点恰为本段 core 最后一 token 且下段紧随 → 丢弃并续组。"""
    results = [
        _chunk_result(0.0, 20.0, [(18.6, "所"), (18.9, "以"), (19.5, "没"), (19.9, "。")],
                      input_start=0.0),
        _chunk_result(20.0, 40.0, [(20.3, "必"), (20.8, "要"), (21.3, "学"), (21.4, "。")],
                      input_start=19.0),
    ]
    records, _ = asr_mod._merge_results({}, results)
    assert [r["text"] for r in records] == ["所以没必要学。"]


def test_merge_results_keeps_boundary_period_before_pause():
    """真实句末：边界句号后是 >1.2s 停顿 → 保留句号，正常断句。"""
    results = [
        _chunk_result(0.0, 20.0, [(18.6, "第"), (18.9, "一"), (19.5, "句"), (19.9, "。")],
                      input_start=0.0),
        _chunk_result(20.0, 40.0, [(22.0, "第"), (22.3, "二"), (22.6, "句"), (22.7, "。")],
                      input_start=19.0),
    ]
    records, _ = asr_mod._merge_results({}, results)
    assert [r["text"] for r in records] == ["第一句。", "第二句。"]


def test_merge_results_dedupes_boundary_punctuation():
    """段边界重识别的重复标点：句号后紧跟的下一段开头逗号被丢弃。"""
    results = [
        _chunk_result(0.0, 20.0, [(18.0, "资"), (18.5, "产"), (18.9, "负"),
                                  (19.4, "债"), (19.6, "表"), (19.9, "。")],
                      input_start=0.0),
        _chunk_result(20.0, 40.0, [(21.5, "，"), (21.8, "比"), (22.1, "如"),
                                   (22.4, "说"), (22.7, "复"), (22.9, "利"), (23.0, "。")],
                      input_start=19.0),
    ]
    records, _ = asr_mod._merge_results({}, results)
    assert [r["text"] for r in records] == ["资产负债表。", "比如说复利。"]


def test_merge_results_silence_and_coarse_records():
    """静音段入 silence；有文本无 token 的段用粗粒度并与 token 记录按时间排序。"""
    silent = {"core_start": 0.0, "core_end": 20.0, "input_start": 0.0,
              "text": "", "tokens": None, "timestamps": None}
    coarse = {"core_start": 20.0, "core_end": 40.0, "input_start": 19.0,
              "text": "粗粒度文本", "tokens": None, "timestamps": None}
    timed = _chunk_result(40.0, 60.0, [(41.0, "后"), (41.5, "段")], input_start=40.0)
    records, silence = asr_mod._merge_results({}, [silent, coarse, timed])
    assert silence == [[0.0, 20.0]]
    assert records == [
        {"start_s": 20.0, "end_s": 40.0, "text": "粗粒度文本"},
        {"start_s": 41.0, "end_s": 41.5 + 0.5, "text": "后段"},
    ]


# ---- remerge CLI（用存储的识别结果重算，不重新识别音频）----

def test_remerge_cli_skips_unchanged_then_republishes(client, user_a, asr_env, fresh_queue,
                                                      monkeypatch, capsys):
    """remerge：合并结果与已发布一致→跳过；有变化→新来源版本；重复执行幂等。"""
    import argparse

    from kbserver import cli

    asr_env.install()
    r = _capture_bili_url(client, user_a["phone"]["token"], "asrremerge")
    item_id = r.json()["item_id"]
    client.post(f"/v1/items/{item_id}/asr", json={},
                headers=auth(user_a["desktop"]["token"]))
    _drain(_session_factory(), gate=AlwaysAllowGate())
    with _session_factory()() as db:
        rev0 = db.get(worker.Item, item_id).source_revision

    args = argparse.Namespace(command="remerge", user=None, item=item_id,
                              force=False, dry_run=False)

    # 1) 合并结果与已发布一致：无变化，不新增版本
    cli.cmd_remerge(args)
    assert "无变化 1 条" in capsys.readouterr().out
    with _session_factory()() as db:
        assert db.get(worker.Item, item_id).source_revision == rev0

    # 2) 换合并逻辑（模拟新断句）：发布新来源版本
    def fake_merge(manifest, results):
        text = "".join((res.get("text") or "") for res in results)
        return [{"start_s": 0.0, "end_s": 40.0, "text": text}], []

    monkeypatch.setattr(asr_mod, "_merge_results", fake_merge)
    cli.cmd_remerge(args)
    assert "重算 1 条" in capsys.readouterr().out
    with _session_factory()() as db:
        assert db.get(worker.Item, item_id).source_revision == rev0 + 1

    # 3) 同样逻辑再跑一遍：已与发布一致，幂等跳过
    cli.cmd_remerge(args)
    assert "无变化 1 条" in capsys.readouterr().out
    with _session_factory()() as db:
        assert db.get(worker.Item, item_id).source_revision == rev0 + 1

    # 4) 用户编辑原文后的版本带 edited_by_user：remerge 必须跳过不覆盖
    with _session_factory()() as db:
        cur_rev = db.get(worker.Item, item_id).source_revision
    r = client.post(f"/v1/items/{item_id}/source-text",
                    json={"expected_source_revision": cur_rev,
                          "text": "我自己整理过的正文，一行一块。"},
                    headers=auth(user_a["desktop"]["token"]))
    assert r.status_code == 202
    cli.cmd_remerge(args)
    out = capsys.readouterr().out
    assert "重算 0 条" in out and "无变化 0 条" in out
    with _session_factory()() as db:
        it = db.get(worker.Item, item_id)
        assert it.source_revision == cur_rev + 1  # 编辑产生的新版本未被 remerge 动过

    # 5) --force 显式覆盖人工编辑：重算发布更新版本
    force_args = argparse.Namespace(command="remerge", user=None, item=item_id,
                                    force=True, dry_run=False)
    cli.cmd_remerge(force_args)
    assert "重算 1 条" in capsys.readouterr().out
    with _session_factory()() as db:
        assert db.get(worker.Item, item_id).source_revision == cur_rev + 2
