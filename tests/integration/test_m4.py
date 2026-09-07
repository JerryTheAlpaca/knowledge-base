"""M4 来源适配器集成测试。

覆盖：字幕格式解析（B 站 JSON/SRT/VTT）、异常时间警告、B 站提取器
（mock 网络：分 P 选择、无轨 needs_input、分 P 越界、未指定分 P 说明、
短链展开）、上传字幕文件不触网（A19）、网络错误走任务退避而非 needs_input。

B 站接口响应用脱敏合成数据，不包含真实 Cookie/Key/私人内容。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tests.conftest import auth

from kbserver.extractors import bilibili as bili
from kbserver.extractors import subtitles as subfmt
from kbserver.security.safe_fetch import FetchResult, SafeFetchError


# ---- 字幕格式解析单元测试 ----

def test_parse_platform_json_and_normalize():
    records = subfmt.parse_platform_json({
        "body": [
            {"from": 0.0, "to": 2.5, "content": "第一句。"},
            {"from": 125.4, "to": 129.8, "content": "第二句。"},
        ]
    })
    segments, warnings = subfmt.normalize_records(records, source="platform_subtitle", video_duration_s=300.0)
    assert warnings == []
    assert [s["segment_id"] for s in segments] == ["s0001", "s0002"]
    assert segments[1]["start_ms"] == 125400
    assert segments[1]["end_ms"] == 129800
    assert segments[1]["original_record_index"] == 1
    assert segments[1]["source"] == "platform_subtitle"


def test_normalize_records_anomaly_warnings():
    records = [
        {"start_s": -1.0, "end_s": 1.0, "text": "负时间。"},
        {"start_s": 5.0, "end_s": 4.0, "text": "结束早于开始。"},
        {"start_s": 10.0, "end_s": 11.0, "text": ""},  # 空正文 → 跳过
        {"start_s": 999.0, "end_s": 1000.0, "text": "超出时长。"},
    ]
    segments, warnings = subfmt.normalize_records(records, source="platform_subtitle", video_duration_s=60.0)
    texts = [s["text"] for s in segments]
    assert "负时间。" in texts and "结束早于开始。" in texts and "超出时长。" in texts
    assert len(warnings) >= 3
    # 段落 ID 连续重排，不因跳过而断号
    assert [s["segment_id"] for s in segments] == ["s0001", "s0002", "s0003"]
    # 负起始时间被钳到 0
    assert segments[0]["start_ms"] == 0


def test_parse_srt_and_vtt():
    srt = (
        "1\n00:00:01,000 --> 00:00:03,500\n第一句话\n多行合并。\n\n"
        "2\n00:00:04,000 --> 00:00:06,000\n第二句话。\n"
    )
    records = subfmt.parse_srt(srt)
    assert records[0]["start_s"] == 1.0
    assert records[0]["text"] == "第一句话 多行合并。"
    assert records[1]["end_s"] == 6.0

    vtt = (
        "WEBVTT\n\n"
        "00:01.000 --> 00:03.500\nVTT 第一句。\n\n"
        "NOTE 注释块\n\n"
        "00:04.000 --> 00:06.000\nVTT 第二句。\n"
    )
    records_v = subfmt.parse_vtt(vtt)
    assert len(records_v) == 2
    assert records_v[0]["start_s"] == 1.0

    with pytest.raises(ValueError):
        subfmt.parse_srt("不是字幕内容")
    with pytest.raises(ValueError):
        subfmt.parse_srt("没有时间行\n也没有块")


def test_parse_any_sniffing_and_rendering():
    srt_bytes = "1\n00:00:01,000 --> 00:00:02,000\n嗅探。\n".encode("utf-8")
    records, kind = subfmt.parse_any(srt_bytes)
    assert kind == "srt"
    segments, _ = subfmt.normalize_records(records, source="tool_exported_srt")
    rendered = subfmt.segments_to_srt(segments)
    assert "00:00:01,000 --> 00:00:02,000" in rendered
    md = subfmt.segments_to_normalized_md(segments)
    assert md == "嗅探。 ^s0001\n"


def test_bilibili_json_structure_change_raises():
    with pytest.raises(ValueError):
        subfmt.parse_platform_json({"unexpected": []})
    with pytest.raises(ValueError):
        subfmt.parse_platform_json({"body": [{"from": "x", "to": 1.0, "content": "坏数据"}]})


# ---- B 站提取器集成测试（mock 网络） ----

BV = "BV1testBV000"
VIEW_URL = f"https://api.bilibili.com/x/web-interface/view?bvid={BV}"
SUBTITLE_BODY = {
    "body": [
        {"from": 1.0, "to": 3.0, "content": "P2 第一句。"},
        {"from": 3.5, "to": 6.0, "content": "P2 第二句。"},
    ]
}


def _view_doc(pages: list[dict]) -> dict:
    return {"code": 0, "data": {
        "bvid": BV, "title": "测试视频标题", "owner": {"name": "测试UP主"},
        "pubdate": 1700000000, "duration": 300, "pages": pages,
    }}


def _player_doc(tracks: list[dict]) -> dict:
    return {"code": 0, "data": {"subtitle": {"allow_submit": False, "subtitles": tracks}}}


TRACK_MANUAL = {
    "id": 988001, "lan": "zh-CN", "lan_doc": "中文（人工）",
    "ai_type": 0, "subtitle_url": "//aisubtitle.hdslb.com/track/988001.json",
}
TRACK_AI = {
    "id": 988002, "lan": "ai-zh", "lan_doc": "中文（自动）",
    "ai_type": 1, "subtitle_url": "//aisubtitle.hdslb.com/track/988002.json",
}


class FakeBiliNet:
    """按 URL 前缀路由的假 safe_fetch；记录调用以便断言。"""

    def __init__(self, *, view=None, players=None, subtitle=None, redirects=None, fail_urls=(),
                 require_cookie=False, cookie_value=None):
        # players: {"cid值": player_doc}; redirects: {"短链": "最终URL"}
        self.view = view or _view_doc([{"page": 1, "cid": 111, "part": "P1", "duration": 300}])
        self.players = players or {"111": _player_doc([])}
        self.subtitle = subtitle if subtitle is not None else SUBTITLE_BODY
        self.redirects = redirects or {}
        self.fail_urls = set(fail_urls)
        self.calls: list[str] = []
        # require_cookie=True 时，player 仅在 Cookie 头匹配 cookie_value 时返回轨道
        self.require_cookie = require_cookie
        self.cookie_value = cookie_value
        self.cookie_headers_seen: list = []

    def __call__(self, url, *, max_bytes=None, timeout=20.0, mime_prefixes=None, headers=None):
        self.calls.append(url)
        if url in self.fail_urls:
            raise SafeFetchError("NETWORK_ERROR", f"下载失败：{url}")
        for short, final in self.redirects.items():
            if url.startswith(short):
                return FetchResult(url=final, status_code=200, mime="text/html", content=b"")
        if "web-interface/view" in url:
            return FetchResult(url=url, status_code=200, mime="application/json",
                               content=json.dumps(self.view).encode("utf-8"))
        if "player/v2" in url:
            cid = url.split("cid=")[-1]
            if self.require_cookie:
                cookie = (headers or {}).get("Cookie")
                self.cookie_headers_seen.append(cookie)
                if cookie != self.cookie_value:
                    doc = {"code": 0, "data": {"subtitle": {"allow_submit": False, "subtitles": []}}}
                else:
                    doc = self.players.get(cid, _player_doc([]))
            else:
                doc = self.players.get(cid, _player_doc([]))
            return FetchResult(url=url, status_code=200, mime="application/json",
                               content=json.dumps(doc).encode("utf-8"))
        if "aisubtitle" in url or url.endswith(".json"):
            return FetchResult(url=url, status_code=200, mime="application/json",
                               content=json.dumps(self.subtitle).encode("utf-8"))
        raise SafeFetchError("SOURCE_BLOCKED", f"意外请求：{url}")


@pytest.fixture()
def bili_net(monkeypatch):
    def install(net: FakeBiliNet):
        monkeypatch.setattr(bili, "safe_fetch", net)
        monkeypatch.setattr(bili, "time", SimpleNamespace(sleep=lambda s: None))
        return net
    return install


def _capture_url(client, token, key: str, url: str | None = None, share: str | None = None,
                 upload_ids: list[str] | None = None):
    body = {
        "client_capture_id": f"{key}-1111-2222-3333-444444444444",
        "input_kind": "share" if share else "url",
        "original_url": url,
        "share_text": share,
    }
    if upload_ids:
        body["upload_ids"] = upload_ids
    return client.post(
        "/v1/captures", json=body,
        headers={**auth(token), "Idempotency-Key": key},
    )


def _drain(session_factory, max_rounds=20):
    from kbserver.workers import worker

    for _ in range(max_rounds):
        if not worker.run_once(session_factory):
            break


def _get_item(client, token, item_id):
    return client.get(f"/v1/items/{item_id}", headers=auth(token)).json()


def _manifest(client, token, item):
    return client.get(
        f"/v1/items/{item['item_id']}/bundles/{item['bundle_revision']}/manifest",
        headers=auth(token),
    ).json()


def test_bilibili_extract_p2_success(client, user_a, session_factory, bili_net):
    """A17：多 P 视频取 p=2 对应 cid 的字幕，文本与时间一致。"""
    net = bili_net(FakeBiliNet(
        view=_view_doc([
            {"page": 1, "cid": 111, "part": "第一集", "duration": 300},
            {"page": 2, "cid": 222, "part": "第二集", "duration": 240},
        ]),
        players={"222": _player_doc([TRACK_AI, TRACK_MANUAL])},
    ))
    c = _capture_url(client, user_a["phone"]["token"], "m4p2",
                     url=f"https://www.bilibili.com/video/{BV}/?p=2")
    assert c.status_code == 202
    item_id = c.json()["item_id"]
    _drain(session_factory)

    it = _get_item(client, user_a["desktop"]["token"], item_id)
    # extract 成功 → r2；enrich 无凭据 → waiting_key
    assert it["source_revision"] == 2
    assert it["pipeline_state"] == "waiting_key"
    m = _manifest(client, user_a["desktop"]["token"], it)
    assert m["source_revision"] == 2
    assert m["source"]["platform"] == "bilibili"
    assert m["source"]["title"] == "测试视频标题"
    assert m["source"]["author"] == "测试UP主"
    assert m["source"]["coverage"] == "full_text"
    paths = [f["relative_path"] for f in m["files"]]
    assert any(p.startswith(f"originals/bilibili/{BV}/P2/") and p.endswith(".json") for p in paths)
    assert "transcript.srt" in paths and "normalized.md" in paths and "segments.json" in paths
    # view + player + 字幕共 3 次请求，全部走受限下载器
    assert len(net.calls) == 3
    assert any("player/v2" in u and "cid=222" in u for u in net.calls)


def test_bilibili_extractor_track_selection(bili_net):
    """轨选择：中文人工轨优先于 ai-zh 自动轨；轨元数据不含临时下载地址（docs/04 §4.4）。"""
    bili_net(FakeBiliNet(
        view=_view_doc([
            {"page": 1, "cid": 111, "part": "第一集", "duration": 300},
            {"page": 2, "cid": 222, "part": "第二集", "duration": 240},
        ]),
        players={"222": _player_doc([TRACK_AI, TRACK_MANUAL])},
    ))
    ext = bili.extract(f"https://www.bilibili.com/video/{BV}/?p=2")
    assert len(ext.tracks) == 2
    assert all("subtitle_url" not in t and "download_url" not in t for t in ext.tracks)
    assert ext.track["track_id"] == "988001"
    assert ext.cid == "222"
    assert ext.part == 2
    assert ext.title == "测试视频标题"
    assert [s["text"] for s in ext.segments] == ["P2 第一句。", "P2 第二句。"]


def test_bilibili_no_track_needs_input(client, user_a, session_factory, bili_net):
    """匿名拿不到字幕轨：如实进入补充材料，不伪造全文（A18 方向）。"""
    bili_net(FakeBiliNet())  # 默认 player 返回空轨
    c = _capture_url(client, user_a["phone"]["token"], "m4notrack",
                     url=f"https://www.bilibili.com/video/{BV}/")
    item_id = c.json()["item_id"]
    _drain(session_factory)

    it = _get_item(client, user_a["desktop"]["token"], item_id)
    assert it["pipeline_state"] == "needs_input"
    assert it["source_revision"] == 1  # 没有新材料，不产生新来源版本
    assert "字幕轨" in (it.get("state_detail") or "")


def test_bilibili_view_tracks_login_required(client, user_a, session_factory, bili_net):
    """2026-09-07 实测行为：view API 列出轨道但匿名取不到内容 → 明确要求登录，不称“无字幕”。"""
    view = _view_doc([{"page": 1, "cid": 111, "part": "P1", "duration": 300}])
    view["data"]["subtitle"] = {"allow_submit": False, "list": [
        {"id": 42553898841407491, "lan": "zh-CN", "lan_doc": "中文（中国）", "ai_type": 0, "subtitle_url": ""},
        {"id": 35979932100722693, "lan": "en-US", "lan_doc": "English(US)", "ai_type": 0, "subtitle_url": ""},
    ]}
    bili_net(FakeBiliNet(view=view))  # player 仍为空轨
    c = _capture_url(client, user_a["phone"]["token"], "m4login",
                     url=f"https://www.bilibili.com/video/{BV}/")
    item_id = c.json()["item_id"]
    _drain(session_factory)

    it = _get_item(client, user_a["desktop"]["token"], item_id)
    assert it["pipeline_state"] == "needs_input"
    detail = it.get("state_detail") or ""
    assert "2 条字幕轨" in detail and "登录" in detail
    assert "中文（中国）" in detail


def test_bilibili_part_out_of_range(client, user_a, session_factory, bili_net):
    bili_net(FakeBiliNet())
    c = _capture_url(client, user_a["phone"]["token"], "m4p99",
                     url=f"https://www.bilibili.com/video/{BV}/?p=99")
    item_id = c.json()["item_id"]
    _drain(session_factory)
    it = _get_item(client, user_a["desktop"]["token"], item_id)
    assert it["pipeline_state"] == "needs_input"
    assert "99" in (it.get("state_detail") or "")


def test_bilibili_no_explicit_p_saves_p1_with_note(client, user_a, session_factory, bili_net):
    net = bili_net(FakeBiliNet(
        view=_view_doc([
            {"page": 1, "cid": 111, "part": "P1", "duration": 300},
            {"page": 2, "cid": 222, "part": "P2", "duration": 240},
        ]),
        players={"111": _player_doc([TRACK_MANUAL])},
    ))
    c = _capture_url(client, user_a["phone"]["token"], "m4nop",
                     url=f"https://www.bilibili.com/video/{BV}/")
    item_id = c.json()["item_id"]
    _drain(session_factory)

    it = _get_item(client, user_a["desktop"]["token"], item_id)
    assert it["source_revision"] == 2
    assert it["pipeline_state"] == "waiting_key"
    m = _manifest(client, user_a["desktop"]["token"], it)
    assert m["source"]["source_locator"]["cid"] == "111"
    assert any("未指定分 P" in w for w in m["warnings"])
    assert any("aisubtitle" in u for u in net.calls)  # 字幕按轨下载


def test_bilibili_share_text_short_link(client, user_a, session_factory, bili_net):
    """分享文字中的 b23.tv 短链：展开后按最终 URL 处理，原分享文字留在 capture.json。"""
    bili_net(FakeBiliNet(
        players={"111": _player_doc([TRACK_MANUAL])},
        redirects={"https://b23.tv/abc": f"https://www.bilibili.com/video/{BV}/?p=1&share_source=share"},
    ))
    share = f"【测试视频标题】 https://b23.tv/abc 快来保存！"
    c = _capture_url(client, user_a["phone"]["token"], "m4short", share=share)
    item_id = c.json()["item_id"]
    _drain(session_factory)

    it = _get_item(client, user_a["desktop"]["token"], item_id)
    assert it["source_revision"] == 2
    assert it["pipeline_state"] == "waiting_key"
    m = _manifest(client, user_a["desktop"]["token"], it)
    # 分享文字不冒充正文：segments 来自字幕而非 share_text
    assert any(f["relative_path"] == "capture.json" for f in m["files"])
    paths = [f["relative_path"] for f in m["files"]]
    assert "originals/bilibili/BV1testBV000/P1/" in " ".join(paths) or any(
        p.startswith("originals/bilibili/") for p in paths
    )


def test_subtitle_upload_srt_skips_network(client, user_a, session_factory, bili_net, monkeypatch):
    """A19：上传 SRT，源网站不可访问也能完成加工；全程不访问 B 站。"""
    def _no_network(*args, **kwargs):
        raise AssertionError("上传字幕路径不应访问网络")
    monkeypatch.setattr(bili, "safe_fetch", _no_network)
    monkeypatch.setattr(bili, "time", SimpleNamespace(sleep=lambda s: None))

    srt = "1\n00:00:01,000 --> 00:00:03,000\n上传的第一句。\n\n2\n00:00:03,500 --> 00:00:05,000\n上传的第二句。\n"
    up = client.post(
        "/v1/uploads",
        files={"file": ("lecture.srt", srt.encode("utf-8"), "application/x-subrip")},
        headers={**auth(user_a["phone"]["token"]), "Idempotency-Key": "m4srt-up"},
    ).json()
    c = _capture_url(client, user_a["phone"]["token"], "m4srt",
                     url="https://www.bilibili.com/video/WRONGBVID123/",
                     upload_ids=[up["upload_id"]])
    item_id = c.json()["item_id"]
    _drain(session_factory)

    it = _get_item(client, user_a["desktop"]["token"], item_id)
    assert it["source_revision"] == 2
    assert it["pipeline_state"] == "waiting_key"
    m = _manifest(client, user_a["desktop"]["token"], it)
    assert m["source"]["coverage"] == "full_text"
    paths = [f["relative_path"] for f in m["files"]]
    assert "segments.json" in paths and "normalized.md" in paths


def test_bilibili_network_error_retries_not_needs_input(client, user_a, session_factory, bili_net):
    """网络失败与无字幕区分：进入任务退避重试，不直接判 needs_input。"""
    bili_net(FakeBiliNet(fail_urls={VIEW_URL}))
    c = _capture_url(client, user_a["phone"]["token"], "m4net",
                     url=f"https://www.bilibili.com/video/{BV}/")
    item_id = c.json()["item_id"]
    _drain(session_factory, max_rounds=3)

    it = _get_item(client, user_a["desktop"]["token"], item_id)
    assert it["pipeline_state"] in ("extracting", "retry_wait", "queued")
    assert it["pipeline_state"] != "needs_input"


# ---- B 站登录态托管 ----

def test_extract_sessdata_parsing():
    """SESSDATA 提取：裸值 / 前缀 / 完整 Cookie；非法输入拒绝。"""
    from kbserver.api.routes_bilibili import _extract_sessdata

    assert _extract_sessdata("abcDEF12345678901234%2C") == "abcDEF12345678901234%2C"
    assert _extract_sessdata("SESSDATA=abcDEF12345678901234%2C") == "abcDEF12345678901234%2C"
    assert _extract_sessdata("buvid3=x; SESSDATA=abcDEF12345678901234%2C; bili_jct=y") == "abcDEF12345678901234%2C"
    import pytest

    for bad in ("", "太短", "a=b=c", "x; y", "有 空格的值不是sessdata!"):
        with pytest.raises(Exception):
            _extract_sessdata(bad)


def test_bilibili_session_api_lifecycle(client, user_a):
    """SESSDATA 只进不出：明文永不回读；轮换产生新版本；可撤销。"""
    token = user_a["desktop"]["token"]

    r0 = client.get("/v1/bilibili-session", headers=auth(token))
    assert r0.status_code == 200 and r0.json()["configured"] is False

    # 裸值
    p1 = client.put("/v1/bilibili-session", json={"secret": "fake-sessdata-value-1234567890"},
                    headers=auth(token))
    assert p1.status_code == 200
    out = p1.json()
    assert out["configured"] is True and out["credential_version"] == 1
    assert "fake-sessdata" not in json.dumps(out)  # 明文不回读

    # 完整 Cookie 粘贴：只提取 SESSDATA，轮换为 v2
    p2 = client.put("/v1/bilibili-session", json={
        "secret": "buvid3=xyz; SESSDATA=fake-sessdata-rotated-9876543210; bili_jct=abc",
    }, headers=auth(token))
    assert p2.status_code == 200 and p2.json()["credential_version"] == 2
    assert "fake-sessdata-rotated" not in json.dumps(p2.json())

    # 非法输入
    bad = client.put("/v1/bilibili-session", json={"secret": "短"}, headers=auth(token))
    assert bad.status_code == 422
    bad2 = client.put("/v1/bilibili-session", json={"secret": "not a sessdata value with spaces"},
                      headers=auth(token))
    assert bad2.status_code == 422

    # profiles 列表中可见（kind=bilibili_session），仍不回读明文
    listed = client.get("/v1/provider-profiles", headers=auth(token)).json()
    kinds = [p["kind"] for p in listed]
    assert "bilibili_session" in kinds

    # 撤销
    d = client.delete("/v1/bilibili-session", headers=auth(token))
    assert d.status_code == 200
    r1 = client.get("/v1/bilibili-session", headers=auth(token))
    assert r1.json()["configured"] is False


def test_bilibili_extract_with_login_state(client, user_a, session_factory, bili_net):
    """托管登录态后，player 需要登录的视频自动取得字幕；请求带最小凭据 Cookie。"""
    net = bili_net(FakeBiliNet(
        players={"111": _player_doc([TRACK_MANUAL])},
        require_cookie=True,
        cookie_value="SESSDATA=fake-sessdata-value-123456",
    ))
    token = user_a["desktop"]["token"]

    # 未托管登录态：先降级为 needs_input
    c = _capture_url(client, user_a["phone"]["token"], "m4login1",
                     url=f"https://www.bilibili.com/video/{BV}/")
    item_id = c.json()["item_id"]
    _drain(session_factory)
    assert _get_item(client, token, item_id)["pipeline_state"] == "needs_input"

    # 托管登录态 → needs_input 条目自动重新提取 → 成功
    put = client.put("/v1/bilibili-session", json={"secret": "fake-sessdata-value-123456"},
                     headers=auth(token))
    assert put.status_code == 200
    assert put.json()["requeued_items"] >= 1
    _drain(session_factory)

    it = _get_item(client, token, item_id)
    assert it["source_revision"] == 2
    assert it["pipeline_state"] == "waiting_key"
    # 托管后的 player 调用带最小凭据 Cookie（托管前的匿名调用 Cookie 为空）
    assert net.cookie_headers_seen
    assert any(h == "SESSDATA=fake-sessdata-value-123456" for h in net.cookie_headers_seen)
    assert all(h in (None, "SESSDATA=fake-sessdata-value-123456") for h in net.cookie_headers_seen)


def test_bilibili_session_invalid_needs_input(client, user_a, session_factory, bili_net):
    """登录凭据失效（-101）：明确提示更新登录态，而不是冒充匿名失败。"""
    # view 正常（真实场景 view 匿名可用），player 对该 cid 返回 -101
    dead_players = {"111": {"code": -101, "message": "账号未登录"}}
    bili_net(FakeBiliNet(players=dead_players))
    client.put("/v1/bilibili-session", json={"secret": "fake-sessdata-value-123456"},
               headers=auth(user_a["desktop"]["token"]))
    c = _capture_url(client, user_a["phone"]["token"], "m4dead",
                     url=f"https://www.bilibili.com/video/{BV}/")
    item_id = c.json()["item_id"]
    _drain(session_factory)

    it = _get_item(client, user_a["desktop"]["token"], item_id)
    assert it["pipeline_state"] == "needs_input"
    assert "失效" in (it.get("state_detail") or "")
