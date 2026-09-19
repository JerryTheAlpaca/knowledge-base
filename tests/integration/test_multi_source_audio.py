"""多来源音频接入测试（docs/13 §9 验收表）。

覆盖：来源标签/图标派生、静态网页音频候选解析、音频直链探测、上传分块续传
（幂等/冲突/恢复/完成）、上传录音 → 对象输入 → 现有 ASR 全链路、原件引用保护
与跨用户隔离、10 小时时长边界（含恰好 36000 秒）。

网络探测与解码用桩；时长边界用固定引擎输出，均不代表真实 ASR 质量（docs/13 §9）。
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import auth

from kbserver.audio import prepare as audio_prepare
from kbserver.domain import source_labels
from kbserver.extractors import audio_sources
from kbserver.models import AudioAsset, AudioUploadSession, Item, Upload
from kbserver.security.safe_fetch import ProbeResult
from kbserver.storage.objects import ObjectStore
from kbserver.workers import worker

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


# ---- 来源标签/图标 ----

def test_source_label_mapping_is_distinct_and_text_only():
    """来源标签是纯文字且各自独立：B 站 / 微信公众号 / 网页 / 网页音频 / 上传录音。"""
    bili = source_labels.source_fields("bilibili", "video")
    wx = source_labels.source_fields("wechat_mp", "text")
    web_text = source_labels.source_fields("web", "text")
    web_audio = source_labels.source_fields("web", "audio")
    upload = source_labels.source_fields("audio_upload", "audio")

    assert (bili["source_type"], bili["source_label"]) == ("bilibili", "B 站")
    assert (wx["source_type"], wx["source_label"], wx["platform"]) == (
        "wechat_mp", "微信公众号", "wechat_mp")
    assert (web_text["source_type"], web_text["source_label"]) == ("web", "网页")
    assert (web_audio["source_type"], web_audio["source_label"]) == (
        "web_audio", "网页音频")
    assert (upload["source_type"], upload["source_label"]) == (
        "audio_upload", "上传录音")
    # 公众号不被并进"网页"；只有普通网页才是"网页"
    assert wx["source_type"] != web_text["source_type"]
    # 标签是纯文字：不含表情符号或图标符号
    for f in (bili, wx, web_text, web_audio, upload):
        assert f["source_label"] == f["source_label"].strip()
        assert re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9 ]+", f["source_label"]), f["source_label"]
    labels = {bili["source_label"], wx["source_label"], web_text["source_label"],
              web_audio["source_label"], upload["source_label"]}
    assert len(labels) == 5


def test_capture_channel_is_not_a_platform():
    """采集渠道（web_inbox）不是平台：平台按 URL 判定，公众号仍是 wechat_mp。"""
    from kbserver.domain import pipeline

    assert pipeline._capture_platform(
        {"source_hint": "web_inbox", "original_url": "https://mp.weixin.qq.com/s/x"}
    ) == ("wechat_mp", "text")
    assert pipeline._capture_platform(
        {"source_hint": "web_inbox", "original_url": "https://www.bilibili.com/video/BV1x"}
    ) == ("bilibili", "video")
    assert pipeline._capture_platform(
        {"source_hint": "web_inbox", "original_url": "https://example.com/post"}
    ) == ("web", "text")
    # 无 URL 的纯文字/文件采集不冒充平台
    assert pipeline._capture_platform(
        {"source_hint": "web_inbox", "input_kind": "text"}
    ) == ("unknown", "text")
    # 旧记录把渠道写进 platform 时，展示与来源判定都回退到 URL
    assert source_labels.source_fields("web_inbox", None)["source_label"] == "未知"
    assert source_labels.resolve_platform(
        "web_inbox", "https://mp.weixin.qq.com/s/x") == "wechat_mp"
    # 音频转写意图下公众号链接也是网页音频，但平台不丢
    assert pipeline._capture_platform(
        {"processing_intent": "transcribe_audio",
         "original_url": "https://mp.weixin.qq.com/s/x"}
    ) == ("wechat_mp", "audio")


def test_legacy_records_do_not_backfill_audio():
    """旧 web 记录缺 media_kind 时保持网页，不因 asr 字样强行回填音频。"""
    assert source_labels.source_fields("web", None)["source_type"] == "web"
    assert source_labels.source_fields("wechat_mp", None)["media_kind"] == "text"


# ---- 静态网页音频候选 ----

def test_parse_audio_candidates_dedupes_formats_and_relative_urls():
    html = b"""
    <html><head><title>podcast page</title>
    <meta property="og:audio" content="/media/cover.mp3">
    <script type="application/ld+json">
      {"@type":"AudioObject","contentUrl":"/media/ld.json.mp3"}
    </script>
    </head><body>
      <audio><source src="a.mp3" type="audio/mpeg"><source src="a.ogg" type="audio/ogg"></audio>
    </body></html>
    """
    cands = audio_sources.parse_audio_candidates(html, "https://example.com/post/1")
    urls = [c["url"] for c in cands]
    # 同一 <audio> 下的两种编码合并为一条；og:audio 与 JSON-LD 各一条
    assert urls == ["https://example.com/post/a.mp3",
                    "https://example.com/media/cover.mp3",
                    "https://example.com/media/ld.json.mp3"]
    assert cands[0]["candidate_id"] and len(cands[0]["candidate_id"]) == 16


# ---- 音频直链探测 ----

def test_web_direct_audio_uses_mime_not_suffix(monkeypatch):
    monkeypatch.setattr(audio_sources, "probe_url", lambda url, **kw: ProbeResult(
        url="https://cdn.example.com/signed?token=x", status_code=200,
        mime="audio/mpeg", content_length=None, accept_ranges=True, sample=b""))
    from kbserver.config import get_settings

    resolved = audio_sources._web_direct_or_page(
        "https://example.com/play?id=1", get_settings(), None)
    assert resolved.source.platform == "web"
    assert resolved.source.media_kind == "audio"
    # 持久化定位不含带时效签名的素材地址
    assert "token=" not in str(resolved.source.locator())
    assert resolved.input.urls[0].startswith("https://cdn.example.com/")


def test_web_page_audio_multiple_candidates_requires_selection(monkeypatch):
    monkeypatch.setattr(audio_sources, "probe_url", lambda url, **kw: ProbeResult(
        url=url, status_code=200, mime="text/html", content_length=10,
        accept_ranges=False, sample=b""))
    html = b'<audio src="/a.mp3"></audio><audio src="/b.mp3"></audio>'
    monkeypatch.setattr(audio_sources, "safe_fetch", lambda url, **kw: SimpleNamespace(
        url=url, content=html, mime="text/html"))

    from kbserver.config import get_settings

    with pytest.raises(audio_sources.AudioSourceSelectionRequired) as exc:
        audio_sources._web_direct_or_page("https://example.com/p", get_settings(), None)
    candidates = audio_sources.candidate_list(exc.value.candidates)
    assert len(candidates) == 2 and all(c["candidate_id"] for c in candidates)

    chosen_id = candidates[1]["candidate_id"]
    resolved = audio_sources._web_direct_or_page(
        "https://example.com/p", get_settings(), chosen_id)
    assert resolved.input.urls == ["https://example.com/b.mp3"]
    # 不复用「客户端直接提交的新 URL」：只能按服务端候选 ID 选择
    with pytest.raises(audio_sources.AudioSourceSelectionRequired):
        audio_sources._web_direct_or_page("https://example.com/p", get_settings(), "deadbeef")


# ---- 上传分块续传 ----

def _create_session(client, token, key=None, total=10, filename="meeting.m4a"):
    return client.post("/v1/audio-uploads",
                       json={"filename": filename, "total_bytes": total,
                             "mime": "audio/mp4"},
                       headers=auth(token))


def _put_chunk(client, token, session_id, offset, data: bytes):
    return client.put(f"/v1/audio-uploads/{session_id}/chunks", content=data,
                      headers={**auth(token), "X-Upload-Offset": str(offset),
                               "X-Chunk-Sha256": hashlib.sha256(data).hexdigest(),
                               "Content-Type": "application/octet-stream"})


def test_audio_upload_chunked_resume_and_idempotency(client, user_a, user_b):
    payload = b"ABCDEFGHIJ"
    r = _create_session(client, user_a["phone"]["token"], total=len(payload))
    assert r.status_code == 201
    sess = r.json()
    assert sess["offset"] == 0 and sess["max_duration_seconds"] == 36000

    r = _put_chunk(client, user_a["phone"]["token"], sess["session_id"], 0, b"ABCDE")
    assert r.status_code == 200 and r.json()["offset"] == 5
    # 同 offset 同摘要重传幂等
    r = _put_chunk(client, user_a["phone"]["token"], sess["session_id"], 0, b"ABCDE")
    assert r.status_code == 409 and r.json()["error"]["details"]["expected_offset"] == 5
    # 偏移冲突
    r = _put_chunk(client, user_a["phone"]["token"], sess["session_id"], 9, b"X")
    assert r.status_code == 409
    # 块摘要不符 → 422，且未推进 offset
    r = client.put(f"/v1/audio-uploads/{sess['session_id']}/chunks", content=b"FGHIJ",
                   headers={**auth(user_a["phone"]["token"]), "X-Upload-Offset": "5",
                            "X-Chunk-Sha256": "0" * 64})
    assert r.status_code == 422
    r = client.get(f"/v1/audio-uploads/{sess['session_id']}",
                   headers=auth(user_a["phone"]["token"]))
    assert r.json()["offset"] == 5
    # 他人不可见
    r = client.get(f"/v1/audio-uploads/{sess['session_id']}",
                   headers=auth(user_b["phone"]["token"]))
    assert r.status_code == 404

    # 续传剩下部分并完成
    r = _put_chunk(client, user_a["phone"]["token"], sess["session_id"], 5, b"FGHIJ")
    assert r.status_code == 200 and r.json()["offset"] == 10
    r = client.post(f"/v1/audio-uploads/{sess['session_id']}/complete",
                    headers=auth(user_a["phone"]["token"]))
    assert r.status_code == 200
    out = r.json()
    assert out["sha256"] == hashlib.sha256(payload).hexdigest() and out["bytes"] == 10
    # 完成幂等
    r2 = client.post(f"/v1/audio-uploads/{sess['session_id']}/complete",
                     headers=auth(user_a["phone"]["token"]))
    assert r2.status_code == 200 and r2.json()["upload_id"] == out["upload_id"]


def test_audio_upload_recovers_truncated_tail(client, user_a):
    """块写盘与 offset 更新之间崩溃：按最后确认 offset 截去未提交尾部。"""
    store = ObjectStore()
    r = _create_session(client, user_a["phone"]["token"], total=6)
    sid = r.json()["session_id"]
    _put_chunk(client, user_a["phone"]["token"], sid, 0, b"ABC")
    # 模拟崩溃留下的未提交尾巴（数据库 offset 仍为 3）
    with open(store.audio_upload_dir() / _staging_name(sid), "ab") as f:
        f.write(b"ZZZ")
    _put_chunk(client, user_a["phone"]["token"], sid, 3, b"DEF")
    r = client.post(f"/v1/audio-uploads/{sid}/complete",
                    headers=auth(user_a["phone"]["token"]))
    assert r.status_code == 200
    assert r.json()["sha256"] == hashlib.sha256(b"ABCDEF").hexdigest()


def _staging_name(session_id: str) -> str:
    from kbserver.db import get_session_factory

    with get_session_factory()() as db:
        return db.get(AudioUploadSession, session_id).staging_path


def test_audio_upload_cancel_releases_staging(client, user_a):
    r = _create_session(client, user_a["phone"]["token"], total=4)
    sid = r.json()["session_id"]
    _put_chunk(client, user_a["phone"]["token"], sid, 0, b"AB")
    r = client.delete(f"/v1/audio-uploads/{sid}", headers=auth(user_a["phone"]["token"]))
    assert r.status_code == 200 and r.json()["released_staging"] is True
    r = client.get(f"/v1/audio-uploads/{sid}", headers=auth(user_a["phone"]["token"]))
    assert r.json()["state"] == "cancelled"


def test_audio_upload_rejects_over_limit(client, user_a, monkeypatch):
    monkeypatch.setenv("MAX_AUDIO_UPLOAD_BYTES", "1024")
    r = _create_session(client, user_a["phone"]["token"], total=2048)
    assert r.status_code == 413


# ---- 上传录音 → 对象输入 → ASR 全链路 ----

def _make_duration_ffmpeg(tmp_path: Path, seconds: int) -> Path:
    if os.name == "nt":
        path = tmp_path / "ffdur.cmd"
        path.write_text(f'@"{sys.executable}" "{FIXTURES / "fake_ffmpeg_duration.py"}" %*\r\n',
                        encoding="mbcs")
    else:
        path = tmp_path / "ffdur"
        path.write_text(f'#!/bin/sh\nexec "{sys.executable}" '
                        f'"{FIXTURES / "fake_ffmpeg_duration.py"}" "$@"\n', encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


class AlwaysAllowGate:
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


@pytest.fixture()
def asr_object_env(monkeypatch, tmp_path):
    """对象输入 prepare 环境：半长假 FFmpeg + 假引擎 + 模型可用。"""
    from tests.integration.test_asr import _make_wrapper
    from kbserver.workers import asr as asr_mod

    monkeypatch.setenv("ASR_ENABLED", "true")
    monkeypatch.setattr(asr_mod, "model_available", lambda alias: True)
    wrapper_dir = Path(tempfile.mkdtemp(prefix="audiosrc-wrap-"))
    monkeypatch.setenv("ASR_FFMPEG_BIN",
                       str(_make_wrapper(wrapper_dir, "fake-ffmpeg", FIXTURES / "fake_ffmpeg.py")))
    monkeypatch.setenv("ASR_ENGINE_BIN",
                       str(_make_wrapper(wrapper_dir, "fake-sherpa", FIXTURES / "fake_sherpa.py")))
    monkeypatch.setenv("KBI_FAKE_SHERPA_STATE", str(wrapper_dir / "sherpa-state.json"))
    monkeypatch.delenv("KBI_FAKE_SHERPA_FAIL_AT", raising=False)
    return wrapper_dir


def _upload_audio(client, token, key, data: bytes = b"A" * 4096):
    session = client.post("/v1/audio-uploads",
                          json={"filename": "会议录音.m4a", "total_bytes": len(data),
                                "mime": "audio/mp4"},
                          headers=auth(token)).json()
    _put_chunk(client, token, session["session_id"], 0, data)
    return client.post(f"/v1/audio-uploads/{session['session_id']}/complete",
                       headers=auth(token)).json()


def _drain(session_factory, gate=None, max_rounds=80):
    for _ in range(max_rounds):
        if not worker.run_once(session_factory, gate):
            break


def test_uploaded_audio_transcribes_via_object_input(client, user_a, asr_object_env):
    from kbserver.db import get_session_factory

    up = _upload_audio(client, user_a["phone"]["token"], "audio-e2e-0001")
    r = client.post("/v1/captures", json={
        "client_capture_id": "audio-e2e-0001-1111-2222-3333-444444444444",
        "input_kind": "file",
        "processing_intent": "transcribe_audio",
        "primary_audio_upload_id": up["upload_id"],
        "user_note": "产品评审会",
    }, headers={**auth(user_a["phone"]["token"]), "Idempotency-Key": "audio-e2e-0001"})
    assert r.status_code == 202
    item_id = r.json()["item_id"]

    # 原件引用已登记：上传不再按未引用 24h 过期
    with get_session_factory()() as db:
        asset = db.query(AudioAsset).filter(AudioAsset.item_id == item_id).one()
        assert asset.retention_state == "retained"
        assert db.get(Upload, up["upload_id"]).expires_at is None

    _drain(get_session_factory(), gate=AlwaysAllowGate())
    sf = get_session_factory()
    with sf() as db:
        item = db.get(Item, item_id)
        run = db.query(worker.AsrRun).filter(worker.AsrRun.item_id == item_id).one()
        assert run.state == "succeeded"
        assert run.input_kind == "object"
        assert run.input_fingerprint == f"upload:{up['sha256']}"
        assert item.pipeline_state == "waiting_key"  # 发布后 enrich 无凭据等待

    doc = client.get(f"/v1/items/{item_id}", headers=auth(user_a["desktop"]["token"])).json()
    assert doc["source_type"] == "audio_upload" and doc["source_label"] == "上传录音"
    assert doc["platform"] == "audio_upload" and doc["media_kind"] == "audio"
    assert doc["audio_original_retained"] is True
    assert doc["audio_original_download"] == f"/v1/items/{item_id}/audio-original"

    manifest = client.get(
        f"/v1/items/{item_id}/bundles/{doc['bundle_revision']}/manifest",
        headers=auth(user_a["desktop"]["token"])).json()
    src = manifest["source"]
    assert src["source_type"] == "audio_upload"
    assert src["original_media_retained"] is True
    paths = [f["relative_path"] for f in manifest["files"]]
    assert "asr/original_audio.json" in paths  # 只投递原件引用，不投递大文件
    assert not any(p.startswith("uploads/会议录音") for p in paths)


def test_uploaded_audio_original_download_and_isolation(client, user_a, user_b, asr_object_env):
    from kbserver.db import get_session_factory

    data = b"B" * 2048
    up = _upload_audio(client, user_a["phone"]["token"], "audio-dl-0001", data)
    r = client.post("/v1/captures", json={
        "client_capture_id": "audio-dl-0001-1111-2222-3333-444444444444",
        "input_kind": "file", "processing_intent": "transcribe_audio",
        "primary_audio_upload_id": up["upload_id"],
    }, headers={**auth(user_a["phone"]["token"]), "Idempotency-Key": "audio-dl-0001"})
    item_id = r.json()["item_id"]

    r = client.get(f"/v1/items/{item_id}/audio-original",
                   headers=auth(user_a["desktop"]["token"]))
    assert r.status_code == 200 and r.content == data
    # 跨用户不能下载
    r = client.get(f"/v1/items/{item_id}/audio-original",
                   headers=auth(user_b["desktop"]["token"]))
    assert r.status_code == 404
    # 纯文字条目没有原件
    r2 = client.post("/v1/captures", json={
        "client_capture_id": "audio-dl-0002-1111-2222-3333-444444444444",
        "input_kind": "text", "text": "普通文字条目",
    }, headers={**auth(user_a["phone"]["token"]), "Idempotency-Key": "audio-dl-0002"})
    r = client.get(f"/v1/items/{r2.json()['item_id']}/audio-original",
                   headers=auth(user_a["desktop"]["token"]))
    assert r.status_code == 404


def test_original_audio_survives_retention_sweep(client, user_a, asr_object_env, monkeypatch):
    """转写成功后原件仍受引用保护，清理任务不删除；删除条目后才释放。"""
    from kbserver.db import get_session_factory

    up = _upload_audio(client, user_a["phone"]["token"], "audio-ret-0001", b"E" * 3072)
    r = client.post("/v1/captures", json={
        "client_capture_id": "audio-ret-0001-1111-2222-3333-444444444444",
        "input_kind": "file", "processing_intent": "transcribe_audio",
        "primary_audio_upload_id": up["upload_id"],
    }, headers={**auth(user_a["phone"]["token"]), "Idempotency-Key": "audio-ret-0001"})
    item_id = r.json()["item_id"]
    _drain(get_session_factory(), gate=AlwaysAllowGate())

    sf = get_session_factory()
    store = ObjectStore()
    with sf() as db:
        upload = db.get(Upload, up["upload_id"])
        key = upload.storage_key
        assert store.object_exists(key)
    # 把 Bundle 全部设为过期并清理：原件对象必须仍在
    with sf() as db:
        from kbserver.models import BundleRevision, utcnow
        for b in db.query(BundleRevision).all():
            b.expires_at = utcnow()
        db.commit()
    stats = worker.retention_sweep(sf, store)
    assert stats["expired_bundles"] >= 1
    assert store.object_exists(key)  # 原件仍被 AudioAsset 引用

    # 删除条目 → 释放引用；再次清理后对象可回收
    client.delete(f"/v1/items/{item_id}", headers=auth(user_a["desktop"]["token"]))
    with sf() as db:
        asset = db.query(AudioAsset).filter(AudioAsset.item_id == item_id).one()
        assert asset.retention_state == "released"
    worker.retention_sweep(sf, store)
    assert not store.object_exists(key)


def test_same_content_two_users_object_not_deleted(client, user_a, user_b, asr_object_env):
    """同内容两用户各一条目：删除其中一条不得删掉另一用户的对象。"""
    from kbserver.db import get_session_factory

    data = b"C" * 1024
    ids = []
    for name, user in (("a", user_a), ("b", user_b)):
        up = _upload_audio(client, user["phone"]["token"], f"audio-two-{name}-0001", data)
        r = client.post("/v1/captures", json={
            "client_capture_id": f"audio-two-{name}-0001-1111-2222-3333-444444444444",
            "input_kind": "file", "processing_intent": "transcribe_audio",
            "primary_audio_upload_id": up["upload_id"],
        }, headers={**auth(user["phone"]["token"]), "Idempotency-Key": f"audio-two-{name}-0001"})
        ids.append((user, r.json()["item_id"]))
        assert up["sha256"] == hashlib.sha256(data).hexdigest()

    store = ObjectStore()
    key = store.storage_key(hashlib.sha256(data).hexdigest())
    assert store.object_exists(key)
    client.delete(f"/v1/items/{ids[0][1]}", headers=auth(ids[0][0]["desktop"]["token"]))
    sf = get_session_factory()
    worker.retention_sweep(sf, store)
    assert store.object_exists(key)  # 另一用户仍在引用


# ---- 10 小时时长边界 ----

def _prepare_with_seconds(tmp_path, monkeypatch, seconds: int, hint: float | None = None,
                          rate: int = 1, chunk_seconds: int = 20):
    """构造指定时长输入并跑一次 prepare。

    rate=1（默认）只验证时长/切段记账，避免生成真实 16k PCM 的 GB 级磁盘占用；
    需要核验采样精度时显式传 rate=16000。
    """
    from kbserver.audio.types import AudioSource, RemoteAudioInput

    ff = _make_duration_ffmpeg(tmp_path, seconds)
    monkeypatch.setenv("KBI_FAKE_FFMPEG_SECONDS", str(seconds))
    monkeypatch.setenv("KBI_FAKE_FFMPEG_RATE", str(rate))
    source = AudioSource(platform="web", media_kind="audio", adapter_id="direct_audio",
                         adapter_version="t", duration_hint=hint)
    inp = RemoteAudioInput(source=source, urls=["https://example.com/a.mp3"],
                           headers={}, duration_hint=hint)
    monkeypatch.setattr(audio_prepare, "stream_to_sink", _FakeSink())
    limits = audio_prepare.AudioLimits(ffmpeg_bin=str(ff), chunk_seconds=chunk_seconds,
                                       max_bytes=10 * 1024**3, max_duration_s=36000)
    return audio_prepare.prepare_audio(inp, tmp_path, limits=limits)


class _FakeSink:
    def __call__(self, url, *, sink, max_bytes, timeout=20.0, headers=None,
                 should_cancel=None, on_progress=None):
        from kbserver.security.safe_fetch import StreamResult

        sink(b"x" * 4096)
        if on_progress:
            on_progress(4096)
        return StreamResult(url=url, status_code=200, bytes_read=4096)


def test_remote_input_decodes_from_a_seekable_local_file(tmp_path, monkeypatch):
    """远程音频先整条落盘，再让 FFmpeg 读本地文件。

    直连管道时输入不可 seek，moov 在尾部的常见 M4A 会被判成
    audio_stream_unsupported —— 那类失败是终态、一次都不重试，等于网页音频
    白传一趟。临时文件在解码产物齐全后必须消失，不然多占一份盘。
    """
    seen: dict = {}
    real_command = audio_prepare._ffmpeg_command

    def spy(limits, chunks_dir, source_arg):
        seen["source_arg"] = source_arg
        seen["is_file"] = os.path.isfile(source_arg)
        seen["bytes"] = os.path.getsize(source_arg) if seen["is_file"] else -1
        return real_command(limits, chunks_dir, source_arg)

    monkeypatch.setattr(audio_prepare, "_ffmpeg_command", spy)
    prepared = _prepare_with_seconds(tmp_path, monkeypatch, 40)

    assert seen["is_file"], f"解码输入应是本地文件，实际是 {seen['source_arg']}"
    assert seen["bytes"] == prepared.source_bytes == 4096
    assert not (tmp_path / audio_prepare.REMOTE_INPUT_FILE).exists()
    assert prepared.timings["download_s"] >= 0


def test_duration_boundary_accepts_exactly_36000(tmp_path, monkeypatch):
    prepared = _prepare_with_seconds(tmp_path, monkeypatch, 36000)
    assert prepared.total_duration == pytest.approx(36000, abs=0.01)
    assert len(prepared.chunks) == 1800


def test_duration_boundary_rejects_over_36000(tmp_path, monkeypatch):
    from kbserver.audio.types import AudioPrepareError

    with pytest.raises(AudioPrepareError) as exc:
        _prepare_with_seconds(tmp_path, monkeypatch, 36001)
    assert exc.value.status == "audio_too_long"


def test_declared_duration_over_limit_rejected_before_decode(tmp_path, monkeypatch):
    from kbserver.audio.types import AudioPrepareError

    with pytest.raises(AudioPrepareError) as exc:
        _prepare_with_seconds(tmp_path, monkeypatch, 60, hint=36001.0)
    assert exc.value.status == "audio_too_long"


def test_unknown_duration_still_enforced(tmp_path, monkeypatch):
    """未声明时长的输入也按实际采样数硬限，不无限解码（docs/13 §7.1）。"""
    from kbserver.audio.types import AudioPrepareError

    with pytest.raises(AudioPrepareError) as exc:
        _prepare_with_seconds(tmp_path, monkeypatch, 36060, hint=None)
    assert exc.value.status == "audio_too_long"


def test_duration_accounting_is_sample_accurate(tmp_path, monkeypatch):
    """16kHz 真实采样下按采样数核算时长（10 小时边界用极小采样率只验记账）。"""
    prepared = _prepare_with_seconds(tmp_path, monkeypatch, 60, rate=16000)
    assert prepared.total_duration == pytest.approx(60, abs=0.01)
    assert len(prepared.chunks) == 3 and prepared.chunks[-1].duration == pytest.approx(20)


# ---- 能力/候选接口 ----

def test_audio_sources_endpoint_states(client, user_a):
    r = client.post("/v1/captures", json={
        "client_capture_id": "audio-src-0001-1111-2222-3333-444444444444",
        "input_kind": "text", "text": "普通文字条目",
    }, headers={**auth(user_a["phone"]["token"]), "Idempotency-Key": "audio-src-0001"})
    item_id = r.json()["item_id"]
    r = client.get(f"/v1/items/{item_id}/audio-sources",
                   headers=auth(user_a["desktop"]["token"]))
    assert r.status_code == 200
    assert r.json()["state"] == "unsupported"
    # 纯文字条目不支持转写（不再是"只有 B 站"语义，而是"没有音频来源"）
    r = client.post(f"/v1/items/{item_id}/asr", json={},
                    headers=auth(user_a["desktop"]["token"]))
    assert r.status_code == 422


def test_trigger_on_uploaded_audio_item_is_supported(client, user_a, asr_object_env):
    from kbserver.db import get_session_factory

    up = _upload_audio(client, user_a["phone"]["token"], "audio-trig-0001")
    r = client.post("/v1/captures", json={
        "client_capture_id": "audio-trig-0001-1111-2222-3333-444444444444",
        "input_kind": "file", "processing_intent": "transcribe_audio",
        "primary_audio_upload_id": up["upload_id"],
    }, headers={**auth(user_a["phone"]["token"]), "Idempotency-Key": "audio-trig-0001"})
    item_id = r.json()["item_id"]
    assert r.status_code == 202
    # 已自动入队：重复触发幂等，不新建 run
    r = client.post(f"/v1/items/{item_id}/asr", json={},
                    headers=auth(user_a["desktop"]["token"]))
    assert r.status_code == 200 and r.json()["created"] is False
    assert r.json()["asr_supported"] is True and r.json()["audio_kind"] == "upload"
    with get_session_factory()() as db:
        assert db.query(worker.Job).filter(
            worker.Job.item_id == item_id, worker.Job.stage == "asr_prepare").count() == 1


def test_legacy_channel_platform_renders_real_source(client, user_a):
    """旧记录把渠道 web_inbox 写进 platform：展示按 URL 回退，不显示渠道名。"""
    from kbserver.db import get_session_factory
    from kbserver.models import SourceRevision

    r = client.post("/v1/captures", json={
        "client_capture_id": "legacy-wx-0001-1111-2222-3333-444444444444",
        "input_kind": "url", "capture_channel": "web_inbox", "source_hint": "unknown",
        "original_url": "https://mp.weixin.qq.com/s/abcdef",
    }, headers={**auth(user_a["phone"]["token"]), "Idempotency-Key": "legacy-wx-0001"})
    item_id = r.json()["item_id"]
    # 模拟旧记录：platform 字段里存的是采集渠道而不是平台
    with get_session_factory()() as db:
        src = db.query(SourceRevision).filter(SourceRevision.item_id == item_id).one()
        meta = dict(src.metadata_json)
        meta["platform"] = "web_inbox"
        src.metadata_json = meta
        db.commit()

    doc = client.get(f"/v1/items/{item_id}", headers=auth(user_a["desktop"]["token"])).json()
    assert doc["platform"] == "wechat_mp" and doc["source_label"] == "微信公众号"
    assert doc["source_type"] == "wechat_mp"


def test_normal_web_capture_does_not_enter_asr(client, user_a, monkeypatch):
    """网页仅正文、上传普通附件：沿用旧流程，不自动进入 ASR。"""
    from kbserver.db import get_session_factory
    from kbserver.extractors import webpages

    monkeypatch.setattr(webpages, "extract", lambda url, **kw: SimpleNamespace(
        platform="web", canonical_url="https://example.com/a", title="标题", author=None,
        published_at=None, raw_html=b"<html></html>", raw_mime="text/html",
        segments=[{"segment_id": "s0001", "text": "正文一段", "locator": {},
                   "origin": "web_article", "confidence": None, "kind": "paragraph"}],
        images=[], missing_materials=[], warnings=[], coverage="full_text"))
    r = client.post("/v1/captures", json={
        "client_capture_id": "web-noasr-0001-1111-2222-3333-444444444444",
        "input_kind": "url", "original_url": "https://example.com/a",
    }, headers={**auth(user_a["phone"]["token"]), "Idempotency-Key": "web-noasr-0001"})
    item_id = r.json()["item_id"]
    _drain(get_session_factory())
    with get_session_factory()() as db:
        assert db.query(worker.AsrRun).filter(worker.AsrRun.item_id == item_id).count() == 0
        assert db.query(worker.Job).filter(
            worker.Job.item_id == item_id, worker.Job.stage.like("asr%")).count() == 0


def test_republish_replaces_stale_readable_in_manifest(client, user_a, monkeypatch):
    """重发布必须替换同路径旧文件：清单里两份 readable.md 时读侧命中靠前的旧版，
    转写完成后「下载原文」仍只有标题（网页壳页 → 补充正文 → 转写发布）。
    """
    from kbserver.db import get_session_factory
    from kbserver.extractors import webpages
    from kbserver.models import Item, SourceRevision
    from kbserver.workers.publish import publish_segments_revision

    token = user_a["desktop"]["token"]
    monkeypatch.setattr(webpages, "extract", lambda url, **kw: SimpleNamespace(
        platform="web", canonical_url="https://example.com/podcast", title="播客第 1 期",
        author=None, published_at=None, raw_html=b"<html></html>", raw_mime="text/html",
        segments=[{"segment_id": "s0001", "text": "播客第 1 期", "locator": {},
                   "origin": "web_article", "confidence": None, "kind": "heading"}],
        images=[], missing_materials=[], warnings=[], coverage="full_text"))
    item_id = client.post("/v1/captures", json={
        "client_capture_id": "republish-0001-1111-2222-3333-444444444444",
        "input_kind": "url", "original_url": "https://example.com/podcast",
    }, headers={**auth(token), "Idempotency-Key": "republish-0001"}).json()["item_id"]

    sf = get_session_factory()
    _drain(sf)

    def readable() -> str:
        r = client.get(f"/v1/items/{item_id}/reading", headers=auth(token)).json()
        return (r["source_material"] or {})["readable_md"] or ""

    assert readable().strip() == "## 播客第 1 期 ^p0001"

    rev = client.get(f"/v1/items/{item_id}", headers=auth(token)).json()["source_revision"]
    client.post(f"/v1/items/{item_id}/supplements",
                json={"expected_source_revision": rev, "text": "人工补充的第一段。"},
                headers=auth(token))
    _drain(sf)
    assert "人工补充的第一段" in readable()

    # 转写完成：ASR 与提取共用这条发布路径产出全文
    with sf() as db:
        item = db.get(Item, item_id)
        source = db.query(SourceRevision).filter(
            SourceRevision.item_id == item.id,
            SourceRevision.revision == item.source_revision).one()
        publish_segments_revision(
            db, ObjectStore(), None, item, source,
            segments=[{"segment_id": "s0001", "text": "这里是机器转写的正文。",
                       "locator": {}, "origin": "asr", "confidence": None}],
            warnings=[], extra_files=[], meta_updates={"coverage": "full_text"})
        db.commit()

    assert "这里是机器转写的正文" in readable()
    with sf() as db:
        revision = db.get(Item, item_id).bundle_revision
    paths = [f["relative_path"] for f in client.get(
        f"/v1/items/{item_id}/bundles/{revision}/manifest", headers=auth(token)
    ).json()["files"]]
    for path in ("readable.md", "normalized.md", "segments.json"):
        assert paths.count(path) == 1, (path, paths)
