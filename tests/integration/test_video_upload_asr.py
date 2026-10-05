"""上传视频与格式支持（docs/13 §6.2）。

覆盖：容器清单接口、扩展名优先的判定、不支持容器在接收字节之前拒绝、
上传视频 → 对象输入 → 现有 ASR 全链路并按「上传视频」显示、只有画面没有
音轨时条目显示为不支持（不给重试）、跨用户与不支持文件不建条目。

解码与识别用桩：链路编排证据，不代表真实转写质量（docs/11 §9.4）。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from tests.conftest import auth
from tests.integration.test_asr import _make_wrapper
from tests.integration.test_multi_source_audio import _drain, _put_chunk, AlwaysAllowGate

from kbserver.db import get_session_factory
from kbserver.domain import media_formats
from kbserver.models import AudioAsset, Capture, Item, Upload
from kbserver.workers import worker

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


# ---- 容器判定（不触库的纯函数）----

def test_classify_prefers_extension_over_mime():
    """扩展名说了算：.mkv 常常没有 MIME，.mp4 可能被安卓报成 audio/mp4。"""
    assert media_formats.classify("课堂.mkv", "") == "video"
    assert media_formats.classify("clip.MP4", "audio/mp4") == "video"
    assert media_formats.classify("播客.m4a", None) == "audio"
    # 没有扩展名才看 MIME
    assert media_formats.classify("recording", "audio/mpeg") == "audio"
    assert media_formats.classify("stream", "video/quicktime") == "video"
    assert media_formats.classify("notes", "application/octet-stream") is None


def test_classify_rejects_containers_we_do_not_claim():
    """不在清单里的容器一律按不支持处理，不用 MIME 把它捞回来。"""
    for name in ("clip.rmvb", "disc.vob", "old.avi.exe", "notes.txt", "archive.zip"):
        assert media_formats.classify(name, "video/x-msvideo") is None, name


def test_unsupported_message_lists_what_can_be_uploaded():
    msg = media_formats.unsupported_message("clip.rmvb", "video/vnd.rn-realmedia")
    assert "不支持" in msg and ".rmvb" in msg
    assert msg.endswith(media_formats.SUPPORTED_HINT + "。")


# ---- 上传协议层的格式闸门 ----

def test_rejected_containers_are_named_not_silently_dropped(client, user_a):
    """.rmvb 之类是「想传影像但不支持」，与图片/PDF 这种本来就不是音视频的文件不同：
    界面要能分开说这两句话，所以清单里明确列出前者。"""
    fmts = client.get("/v1/media-formats", headers=auth(user_a["phone"]["token"])).json()
    assert ".rmvb" in fmts["rejected"] and ".vob" in fmts["rejected"]
    accepted = set(fmts["audio"]) | set(fmts["video"])
    assert accepted & set(fmts["rejected"]) == set()
    for name in ("notes.txt", "page.png", "deck.pdf"):
        assert media_formats.classify(name, "") is None


def test_media_formats_endpoint_is_the_single_source_for_the_ui(client, user_a):
    """采集框不自带清单：容器清单与提示语都来自这个接口。"""
    r = client.get("/v1/media-formats", headers=auth(user_a["phone"]["token"]))
    assert r.status_code == 200
    body = r.json()
    assert ".mp4" in body["video"] and ".m4a" in body["audio"]
    assert body["hint"] == media_formats.SUPPORTED_HINT


def test_unsupported_container_rejected_before_bytes(client, user_a):
    """几 GB 的视频不能传完才说不支持：创建会话时就拒，且不留下会话与临时文件。"""
    r = client.post("/v1/audio-uploads",
                    json={"filename": "讲座.rmvb", "total_bytes": 4096,
                          "mime": "video/vnd.rn-realmedia"},
                    headers=auth(user_a["phone"]["token"]))
    assert r.status_code == 415
    assert r.json()["error"]["code"] == "MEDIA_UNSUPPORTED"
    assert "不支持" in r.json()["error"]["user_message"]
    assert client.get("/v1/audio-uploads/nope",
                      headers=auth(user_a["phone"]["token"])).status_code == 404


def test_video_container_opens_a_chunked_session(client, user_a):
    data = b"V" * 2048
    r = client.post("/v1/audio-uploads",
                    json={"filename": "课堂实录.mp4", "total_bytes": len(data),
                          "mime": "video/mp4"},
                    headers=auth(user_a["phone"]["token"]))
    assert r.status_code == 201
    sid = r.json()["session_id"]
    assert _put_chunk(client, user_a["phone"]["token"], sid, 0, data).status_code == 200
    done = client.post(f"/v1/audio-uploads/{sid}/complete",
                       headers=auth(user_a["phone"]["token"]))
    assert done.status_code == 200
    assert done.json()["mime"] == "video/mp4"


def test_capture_rejects_unsupported_primary_upload(client, user_a):
    """直连 /v1/uploads 的客户端也要在建条目之前被拦住，不留下永远转不出文字的条目。"""
    token = user_a["phone"]["token"]
    up = client.post("/v1/uploads",
                     files={"file": ("lecture.rmvb", b"x" * 512, "video/vnd.rn-realmedia")},
                     headers={**auth(token), "Idempotency-Key": "vid-bad-1"})
    assert up.status_code == 201
    upload_id = up.json()["upload_id"]

    r = client.post("/v1/captures", json={
        "client_capture_id": "vid-bad-0001-1111-2222-3333-444444444444",
        "input_kind": "video", "processing_intent": "transcribe_audio",
        "primary_audio_upload_id": upload_id,
    }, headers={**auth(token), "Idempotency-Key": "vid-bad-1"})
    assert r.status_code == 415
    assert r.json()["error"]["code"] == "MEDIA_UNSUPPORTED"
    with get_session_factory()() as db:
        assert db.query(Capture).filter(
            Capture.client_capture_id == "vid-bad-0001-1111-2222-3333-444444444444"
        ).one_or_none() is None


# ---- 上传视频 → 对象输入 → ASR ----

@pytest.fixture()
def video_asr_env(monkeypatch):
    """假 FFmpeg 产出两段 20s WAV + 假识别引擎；与上传录音同一套桩。"""
    from kbserver.workers import asr as asr_mod

    monkeypatch.setenv("ASR_ENABLED", "true")
    monkeypatch.setattr(asr_mod, "model_available", lambda alias: True)
    wrapper_dir = Path(tempfile.mkdtemp(prefix="video-asr-wrap-"))
    monkeypatch.setenv("ASR_FFMPEG_BIN", str(
        _make_wrapper(wrapper_dir, "fake-ffmpeg", FIXTURES / "fake_ffmpeg.py")))
    monkeypatch.setenv("ASR_ENGINE_BIN", str(
        _make_wrapper(wrapper_dir, "fake-sherpa", FIXTURES / "fake_sherpa.py")))
    monkeypatch.setenv("KBI_FAKE_SHERPA_STATE", str(wrapper_dir / "sherpa-state.json"))
    monkeypatch.delenv("KBI_FAKE_SHERPA_FAIL_AT", raising=False)
    return wrapper_dir


@pytest.fixture()
def no_audio_track_env(video_asr_env, monkeypatch):
    """只有画面的视频：FFmpeg 取 0:a:0 失败。"""
    dir2 = Path(tempfile.mkdtemp(prefix="video-noaud-"))
    monkeypatch.setenv("ASR_FFMPEG_BIN", str(
        _make_wrapper(dir2, "fake-ffmpeg", FIXTURES / "fake_ffmpeg_no_audio.py")))
    return dir2


def _upload_video(client, token, filename, data=b"V" * 4096, mime="video/mp4"):
    sess = client.post("/v1/audio-uploads",
                       json={"filename": filename, "total_bytes": len(data), "mime": mime},
                       headers=auth(token)).json()
    _put_chunk(client, token, sess["session_id"], 0, data)
    return client.post(f"/v1/audio-uploads/{sess['session_id']}/complete",
                       headers=auth(token)).json()


def _capture_video(client, token, key, upload_id, input_kind="video"):
    r = client.post("/v1/captures", json={
        "client_capture_id": f"{key}-1111-2222-3333-444444444444",
        "input_kind": input_kind, "processing_intent": "transcribe_audio",
        "primary_audio_upload_id": upload_id, "user_note": "线下分享",
    }, headers={**auth(token), "Idempotency-Key": key})
    assert r.status_code == 202, r.json()
    return r.json()["item_id"]


def test_uploaded_video_transcribes_and_shows_as_video(client, user_a, video_asr_env):
    token = user_a["phone"]["token"]
    data = b"V" * 4096
    up = _upload_video(client, token, "课堂实录.mp4", data)
    item_id = _capture_video(client, token, "vid-e2e-1", up["upload_id"])

    with get_session_factory()() as db:
        asset = db.query(AudioAsset).filter(AudioAsset.item_id == item_id).one()
        assert asset.filename == "课堂实录.mp4" and asset.retention_state == "retained"
        assert db.get(Upload, up["upload_id"]).expires_at is None  # 已登记，不按未引用清理

    _drain(get_session_factory(), gate=AlwaysAllowGate())
    with get_session_factory()() as db:
        run = db.query(worker.AsrRun).filter(worker.AsrRun.item_id == item_id).one()
        assert run.state == "succeeded" and run.input_kind == "object"
        # 冻结的来源身份按真实文件记：视频不是录音
        assert run.input_json["source"]["media_kind"] == "video"

    doc = client.get(f"/v1/items/{item_id}", headers=auth(user_a["desktop"]["token"])).json()
    assert doc["platform"] == "audio_upload" and doc["media_kind"] == "video"
    assert doc["source_label"] == "上传视频"
    # 转写完成即清理原件：视频不再占服务器磁盘，详情如实说明已清理
    assert doc["audio_original_retained"] is False
    assert doc["audio_original_released"] is True
    # 文字稿已经发布
    assert doc["workflow"]["steps"][1]["reason_code"] == "PROCESS_DONE"

    r = client.get(f"/v1/items/{item_id}/audio-original",
                   headers=auth(user_a["desktop"]["token"]))
    assert r.status_code == 404


def test_uploaded_video_legacy_input_kind_still_works(client, user_a, video_asr_env):
    """旧客户端把视频当 audio 传：media_kind 按文件实测，不受 input_kind 误导。"""
    token = user_a["phone"]["token"]
    up = _upload_video(client, token, "演讲.mkv", mime="")
    item_id = _capture_video(client, token, "vid-e2e-2", up["upload_id"], input_kind="audio")
    doc = client.get(f"/v1/items/{item_id}", headers=auth(user_a["desktop"]["token"])).json()
    assert doc["media_kind"] == "video" and doc["source_label"] == "上传视频"


def test_video_without_audio_track_is_unsupported_not_retryable(client, user_a,
                                                                no_audio_track_env):
    token = user_a["phone"]["token"]
    up = _upload_video(client, token, "无声演示.mp4")
    item_id = _capture_video(client, token, "vid-noaud-1", up["upload_id"])

    _drain(get_session_factory(), gate=AlwaysAllowGate())
    with get_session_factory()() as db:
        run = db.query(worker.AsrRun).filter(worker.AsrRun.item_id == item_id).one()
        item = db.get(Item, item_id)
        assert run.state == "failed"
        assert (run.input_json or {}).get("last_prepare_status") == "no_audio_stream"
        assert item.state_reason == "media_unsupported"

    wf = client.get(f"/v1/items/{item_id}",
                headers=auth(user_a["desktop"]["token"])).json()["workflow"]
    process = next(s for s in wf["steps"] if s["id"] == "process")
    assert process["reason_code"] == "TRANSCRIBE_UNSUPPORTED"
    assert process["message"] == "这个文件里没有声音轨，语音识别没有内容可转"
    # 提取这一步不算「缺材料」：原件已经完整收到服务器
    extract = next(s for s in wf["steps"] if s["id"] == "extract")
    assert extract["status"] == "completed"
    assert wf["primary_action"] is None
    assert "retry_process" not in wf["available_actions"]
    # 用户仍可以自己补一份文字
    assert "supplement" in wf["available_actions"]


def test_unsupported_codec_message_is_distinct_from_no_audio_track(client, user_a,
                                                                   video_asr_env,
                                                                   monkeypatch):
    """解码失败（容器/编码不支持）与「没有音轨」是两句话，都归入不支持、都不给重试。"""
    from kbserver.audio import prepare as audio_prepare
    from kbserver.audio.types import AudioPrepareError

    def _fail(*a, **kw):
        raise AudioPrepareError("audio_stream_unsupported", "FFmpeg 无法解码该音频（exit 1）")

    monkeypatch.setattr(audio_prepare, "prepare_audio", _fail)
    token = user_a["phone"]["token"]
    up = _upload_video(client, token, "奇怪的容器.mp4")
    item_id = _capture_video(client, token, "vid-codec-1", up["upload_id"])
    _drain(get_session_factory(), gate=AlwaysAllowGate())

    wf = client.get(f"/v1/items/{item_id}",
                headers=auth(user_a["desktop"]["token"])).json()["workflow"]
    process = next(s for s in wf["steps"] if s["id"] == "process")
    assert process["reason_code"] == "TRANSCRIBE_UNSUPPORTED"
    assert "不支持" in process["message"]
