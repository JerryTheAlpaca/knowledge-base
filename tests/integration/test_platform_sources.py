"""新平台来源底座测试（docs/18 P1）：知乎/视频号识别、标签、分发不串入普通网页。

- 视频号与公众号是两个来源，不因都来自微信而合并（docs/18 §6）。
- 知乎/小红书/视频号链接不按普通网页处理，分享文字不作正文；
  适配器在登录墙/风控/播放壳时清楚降级进入补充材料（docs/18 §7.1、§7.3-7.5）。
"""
from __future__ import annotations

import json

import pytest

from kbserver.domain import pipeline, source_labels
from kbserver.domain.platforms import guess_platform
from kbserver.extractors import fetch_base
from kbserver.workers import worker

from tests.conftest import auth
from tests.integration.test_m4_web import FakeWebNet


# ---- 假网络（三平台适配器共用出口：fetch_base.safe_fetch）----

ZHIHU_CHALLENGE_HTML = (
    '<!DOCTYPE html><html lang="en"><head><meta id="zh-zse-ck" charset="UTF-8" '
    'content="fake-zse-payload"></head><body><div>知乎</div></body></html>'
)
CHANNELS_SHELL_HTML = (
    '<html><head><title>视频号</title></head><body>'
    '<p>请在微信客户端打开链接。</p></body></html>'
)
XHS_SEC_HTML = (
    '<html><body><a href="https://www.xiaohongshu.com/404/sec_fake?'
    'error_code=300031&amp;error_msg=%E5%BD%93%E5%89%8D%E7%AC%94%E8%AE%B0%E6%9A%82%E6%97%B6%E6%97%A0%E6%B3%95%E6%B5%8F%E8%A7%88">'
    'Found</a></body></html>'
)


@pytest.fixture()
def platform_net(monkeypatch):
    def install(net: FakeWebNet):
        monkeypatch.setattr(fetch_base, "safe_fetch", net)
        return net
    return install


# ---- 平台识别 ----

def test_guess_platform_new_sources():
    assert guess_platform("https://www.zhihu.com/question/1/answer/2") == "zhihu"
    assert guess_platform("https://zhuanlan.zhihu.com/p/123456") == "zhihu"
    assert guess_platform("https://channels.weixin.qq.com/web/feed/abc") == "wechat_channels"
    # sph 短链（P0 真实样本确认的形态，docs/18 §7.4）
    assert guess_platform("https://weixin.qq.com/sph/AXMl5KcmD0") == "wechat_channels"
    # 既有平台不受影响；公众号不并入视频号
    assert guess_platform("https://mp.weixin.qq.com/s/x") == "wechat_mp"
    assert guess_platform("https://www.xiaohongshu.com/explore/1") == "xiaohongshu"
    assert guess_platform("https://xhslink.com/abc123") == "xiaohongshu"
    # xhslink.cn：2026-09-17 真实分享链接确认的新短链域
    assert guess_platform("https://xhslink.cn/o/6ePcmGn1WVH") == "xiaohongshu"
    assert guess_platform("https://example.com/post") == "web"


# ---- 来源标签 ----

def test_source_fields_new_sources():
    zhihu = source_labels.source_fields("zhihu", "text")
    assert (zhihu["source_type"], zhihu["source_label"], zhihu["icon_key"]) == ("zhihu", "知乎", "zhihu")
    ch = source_labels.source_fields("wechat_channels", "video")
    assert (ch["source_type"], ch["source_label"], ch["platform"]) == (
        "wechat_channels", "微信视频号", "wechat_channels")
    ch_text = source_labels.source_fields("wechat_channels", "text")
    assert ch_text["source_type"] == "wechat_channels"
    # 视频号与公众号不合并
    wx = source_labels.source_fields("wechat_mp", "text")
    assert ch["source_type"] != wx["source_type"]
    assert ch["source_label"] != wx["source_label"]
    # 默认 media_kind：视频号是视频，知乎是文字
    assert source_labels.default_media_kind("wechat_channels") == "video"
    assert source_labels.default_media_kind("zhihu") == "text"
    # 标签是纯文字（用户要求不带表情符号）
    for f in (zhihu, ch):
        assert f["source_label"] == f["source_label"].strip()


# ---- 采集平台判定 ----

def test_capture_platform_routes_new_sources():
    assert pipeline._capture_platform(
        {"original_url": "https://www.zhihu.com/question/1/answer/2"}) == ("zhihu", "text")
    assert pipeline._capture_platform(
        {"original_url": "https://channels.weixin.qq.com/web/feed/abc"}) == ("wechat_channels", "video")
    # 音频转写意图下视频号链接也是网页音频语义，但平台不丢
    assert pipeline._capture_platform(
        {"processing_intent": "transcribe_audio",
         "original_url": "https://channels.weixin.qq.com/web/feed/abc"}) == ("wechat_channels", "audio")


