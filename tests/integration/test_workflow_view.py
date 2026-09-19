"""WorkflowView 验收（docs/17 §10、§14.1）。

- 四阶段推导：提取/加工/整理/发布，用户文案不透传内部枚举。
- 发布只认当前最新 Bundle 的有效 Receipt；摘要不一致不点亮。
- 旧版本回执不冒充新版本已发布。
- 列表 view/search 过滤与诊断接口。
- 不修改既有同步回执协议（docs/02 §10.3）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kbserver.app import create_app
from kbserver.models import (
    AsrRun,
    BundleRevision,
    Capture,
    Item,
    Receipt,
    SourceRevision,
    utcnow,
)
from tests.conftest import auth


@pytest.fixture()
def wc():
    with TestClient(create_app()) as c:
        yield c


def _seed_item(db, user_id: str, *, pipeline_state: str = "queued",
               state_detail: str = "", state_reason: str = "", source_revision: int = 1,
               bundle_revision: int = 0, meta: dict | None = None) -> Item:
    capture = Capture(user_id=user_id, client_capture_id=utcnow().isoformat() + user_id[:6],
                      request_hash="0" * 64, input_json={})
    db.add(capture)
    db.flush()
    item = Item(user_id=user_id, capture_id=capture.id,
                pipeline_state=pipeline_state, state_detail=state_detail,
                state_reason=state_reason,
                source_revision=source_revision, bundle_revision=bundle_revision)
    db.add(item)
    db.flush()
    db.add(SourceRevision(
        item_id=item.id, user_id=user_id, revision=1, content_hash="0" * 64,
        metadata_json=meta or {}, artifacts_json={},
    ))
    db.flush()
    return item


def _add_bundle(db, item: Item, revision: int, *, processing_state: str = "ready",
                source_revision: int = 1, manifest_sha256: str | None = None) -> BundleRevision:
    bundle = BundleRevision(
        item_id=item.id, user_id=item.user_id, revision=revision,
        source_revision=source_revision, manifest_key=f"m/{item.id}/{revision}",
        manifest_sha256=manifest_sha256 or (str(revision) * 64)[:64],
        processing_state=processing_state,
    )
    db.add(bundle)
    return bundle


def _get_item(wc, token: str, item_id: str) -> dict:
    r = wc.get(f"/v1/items/{item_id}", headers=auth(token))
    assert r.status_code == 200
    return r.json()


def _workflow(db, wc, token: str, item: Item) -> dict:
    db.commit()
    return _get_item(wc, token, item.id)["workflow"]


# ---- 发布语义（docs/17 §9.2）----

def test_ready_bundle_without_receipt_waits_for_obsidian(wc, user_a, db):
    item = _seed_item(db, user_a["user_id"], pipeline_state="ready")
    _add_bundle(db, item, 1, processing_state="ready")
    wf = _workflow(db, wc, user_a["desktop"]["token"], item)
    pub = wf["steps"][2]
    assert pub["id"] == "publish"
    assert pub["status"] == "waiting"
    assert pub["message"] == "等待 Obsidian 下载"
    assert wf["delivery"]["status"] == "waiting_obsidian"
    assert wf["overall_state"] == "working"
    assert wf["reason_code"] == "WAITING_OBSIDIAN"


def test_receipt_lights_publish(wc, user_a, db):
    item = _seed_item(db, user_a["user_id"], pipeline_state="ready", bundle_revision=1)
    bundle = _add_bundle(db, item, 1, processing_state="ready")
    db.add(Receipt(user_id=item.user_id, item_id=item.id, bundle_revision=1,
                   device_id=user_a["desktop"]["device_id"],
                   manifest_sha256=bundle.manifest_sha256))
    wf = _workflow(db, wc, user_a["desktop"]["token"], item)
    assert wf["steps"][2]["status"] == "completed"
    assert wf["steps"][2]["message"] == "已发布到 Obsidian"
    assert wf["delivery"]["status"] == "published"
    assert wf["delivery"]["received_at"]
    assert wf["overall_state"] == "published"


def test_stale_receipt_does_not_publish_new_bundle(wc, user_a, db):
    """旧版本有回执、最新版本没有：显示等待发布，不沿用「已发布」（§9.2）。"""
    item = _seed_item(db, user_a["user_id"], pipeline_state="ready")
    bundle1 = _add_bundle(db, item, 1, processing_state="ready")
    db.add(Receipt(user_id=item.user_id, item_id=item.id, bundle_revision=1,
                   device_id=user_a["desktop"]["device_id"],
                   manifest_sha256=bundle1.manifest_sha256))
    _add_bundle(db, item, 2, processing_state="original_only", source_revision=1)
    item.bundle_revision = 2
    wf = _workflow(db, wc, user_a["desktop"]["token"], item)
    assert wf["delivery"]["status"] != "published"
    assert wf["steps"][2]["status"] == "waiting"


def test_receipt_with_wrong_manifest_not_accepted(wc, user_a, db):
    item = _seed_item(db, user_a["user_id"], pipeline_state="ready", bundle_revision=1)
    _add_bundle(db, item, 1, processing_state="ready")
    db.add(Receipt(user_id=item.user_id, item_id=item.id, bundle_revision=1,
                   device_id=user_a["desktop"]["device_id"],
                   manifest_sha256="f" * 64))
    wf = _workflow(db, wc, user_a["desktop"]["token"], item)
    assert wf["steps"][2]["status"] == "waiting"


def test_receipt_not_shared_across_users(wc, user_a, user_b, db):
    """回执按用户隔离：B 的条目不能因 A 的回执点亮发布。"""
    item = _seed_item(db, user_b["user_id"], pipeline_state="ready", bundle_revision=1)
    bundle = _add_bundle(db, item, 1, processing_state="ready")
    db.add(Receipt(user_id=user_a["user_id"], item_id=item.id, bundle_revision=1,
                   device_id=user_a["desktop"]["device_id"],
                   manifest_sha256=bundle.manifest_sha256))
    wf = _workflow(db, wc, user_b["desktop"]["token"], item)
    assert wf["steps"][2]["status"] == "waiting"


def test_no_device_asks_to_connect_obsidian(db):
    """没有桌面设备且无回执：发布步骤提示连接 Obsidian（推导单测；设备令牌随撤销失效，
    故直接验证推导函数，与线上 Web 通道读取同一视图）。"""
    from kbserver.domain.workflow_view import derive_item_workflow

    class _Item:  # 只暴露推导用到的字段
        id = "i"
        user_id = "u"
        source_revision = 1
        bundle_revision = 1
        pipeline_state = "ready"

    class _Bundle:
        revision = 1
        source_revision = 1
        processing_state = "ready"
        manifest_sha256 = "a" * 64

    wf = derive_item_workflow(
        item=_Item(), meta={}, run=None, bundle=_Bundle(), receipt=None,
        has_device=False, active_job=None, auto_enrich=True,
    )
    pub = wf["steps"][2]
    assert pub["status"] == "attention"
    assert pub["message"] == "连接 Obsidian 后自动发布"
    assert wf["primary_action"] == "connect_obsidian"
    assert wf["delivery"]["status"] == "connect_obsidian"


# ---- 缺模型 / 缺正文（docs/17 §14.1）----

def test_waiting_key_maps_to_choose_model(wc, user_a, db):
    item = _seed_item(db, user_a["user_id"], pipeline_state="waiting_key")
    wf = _workflow(db, wc, user_a["desktop"]["token"], item)
    org = wf["steps"][1]
    assert org["status"] == "attention"
    assert org["message"] == "需要选择整理模型"
    assert wf["primary_action"] == "choose_model"
    assert wf["overall_state"] == "attention"
    assert wf["message"] == "需要选择整理模型"


def test_needs_input_maps_to_supplement(wc, user_a, db):
    item = _seed_item(db, user_a["user_id"], pipeline_state="needs_input",
                      meta={"missing_materials": ["main_content"]})
    wf = _workflow(db, wc, user_a["desktop"]["token"], item)
    ext = wf["steps"][0]
    assert ext["status"] == "attention"
    assert ext["message"] == "需要补充正文或字幕"
    assert wf["primary_action"] == "supplement"


def test_bilibili_login_wall_offers_connect_platform(wc, user_a, db):
    """登录墙按 state_reason 机器码判定：未托管 → 连接该平台（审查 C-14）。"""
    item = _seed_item(db, user_a["user_id"], pipeline_state="needs_input",
                      state_reason="login_required",
                      meta={"platform": "bilibili", "media_kind": "video",
                            "missing_materials": ["main_content"]})
    wf = _workflow(db, wc, user_a["desktop"]["token"], item)
    step = wf["steps"][0]
    assert step["message"] == "需要连接 B 站才能读取这条内容"
    assert step["reason_code"] == "WAITING_PLATFORM_AUTH"
    assert wf["primary_action"] == "connect_platform"
    assert "connect_platform" in wf["available_actions"]
    assert "supplement" in wf["available_actions"]  # 补充材料仍是兜底路径


def test_hosted_session_offers_update_instead_of_connect(wc, user_a, db):
    """已托管该平台登录态后仍撞墙：动作换成「更新登录信息」，不再让用户连接。"""
    token = user_a["desktop"]["token"]
    r = wc.put("/v1/platform-sessions/xiaohongshu",
               json={"secret": "a1=abc123456; web_session=0a1b2c3d4e5f;"},
               headers=auth(token))
    assert r.status_code in (200, 201), r.text
    item = _seed_item(db, user_a["user_id"], pipeline_state="needs_input",
                      state_reason="login_required",
                      meta={"platform": "xiaohongshu", "media_kind": "text"})
    wf = _workflow(db, wc, token, item)
    step = wf["steps"][0]
    assert step["reason_code"] == "WAITING_SESSION_UPDATE"
    assert step["message"].startswith("小红书登录态")
    assert wf["primary_action"] == "update_session"


def test_login_wall_on_platform_without_session_ui_needs_content(wc, user_a, db):
    """知乎尚未开放设置页登录态入口：如实按「需要补充正文」呈现，不给死路动作。"""
    item = _seed_item(db, user_a["user_id"], pipeline_state="needs_input",
                      state_reason="login_required",
                      meta={"platform": "zhihu", "media_kind": "text"})
    wf = _workflow(db, wc, user_a["desktop"]["token"], item)
    assert wf["steps"][0]["reason_code"] == "NEEDS_CONTENT"
    assert wf["primary_action"] == "supplement"


def test_non_login_needs_input_does_not_offer_connect(wc, user_a, db):
    """同平台但不是登录原因（如内容已删除）：不出现连接/更新动作。"""
    item = _seed_item(db, user_a["user_id"], pipeline_state="needs_input",
                      state_reason="deleted",
                      meta={"platform": "xiaohongshu", "media_kind": "text"})
    wf = _workflow(db, wc, user_a["desktop"]["token"], item)
    assert wf["steps"][0]["reason_code"] == "NEEDS_CONTENT"
    assert "connect_platform" not in wf["available_actions"]


# ---- 加工阶段：真实百分比（docs/17 §5.4）----

def _add_run(db, item: Item, *, state: str, pause_reason: str = "",
             done: int = 0, count: int = 0) -> AsrRun:
    run = AsrRun(
        user_id=item.user_id, item_id=item.id, source_revision=1,
        recipe_hash="x", model_alias="sense_voice", model_id="m",
        state=state, pause_reason=pause_reason,
        next_chunk_index=done, chunk_count=count,
    )
    db.add(run)
    return run


def test_transcribing_shows_real_percent(wc, user_a, db):
    item = _seed_item(db, user_a["user_id"], pipeline_state="extracting",
                      meta={"media_kind": "audio"})
    _add_run(db, item, state="transcribing", done=63, count=100)
    wf = _workflow(db, wc, user_a["desktop"]["token"], item)
    proc = wf["steps"][1]
    assert proc["status"] == "running"
    assert proc["progress_percent"] == 63
    assert proc["message"] == "正在语音识别 63%"
    assert proc["label"] == "语音识别"
    assert wf["overall_state"] == "working"
    # 提取已完成（来源已定位），整理在等待语音识别
    assert wf["steps"][0]["status"] == "completed"
    assert wf["steps"][2]["message"] == "等待语音识别完成"


def test_transcribing_without_total_shows_no_fake_percent(wc, user_a, db):
    item = _seed_item(db, user_a["user_id"], pipeline_state="extracting",
                      meta={"media_kind": "audio"})
    _add_run(db, item, state="transcribing", done=3, count=0)
    wf = _workflow(db, wc, user_a["desktop"]["token"], item)
    proc = wf["steps"][1]
    assert proc["status"] == "running"
    assert proc["progress_percent"] is None
    assert "%" not in proc["message"]


def test_paused_run_freezes_percent_and_says_waiting(wc, user_a, db):
    item = _seed_item(db, user_a["user_id"], pipeline_state="extracting",
                      meta={"media_kind": "audio"})
    _add_run(db, item, state="paused", pause_reason="resource_busy", done=63, count=100)
    wf = _workflow(db, wc, user_a["desktop"]["token"], item)
    proc = wf["steps"][1]
    assert proc["status"] == "waiting"
    assert proc["message"] == "等待服务器空闲后继续"
    assert proc["progress_percent"] == 63


def test_non_audio_item_omits_process_step(wc, user_a, db):
    """不用 ASR 的条目（网页正文/字幕已是文字）不渲染语音识别节点：三节点直达整理。"""
    item = _seed_item(db, user_a["user_id"], pipeline_state="ready")
    _add_bundle(db, item, 1, processing_state="ready")
    wf = _workflow(db, wc, user_a["desktop"]["token"], item)
    assert [s["id"] for s in wf["steps"]] == ["extract", "organize", "publish"]
    assert wf["steps"][0]["label"] == "提取"
    assert wf["steps"][1]["label"] == "整理"
    assert wf["steps"][2]["label"] == "发布"


def test_audio_item_without_run_keeps_transcribe_step(wc, user_a, db):
    """音频条目（含网页音轨/B 站转写请求）保留语音识别节点，等待触发。"""
    item = _seed_item(db, user_a["user_id"], pipeline_state="queued",
                      meta={"media_kind": "audio", "missing_materials": ["transcript"]})
    wf = _workflow(db, wc, user_a["desktop"]["token"], item)
    proc = wf["steps"][1]
    assert proc["id"] == "process"
    assert proc["label"] == "语音识别"
    assert proc["status"] == "waiting"
    assert proc["message"] == "等待语音识别"
    assert [s["id"] for s in wf["steps"]] == ["extract", "process", "organize", "publish"]


# ---- 列表视图与搜索（docs/17 §10.5、§4.6）----

def test_view_filters_group_items(wc, user_a, db):
    ready_item = _seed_item(db, user_a["user_id"], pipeline_state="ready", bundle_revision=1)
    bundle = _add_bundle(db, ready_item, 1, processing_state="ready")
    db.add(Receipt(user_id=ready_item.user_id, item_id=ready_item.id, bundle_revision=1,
                   device_id=user_a["desktop"]["device_id"],
                   manifest_sha256=bundle.manifest_sha256))
    waiting_item = _seed_item(db, user_a["user_id"], pipeline_state="waiting_key")
    busy_item = _seed_item(db, user_a["user_id"], pipeline_state="enriching")
    token = user_a["desktop"]["token"]

    db.commit()
    r = wc.get("/v1/items?view=published", headers=auth(token)).json()
    assert [it["item_id"] for it in r["items"]] == [ready_item.id]
    r = wc.get("/v1/items?view=attention", headers=auth(token)).json()
    assert [it["item_id"] for it in r["items"]] == [waiting_item.id]
    r = wc.get("/v1/items?view=working", headers=auth(token)).json()
    assert [it["item_id"] for it in r["items"]] == [busy_item.id]


def test_ready_without_receipt_falls_into_working_view(wc, user_a, db):
    """云端成品已生成但未收到回执：在「正在处理」分组，不在「最近完成」。"""
    item = _seed_item(db, user_a["user_id"], pipeline_state="ready", bundle_revision=1)
    _add_bundle(db, item, 1, processing_state="ready")
    db.commit()
    r = wc.get("/v1/items?view=working", headers=auth(user_a["desktop"]["token"])).json()
    assert item.id in [it["item_id"] for it in r["items"]]
    r = wc.get("/v1/items?view=published", headers=auth(user_a["desktop"]["token"])).json()
    assert r["items"] == []


def test_search_is_server_side(wc, user_a, db):
    _seed_item(db, user_a["user_id"], pipeline_state="ready",
               meta={"title": "强化学习入门", "original_url": "https://example.com/rl"})
    _seed_item(db, user_a["user_id"], pipeline_state="ready",
               meta={"title": "做饭笔记"})
    token = user_a["desktop"]["token"]
    db.commit()
    r = wc.get("/v1/items?search=" + "强化学习", headers=auth(token)).json()
    assert r["total"] == 1
    assert r["items"][0]["title"] == "强化学习入门"
    r = wc.get("/v1/items?search=" + "不存在的关键词", headers=auth(token)).json()
    assert r["total"] == 0


def test_workflow_attached_to_list_and_reading(wc, user_a, db):
    item = _seed_item(db, user_a["user_id"], pipeline_state="ready")
    _add_bundle(db, item, 1, processing_state="ready")
    db.commit()
    r = wc.get("/v1/items", headers=auth(user_a["desktop"]["token"])).json()
    assert r["items"][0]["workflow"]["version"] == "1.0"
    r = wc.get(f"/v1/items/{item.id}/reading", headers=auth(user_a["desktop"]["token"]))
    assert r.status_code == 200
    assert r.json()["item"]["workflow"]["delivery"]["status"] == "waiting_obsidian"


# ---- 处理记录（docs/17 §10.3）----

def test_diagnostics_three_layer_structure(wc, user_a, db):
    item = _seed_item(db, user_a["user_id"], pipeline_state="waiting_key",
                      state_detail="未配置模型凭据：配置后自动继续；原始材料已保存。")
    db.commit()
    r = wc.get(f"/v1/items/{item.id}/diagnostics", headers=auth(user_a["desktop"]["token"]))
    assert r.status_code == 200
    body = r.json()
    assert len(body["summary"]) == 3
    # 非 ASR 条目不渲染语音识别节点，摘要只有三步
    assert {s["stage"] for s in body["summary"]} == {"extract", "organize", "publish"}
    assert "内容版本" in body["explanation"]
    tech = body["technical_records"]
    assert tech["content_version"] == 1
    assert tech["content_version_label"] == "内容版本 1"
    assert tech["pipeline_state"] == "waiting_key"


# ---- 初始化接口（docs/17 §10.6）----

def test_onboarding_endpoints(wc, user_a, db):
    token = user_a["desktop"]["token"]
    r = wc.get("/v1/onboarding", headers=auth(token))
    assert r.status_code == 200
    ob = r.json()
    assert ob["completed"] is False        # 没有模型凭据
    assert ob["model"]["completed"] is False
    assert ob["obsidian"]["completed"] is True   # user_a 有桌面设备
    assert ob["platforms"]["optional"] is True
    r = wc.patch("/v1/onboarding", headers=auth(token), json={"dismiss": True})
    assert r.status_code == 200
    assert wc.get("/v1/onboarding", headers=auth(token)).json()["dismissed"] is True
    r = wc.patch("/v1/onboarding", headers=auth(token), json={"reopen": True})
    assert wc.get("/v1/onboarding", headers=auth(token)).json()["dismissed"] is False


def test_devices_summary(wc, user_a, db):
    r = wc.get("/v1/devices/summary", headers=auth(user_a["desktop"]["token"]))
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is True
    assert body["active_device"]["device_id"] == user_a["desktop"]["device_id"]