# ---- 分发边界 ----

def test_webpage_target_excludes_special_platforms():
    """知乎/小红书/视频号/B 站都不按普通网页提取。"""
    for url in ("https://www.zhihu.com/question/1/answer/2",
                "https://zhuanlan.zhihu.com/p/1",
                "https://channels.weixin.qq.com/web/feed/abc",
                "https://www.xiaohongshu.com/explore/1",
                "https://xhslink.com/abc",
                "https://www.bilibili.com/video/BV1x"):
        assert worker._webpage_target({"original_url": url}) is None, url
    assert worker._webpage_target({"original_url": "https://example.com/post"}) == "https://example.com/post"


def test_special_platform_detection():
    assert worker._special_platform({"original_url": ""}, {"platform": "zhihu"}) == "zhihu"
    # meta 缺失时按 URL 推断（含短链）
    assert worker._special_platform(
        {"original_url": "https://xhslink.com/abc"}, {}) == "xiaohongshu"
    assert worker._special_platform(
        {"original_url": "https://weixin.qq.com/sph/AXMl5KcmD0"}, {}) == "wechat_channels"
    # 分享文字里的链接也能识别
    assert worker._special_platform(
        {"share_text": "看这个 https://channels.weixin.qq.com/web/feed/abc"}, {}) == "wechat_channels"
    assert worker._special_platform(
        {"share_text": "好视频 https://weixin.qq.com/sph/AXMl5KcmD0"}, {}) == "wechat_channels"
    # 普通网页与公众号不是专用平台
    assert worker._special_platform({"original_url": "https://example.com"}, {}) is None
    assert worker._special_platform(
        {"original_url": "https://mp.weixin.qq.com/s/x"}, {}) is None


# ---- 端到端：采集 → extract → 清楚降级 ----

def _capture_url(client, token, key: str, url: str | None = None, share: str | None = None):
    body = {
        "client_capture_id": f"{key}-1111-2222-3333-444444444444",
        "input_kind": "share" if share else "url",
        "original_url": url,
        "share_text": share,
    }
    return client.post("/v1/captures", json=body,
                       headers={**auth(token), "Idempotency-Key": key})


def _drain(session_factory, max_rounds: int = 20) -> None:
    for _ in range(max_rounds):
        if not worker.run_once(session_factory):
            break


def _item(client, token, item_id):
    return client.get(f"/v1/items/{item_id}", headers=auth(token)).json()


def test_zhihu_capture_degrades_cleanly(client, user_a, session_factory, platform_net):
    """知乎链接采集：来源分类正确；匿名被 zse 挑战壳拦截时如实进入补充材料。"""
    net = platform_net(FakeWebNet(pages={
        "https://www.zhihu.com/question/1/answer/2": (403, "text/html", ZHIHU_CHALLENGE_HTML.encode("utf-8")),
    }))
    c = _capture_url(client, user_a["phone"]["token"], "p1zhihu",
                     url="https://www.zhihu.com/question/1/answer/2")
    assert c.status_code == 202
    item_id = c.json()["item_id"]
    _drain(session_factory)

    it = _item(client, user_a["desktop"]["token"], item_id)
    assert it["pipeline_state"] == "needs_input"
    assert it["source_revision"] == 1  # 没有新材料，不产生新来源版本
    assert "知乎" in (it.get("state_detail") or "")
    assert net.calls == ["https://www.zhihu.com/question/1/answer/2"]  # 无会话不重试
    m = client.get(
        f"/v1/items/{item_id}/bundles/{it['bundle_revision']}/manifest",
        headers=auth(user_a["desktop"]["token"]),
    ).json()
    assert m["source"]["platform"] == "zhihu"
    assert m["source"]["source_type"] == "zhihu"
    assert m["source"]["source_label"] == "知乎"
    assert m["source"]["media_kind"] == "text"


def test_wechat_channels_capture_degrades_cleanly(client, user_a, session_factory, platform_net):
    """视频号链接采集：来源是 wechat_channels 而非公众号；播放壳如实降级。"""
    net = platform_net(FakeWebNet(pages={
        "https://channels.weixin.qq.com/web/feed/abc": (200, "text/html", CHANNELS_SHELL_HTML.encode("utf-8")),
    }))
    c = _capture_url(client, user_a["phone"]["token"], "p1channels",
                     url="https://channels.weixin.qq.com/web/feed/abc",
                     share="这个视频太好了 https://channels.weixin.qq.com/web/feed/abc")
    assert c.status_code == 202
    item_id = c.json()["item_id"]
    _drain(session_factory)

    it = _item(client, user_a["desktop"]["token"], item_id)
    assert it["pipeline_state"] == "needs_input"
    assert "视频号" in (it.get("state_detail") or "")
    m = client.get(
        f"/v1/items/{item_id}/bundles/{it['bundle_revision']}/manifest",
        headers=auth(user_a["desktop"]["token"]),
    ).json()
    assert m["source"]["platform"] == "wechat_channels"
    assert m["source"]["source_label"] == "微信视频号"
    assert m["source"]["media_kind"] == "video"


def test_xiaohongshu_share_text_not_used_as_body(client, user_a, session_factory, platform_net):
    """小红书链接+分享文字：分享文字只是标题+链接摘要，不作正文；sec 挑战如实降级。"""
    net = platform_net(FakeWebNet(pages={
        "https://xhslink.com/abc123": (200, "text/html", XHS_SEC_HTML.encode("utf-8")),
    }))
    c = _capture_url(client, user_a["phone"]["token"], "p1xhs",
                     url="https://xhslink.com/abc123",
                     share="超好用的探店笔记 https://xhslink.com/abc123")
    assert c.status_code == 202
    item_id = c.json()["item_id"]
    _drain(session_factory)

    it = _item(client, user_a["desktop"]["token"], item_id)
    assert it["pipeline_state"] == "needs_input"
    assert "小红书" in (it.get("state_detail") or "")
    m = client.get(
        f"/v1/items/{item_id}/bundles/{it['bundle_revision']}/manifest",
        headers=auth(user_a["desktop"]["token"]),
    ).json()
    assert m["source"]["platform"] == "xiaohongshu"
    assert m["source"]["source_label"] == "小红书"


# ---- 小红书 xhslink.cn 短链 + 登录墙会话重试（2026-09-17 P0 真实样本）----

XHS_SHORT_URL = "https://xhslink.cn/o/6ePcmGn1WVH"
XHS_NOTE_ID = "6aaa31f80000000011036a1c"
XHS_NOTE_URL = (
    f"https://www.xiaohongshu.com/discovery/item/{XHS_NOTE_ID}"
    "?app_platform=ios&xsec_source=app_share&type=normal"
    "&xsec_token=CBs-1rVoPl58lRk41e3WLW8Y0FaezMsEJEoQrodqa13ak%3D&author_share=1"
)
# 真实形态：短链 302 → /login?redirectPath=<编码的笔记地址>
XHS_LOGIN_URL = (
    "https://www.xiaohongshu.com/login?redirectPath="
    "http%3A%2F%2Fwww.xiaohongshu.com%2Fdiscovery%2Fitem%2F6aaa31f80000000011036a1c"
    "%3Fxsec_token%3DCBs-1rVoPl58lRk41e3WLW8Y0FaezMsEJEoQrodqa13ak%3D"
    "%26xsec_source%3Dapp_share%26author_share%3D1"
)


# 登录页 redirectPath 解码后的真实笔记地址（适配器实际用来重试的 URL）
XHS_DECODED_NOTE_URL = (
    "http://www.xiaohongshu.com/discovery/item/6aaa31f80000000011036a1c"
    "?xsec_token=CBs-1rVoPl58lRk41e3WLW8Y0FaezMsEJEoQrodqa13ak="
    "&xsec_source=app_share&author_share=1"
)


def test_xhs_note_url_from_login():
    """登录页 redirectPath 解码出真实笔记地址，xsec_token 尾随 = 完整保留。"""
    from kbserver.extractors.xiaohongshu import _note_url_from_login
    note = _note_url_from_login(XHS_LOGIN_URL)
    assert note == XHS_DECODED_NOTE_URL
    # 非登录页不解析
    assert _note_url_from_login("https://www.xiaohongshu.com/explore/1") is None


def _xhs_note_state_html(note_id: str) -> bytes:
    state = {
        "note": {"noteDetailMap": {note_id: {"note": {
            "title": "南方医科大学学生疑坠亡同学发声",
            "desc": "9月16日，在社交平台发布相关内容（集成测试正文）。",
            "type": "normal",
            "time": 1726927148000,
            "user": {"nickname": "测试作者"},
            "noteCard": {"type": "normal",
                         "imageList": [{"url": "https://sns-img.example/1.jpg"}]},
        }}}},
    }
    return (
        "<html><head><script>window.__INITIAL_STATE__="
        + json.dumps(state, ensure_ascii=False)
        + "</script></head><body></body></html>"
    ).encode("utf-8")


def test_xiaohongshu_short_link_retries_with_session(client, user_a, session_factory,
                                                     platform_net):
    """xhslink.cn 短链：登录墙 → 从 redirectPath 解出真实地址 → 托管会话重试成功。"""
    net = platform_net(FakeWebNet(pages={
        # 短链展开落在登录页（4 元组模拟重定向后的最终 URL）
        XHS_SHORT_URL: (200, "text/html", "<html>请完成登录后继续</html>".encode("utf-8"), XHS_LOGIN_URL),
        # 解码出的真实笔记地址 + 登录态 → 正常返回笔记页
        XHS_DECODED_NOTE_URL: (200, "text/html", _xhs_note_state_html(XHS_NOTE_ID)),
    }))
    r = client.put("/v1/platform-sessions/xiaohongshu",
                   json={"secret": "a1=abc123456; web_session=0a1b2c3d4e5f;"},
                   headers=auth(user_a["desktop"]["token"]))
    assert r.status_code in (200, 201), r.text

    c = _capture_url(client, user_a["phone"]["token"], "p1xhslogin",
                     url=XHS_SHORT_URL,
                     share="南方医科大学学生疑坠亡同学发声 https://xhslink.cn/o/6ePcmGn1WVH 前往【小红书】一探究竟吧！")
    assert c.status_code == 202
    item_id = c.json()["item_id"]
    _drain(session_factory)

    it = _item(client, user_a["desktop"]["token"], item_id)
    assert it["pipeline_state"] in ("extracted", "enriching", "ready", "waiting_key"), it
    # 抓取序列：短链（登录页）→ 带会话重试真实笔记地址
    assert net.calls == [XHS_SHORT_URL, XHS_DECODED_NOTE_URL]

    m = client.get(
        f"/v1/items/{item_id}/bundles/{it['bundle_revision']}/manifest",
        headers=auth(user_a["desktop"]["token"]),
    ).json()
    src = m["source"]
    assert src["platform"] == "xiaohongshu"
    assert src["author"] == "测试作者"
    assert src["title"] == "南方医科大学学生疑坠亡同学发声"
    assert src["published_at"] == "2024-09-21T13:59:08+00:00"
    assert src["source_locator"]["content_id"] == XHS_NOTE_ID
    assert src["canonical_url"] == XHS_DECODED_NOTE_URL  # 保留 xsec_token 的分享授权地址
    assert any("图片" in s for s in m["missing_materials"]) is False
    reading = client.get(f"/v1/items/{item_id}/reading",
                         headers=auth(user_a["desktop"]["token"])).json()
    assert "集成测试正文" in json.dumps(reading, ensure_ascii=False)


def test_user_supplement_still_wins_for_special_platforms(client, user_a, session_factory):
    """专用平台条目补充正文后仍走用户正文路径（正文补充优先，docs/18 §7.1）。"""
    c = _capture_url(client, user_a["phone"]["token"], "p1zhisupp",
                     url="https://www.zhihu.com/question/1/answer/2")
    item_id = c.json()["item_id"]
    _drain(session_factory)
    assert _item(client, user_a["desktop"]["token"], item_id)["pipeline_state"] == "needs_input"

    # 补充正文 → 重新提取产生新版本
    r = client.post(
        f"/v1/items/{item_id}/source-text",
        json={"expected_source_revision": 1, "text": "这是用户粘贴的知乎回答正文。"},
        headers=auth(user_a["desktop"]["token"]),
    )
    assert r.status_code == 202, r.text
    _drain(session_factory)
    it = _item(client, user_a["desktop"]["token"], item_id)
    assert it["source_revision"] >= 2
    assert it["pipeline_state"] in ("extracted", "enriching", "ready", "waiting_key")


# ---- 视频号 sph 链接：公开元数据接口成功路径（docs/18 §7.4 P0 样本）----

CHANNELS_PREVIEW_URL = "https://channels.weixin.qq.com/finder-preview/pages/sph?id=AXMl5KcmD0"
CHANNELS_PREVIEW_HTML = (
    '<html><head><title>视频号</title></head><body><div id="app"></div></body></html>'
)
# 2026-09-17 真实分享链接（公开视频号内容）的接口返回结构 fixture
FEED_API_OK = {
    "data": {
        "authorInfo": {"nickname": "小楠探秘社", "headImgUrl": "https://example/head.jpg"},
        "feedInfo": {
            "description": "【黄执中vs熊浩】--決定相伴一生的爱侣，要不要一起打上永远爱对方的“思想钢印”？#辩论赛 #华语辩坛老友赛 #新国辩",
            "createtime": 1726927148,
            "coverUrl": "https://finder.video.qq.com/251/20304/stodownload?encfilekey=fake",
            "likeCountFmt": "1383",
        },
        "sceneInfo": {"dynamicExportId": "export/xyz", "expiredTime": 1789652376},
    },
    "errCode": 0,
    "errMsg": "",
}


def test_wechat_channels_sph_metadata_extracted(client, user_a, session_factory,
                                                platform_net, monkeypatch):
    """sph 链接：公开元数据接口取得作者/说明/发布时间，sph 短 id 作稳定内容 ID。"""
    net = platform_net(FakeWebNet(pages={
        CHANNELS_PREVIEW_URL: (200, "text/html", CHANNELS_PREVIEW_HTML.encode("utf-8")),
    }))
    api_calls = []

    def fake_post_json(url, *, body, referer=None, max_bytes=None, timeout=20.0):
        api_calls.append({"url": url, "body": body, "referer": referer})
        return FEED_API_OK

    monkeypatch.setattr(fetch_base, "post_json", fake_post_json)
    c = _capture_url(client, user_a["phone"]["token"], "p1sphmeta",
                     url=CHANNELS_PREVIEW_URL,
                     share="难得的辩论赛合集 https://weixin.qq.com/sph/AXMl5KcmD0")
    assert c.status_code == 202
    item_id = c.json()["item_id"]
    _drain(session_factory)

    it = _item(client, user_a["desktop"]["token"], item_id)
    assert it["pipeline_state"] in ("extracted", "enriching", "ready", "waiting_key"), it

    # 元数据接口被调用：URL/请求体/Referer 同源
    assert len(api_calls) == 1
    call = api_calls[0]
    assert call["url"] == ("https://channels.weixin.qq.com"
                           "/finder-preview/api/feed/get_feed_info")
    assert call["body"]["shortUri"] == "AXMl5KcmD0"
    assert call["referer"] == CHANNELS_PREVIEW_URL
    assert net.calls == [CHANNELS_PREVIEW_URL]  # 页面只抓一次

    m = client.get(
        f"/v1/items/{item_id}/bundles/{it['bundle_revision']}/manifest",
        headers=auth(user_a["desktop"]["token"]),
    ).json()
    src = m["source"]
    assert src["platform"] == "wechat_channels"
    assert src["source_label"] == "微信视频号"
    assert src["media_kind"] == "video"
    assert src["coverage"] == "metadata_only"
    assert src["author"] == "小楠探秘社"
    assert src["published_at"] == "2024-09-21T13:59:08+00:00"
    assert src["source_locator"]["content_id"] == "AXMl5KcmD0"
    # 说明文字进入规范正文（阅读层），且缺失清单标明原视频未保存
    reading = client.get(f"/v1/items/{item_id}/reading",
                         headers=auth(user_a["desktop"]["token"])).json()
    assert "思想钢印" in json.dumps(reading, ensure_ascii=False)
    assert any("原视频" in s for s in m["missing_materials"])


def test_wechat_channels_api_failure_falls_back_to_shell(client, user_a, session_factory,
                                                         platform_net, monkeypatch):
    """sph 链接元数据接口失败：如实回落播放壳语义，不伪造正文。"""
    platform_net(FakeWebNet(pages={
        CHANNELS_PREVIEW_URL: (200, "text/html", CHANNELS_PREVIEW_HTML.encode("utf-8")),
    }))

    def fake_post_json(url, *, body, referer=None, max_bytes=None, timeout=20.0):
        from kbserver.extractors.fetch_base import PlatformError
        raise PlatformError("blocked", "接口请求被拒绝：响应状态码 HTTP 401")

    monkeypatch.setattr(fetch_base, "post_json", fake_post_json)
    c = _capture_url(client, user_a["phone"]["token"], "p1sphfail",
                     url=CHANNELS_PREVIEW_URL)
    assert c.status_code == 202
    item_id = c.json()["item_id"]
    _drain(session_factory)

    it = _item(client, user_a["desktop"]["token"], item_id)
    # 播放壳成功归档（链接已留存），coverage 仍是 metadata_only、无正文
    assert it["pipeline_state"] in ("extracted", "enriching", "ready", "waiting_key"), it
    m = client.get(
        f"/v1/items/{item_id}/bundles/{it['bundle_revision']}/manifest",
        headers=auth(user_a["desktop"]["token"]),
    ).json()
    assert m["source"]["coverage"] == "metadata_only"
    assert m["source"]["source_locator"]["content_id"] == "AXMl5KcmD0"
    assert any("原视频" in s for s in m["missing_materials"])
