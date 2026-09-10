"""M4 网页/公众号适配器集成测试。

覆盖：公众号已知结构提取（标题/作者/发布时间/js_content）、普通网页
启发式正文聚类（排除导航/页脚）、分享文字不当正文、JS 空页与 403 进入
needs_input、网络错误走任务退避、正文图片限量下载与缺失清单（docs/02
§5.1 策略矩阵、§5.2 原始内容定义）。

页面响应用合成数据，不包含真实 Cookie/Key/私人内容。
"""
from __future__ import annotations

import pytest

from tests.conftest import auth

from kbserver.extractors import webpages
from kbserver.security.safe_fetch import FetchResult, SafeFetchError


# ---- 假网络 ----

class FakeWebNet:
    """按 URL 路由的假 safe_fetch；记录调用以便断言。"""

    def __init__(self, pages: dict[str, tuple[int, str, bytes]] | None = None,
                 images: dict[str, tuple[bytes, str] | Exception] | None = None,
                 fail_urls: tuple[str, ...] = ()):
        self.pages = pages or {}
        self.images = images or {}
        self.fail_urls = set(fail_urls)
        self.calls: list[str] = []

    def __call__(self, url, *, max_bytes=None, timeout=20.0, mime_prefixes=None, headers=None):
        self.calls.append(url)
        if url in self.fail_urls:
            raise SafeFetchError("NETWORK_ERROR", f"下载失败：{url}")
        if url in self.pages:
            status, mime, content = self.pages[url]
            return FetchResult(url=url, status_code=status, mime=mime, content=content)
        if url in self.images:
            v = self.images[url]
            if isinstance(v, Exception):
                raise v
            content, mime = v
            return FetchResult(url=url, status_code=200, mime=mime, content=content)
        raise SafeFetchError("SOURCE_BLOCKED", f"意外请求：{url}")


@pytest.fixture()
def web_net(monkeypatch):
    def install(net: FakeWebNet):
        monkeypatch.setattr(webpages, "safe_fetch", net)
        # B 站模块也指向假网络，避免个别路径触网；正常用例不会走到它
        from kbserver.extractors import bilibili as bili
        monkeypatch.setattr(bili, "safe_fetch", net)
        return net
    return install


# ---- 合成页面 ----

WECHAT_URL = "https://mp.weixin.qq.com/s/AbCdEf123"
WECHAT_HTML = """<html><head><title>公众号文章</title></head><body>
<div class="rich_media">
<h1 id="activity-name">测试公众号文章标题</h1>
<a id="js_name">测试公众号</a>
<em id="publish_time">2026年09月01日 08:30</em>
<div id="js_content">
<p>这是第一段正文内容，介绍文章的主题与背景。</p>
<p>这是第二段正文，中间包含<img data-src="https://mmbiz.qpic.cn/img1.jpg">一张图片。</p>
<p>第三段收尾，正文到此结束。</p>
</div>
</div></body></html>""".encode("utf-8")

GENERIC_URL = "https://example.com/posts/hello"
GENERIC_HTML = """<html><head>
<meta property="og:title" content="普通网页标题">
<meta name="author" content="网页作者">
<meta property="article:published_time" content="2026-08-15T10:00:00+08:00">
</head><body>
<nav><p>首页</p><p>登录</p></nav>
<article>
<h2>普通网页标题</h2>
<p>正文第一段，长度足够参与聚类打分，用来验证容器选择。</p>
<p>正文第二段，同样来自正文容器，附带<img src="/img/cover.png">图片。</p>
<p>正文第三段，作为结尾段落存在。</p>
</article>
<footer><p>版权所有</p></footer>
</body></html>""".encode("utf-8")

def make_page_net() -> FakeWebNet:
    """每测试新建一份假网络，避免 calls 跨测试累积。"""
    return FakeWebNet(
        pages={
            WECHAT_URL: (200, "text/html", WECHAT_HTML),
            GENERIC_URL: (200, "text/html", GENERIC_HTML),
        },
        images={
            "https://mmbiz.qpic.cn/img1.jpg": (b"\xff\xd8fake-jpeg", "image/jpeg"),
            "https://example.com/img/cover.png": (b"\x89PNGfake", "image/png"),
        },
    )


def _capture_url(client, token, key: str, url: str | None = None, share: str | None = None):
    body = {
        "client_capture_id": f"{key}-1111-2222-3333-444444444444",
        "input_kind": "share" if share else "url",
        "original_url": url,
        "share_text": share,
    }
    return client.post(
        "/v1/captures", json=body,
        headers={**auth(token), "Idempotency-Key": key},
    )


@pytest.fixture(autouse=True)
def _clear_stale_queue(session_factory):
    """共享库里跨用例遗留的排队任务会抢走 _drain 的领取顺序，导致本用例提取没跑完。

    这些任务不属于当前用例（每个用例自己入队并 drain），因此开始时先取消。
    """
    from kbserver.workers import worker

    with session_factory() as db:
        for j in db.query(worker.Job).filter(
                worker.Job.state.in_(("queued", "retry_wait"))).all():
            j.state = "cancelled"
        db.commit()
    yield


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


# ---- 单元测试 ----

def test_extract_first_url():
    assert webpages.extract_first_url("看看这篇 https://mp.weixin.qq.com/s/x1，写得不错") == \
        "https://mp.weixin.qq.com/s/x1"
    assert webpages.extract_first_url("没有链接") is None
    assert webpages.extract_first_url(None) is None


def test_page_slug_deterministic():
    assert webpages.page_slug("https://a.com") == webpages.page_slug("https://a.com")
    assert webpages.page_slug("https://a.com") != webpages.page_slug("https://b.com")


# ---- 公众号路径 ----

def test_wechat_mp_extract_success(client, user_a, session_factory, web_net):
    """公众号已知结构：标题/作者/发布时间/正文段落；图片默认不下载，「提取图片」重新提取才存。"""
    net = web_net(make_page_net())
    c = _capture_url(client, user_a["phone"]["token"], "m4wx", url=WECHAT_URL)
    assert c.status_code == 202
    item_id = c.json()["item_id"]
    _drain(session_factory)

    it = _get_item(client, user_a["desktop"]["token"], item_id)
    assert it["source_revision"] == 2
    assert it["pipeline_state"] == "waiting_key"
    assert it["images_archived"] == 0
    m = _manifest(client, user_a["desktop"]["token"], it)
    src = m["source"]
    assert src["platform"] == "wechat_mp"
    assert src["title"] == "测试公众号文章标题"
    assert src["author"] == "测试公众号"
    assert src["published_at"] == "2026-09-01T08:30:00"
    assert src["coverage"] == "full_text"
    paths = [f["relative_path"] for f in m["files"]]
    assert not any(p == "originals/webpage/page.html" for p in paths)  # 路径带 slug
    assert any(p.startswith("originals/webpage/") and p.endswith("page.html") for p in paths)
    assert not any("img-" in p for p in paths)  # 默认不下载配图
    assert "normalized.md" in paths and "segments.json" in paths
    assert any("正文图片" in w and "未下载" in w for w in m["warnings"])
    # 只有页面 1 次请求
    assert len(net.calls) == 1

    # 「提取图片」：refetch 带 include_images，新来源版本带图
    r = client.post(f"/v1/items/{item_id}/refetch", json={"include_images": True},
                    headers=auth(user_a["desktop"]["token"]))
    assert r.status_code == 202
    _drain(session_factory)

    it2 = _get_item(client, user_a["desktop"]["token"], item_id)
    assert it2["source_revision"] == 3
    assert it2["images_archived"] == 1
    m2 = _manifest(client, user_a["desktop"]["token"], it2)
    paths2 = [f["relative_path"] for f in m2["files"]]
    assert any(p.endswith("img-001.jpg") for p in paths2)
    # 重新提取：页面 + 图片各 1 次
    assert len(net.calls) == 3


def test_wechat_publishes_paragraph_reading_layer(client, user_a, session_factory, web_net):
    """阅读层：额外发布段落版正文，segments 仍是逐段可引用的粒度。"""
    web_net(make_page_net())
    c = _capture_url(client, user_a["phone"]["token"], "m4wxpara", url=WECHAT_URL)
    assert c.status_code == 202
    item_id = c.json()["item_id"]
    _drain(session_factory)

    it = _get_item(client, user_a["desktop"]["token"], item_id)
    m = _manifest(client, user_a["desktop"]["token"], it)
    paths = [f["relative_path"] for f in m["files"]]
    assert "readable.md" in paths and "normalized.md" in paths

    r = client.get(f"/v1/items/{item_id}/reading", headers=auth(user_a["desktop"]["token"]))
    sm = r.json()["source_material"]
    # 原文三个 <p> 各自成段：段落层尊重原文分段，同时给出片段到段落的映射
    assert sm["readable_md"].count("^p") == 3
    assert sm["normalized_md"].count("^s") == 3
    assert sm["segment_paragraph"] == {"s0001": "p0001", "s0002": "p0002", "s0003": "p0003"}


def test_wechat_share_text_not_used_as_body(client, user_a, session_factory, web_net):
    """分享文字只有标题+链接+摘要：正文来自页面适配器，不冒充全文（docs/04 §5 同理）。"""
    web_net(make_page_net())
    share = "测试公众号文章标题 https://mp.weixin.qq.com/s/AbCdEf123 点击蓝字关注"
    c = _capture_url(client, user_a["phone"]["token"], "m4wxshare", share=share)
    item_id = c.json()["item_id"]
    _drain(session_factory)

    it = _get_item(client, user_a["desktop"]["token"], item_id)
    assert it["source_revision"] == 2  # 适配器产出了新材料；分享文字没有当正文
    assert it["pipeline_state"] == "waiting_key"
    m = _manifest(client, user_a["desktop"]["token"], it)
    assert m["source"]["platform"] == "wechat_mp"
    assert m["source"]["coverage"] == "full_text"
    assert any("点击蓝字关注" not in w for w in m["warnings"])


def test_wechat_missing_content_structure(client, user_a, session_factory, web_net):
    """无 js_content 的公众号页（删除/需登录）：如实进入补充材料。"""
    web_net(FakeWebNet(pages={
        WECHAT_URL: (200, "text/html", "<html><body><p>环境异常，完成验证后即可继续访问。</p></body></html>".encode("utf-8")),
    }))
    c = _capture_url(client, user_a["phone"]["token"], "m4wx404", url=WECHAT_URL)
    item_id = c.json()["item_id"]
    _drain(session_factory)

    it = _get_item(client, user_a["desktop"]["token"], item_id)
    assert it["pipeline_state"] == "needs_input"
    assert it["source_revision"] == 1  # 无新材料
    assert "正文" in (it.get("state_detail") or "")


# ---- 普通网页路径 ----

def test_generic_web_extract_success(client, user_a, session_factory, web_net):
    """启发式正文聚类：排除导航/页脚，标题与元数据来自 meta；图片默认不下载。"""
    net = web_net(make_page_net())
    c = _capture_url(client, user_a["phone"]["token"], "m4web", url=GENERIC_URL)
    item_id = c.json()["item_id"]
    _drain(session_factory)

    it = _get_item(client, user_a["desktop"]["token"], item_id)
    assert it["source_revision"] == 2
    m = _manifest(client, user_a["desktop"]["token"], it)
    src = m["source"]
    assert src["platform"] == "web"
    assert src["title"] == "普通网页标题"
    assert src["author"] == "网页作者"
    assert src["published_at"] == "2026-08-15T10:00:00+08:00"
    assert src["coverage"] == "full_text"
    paths = [f["relative_path"] for f in m["files"]]
    assert not any("img-" in p for p in paths)
    # 默认不下载配图：相对地址也不展开请求
    assert "https://example.com/img/cover.png" not in net.calls


def test_generic_web_excludes_nav_and_footer(web_net):
    """单元级：正文容器聚类后不包含导航与页脚段落。"""
    web_net(make_page_net())
    ext = webpages.extract(GENERIC_URL)
    texts = [s["text"] for s in ext.segments]
    assert "正文第一段，长度足够参与聚类打分，用来验证容器选择。" in texts
    assert "首页" not in texts and "版权所有" not in texts
    assert ext.title == "普通网页标题"


def test_webpage_include_images_switch(web_net):
    """单元级：extract 默认不下载图片；include_images=True 恢复限量下载。"""
    net = web_net(make_page_net())
    default_ext = webpages.extract(GENERIC_URL)
    assert default_ext.images == []
    assert any("未下载" in w for w in default_ext.warnings)
    assert len(net.calls) == 1  # 只抓了页面

    ext = webpages.extract(GENERIC_URL, include_images=True)
    assert [i.ext for i in ext.images] == ["png"]
    # 两次 extract 各抓一次页面，第二次另抓图片：共 3 次请求
    assert len(net.calls) == 3


def test_generic_js_shell_needs_input(client, user_a, session_factory, web_net):
    """JS 渲染空壳页：如实进入补充材料，不伪造正文。"""
    web_net(FakeWebNet(pages={
        GENERIC_URL: (200, "text/html", b"<html><head><title>App</title></head>"
                                       b"<body><div id=\"root\"></div>"
                                       b"<script>render()</script></body></html>"),
    }))
    c = _capture_url(client, user_a["phone"]["token"], "m4js", url=GENERIC_URL)
    item_id = c.json()["item_id"]
    _drain(session_factory)

    it = _get_item(client, user_a["desktop"]["token"], item_id)
    assert it["pipeline_state"] == "needs_input"
    assert "复制正文" in (it.get("state_detail") or "")


def test_webpage_403_blocked_needs_input(client, user_a, session_factory, web_net):
    """反爬 403：blocked → 补充材料，不重试也不伪造。"""
    web_net(FakeWebNet(pages={GENERIC_URL: (403, "text/html", b"<html><body>forbidden</body></html>")}))
    c = _capture_url(client, user_a["phone"]["token"], "m4forbid", url=GENERIC_URL)
    item_id = c.json()["item_id"]
    _drain(session_factory)

    it = _get_item(client, user_a["desktop"]["token"], item_id)
    assert it["pipeline_state"] == "needs_input"
    assert "403" in (it.get("state_detail") or "")


def test_webpage_non_html_rejected(client, user_a, session_factory, web_net):
    """PDF 等非 HTML 响应：明确提示直接上传文件，不硬解析。"""
    web_net(FakeWebNet(pages={GENERIC_URL: (200, "application/pdf", b"%PDF-1.7 fake")}))
    c = _capture_url(client, user_a["phone"]["token"], "m4pdf", url=GENERIC_URL)
    item_id = c.json()["item_id"]
    _drain(session_factory)

    it = _get_item(client, user_a["desktop"]["token"], item_id)
    assert it["pipeline_state"] == "needs_input"
    assert "上传文件" in (it.get("state_detail") or "")


def test_webpage_network_error_retries_not_needs_input(client, user_a, session_factory, web_net):
    """网络失败与页面拒绝区分：进入任务退避重试。"""
    web_net(FakeWebNet(fail_urls=(GENERIC_URL,)))
    c = _capture_url(client, user_a["phone"]["token"], "m4webnet", url=GENERIC_URL)
    item_id = c.json()["item_id"]
    _drain(session_factory, max_rounds=3)

    it = _get_item(client, user_a["desktop"]["token"], item_id)
    assert it["pipeline_state"] in ("extracting", "retry_wait", "queued")
    assert it["pipeline_state"] != "needs_input"


def test_webpage_images_missing_materials(client, user_a, session_factory, web_net):
    """「提取图片」重新提取：一张成功、一张失败，失败图片进 missing_materials 并有警告。"""
    html_two_imgs = GENERIC_HTML.decode("utf-8").replace(
        '<img src="/img/cover.png">',
        '<img src="/img/cover.png"><img src="https://cdn.example.com/fail.jpg">',
    ).encode("utf-8")
    web_net(FakeWebNet(
        pages={GENERIC_URL: (200, "text/html", html_two_imgs)},
        images={
            "https://example.com/img/cover.png": (b"\x89PNGfake", "image/png"),
            "https://cdn.example.com/fail.jpg": SafeFetchError("NETWORK_ERROR", "图片下载失败"),
        },
    ))
    c = _capture_url(client, user_a["phone"]["token"], "m4imgmiss", url=GENERIC_URL)
    item_id = c.json()["item_id"]
    _drain(session_factory)

    it = _get_item(client, user_a["desktop"]["token"], item_id)
    assert it["source_revision"] == 2  # 正文仍在，默认无图片请求
    m = _manifest(client, user_a["desktop"]["token"], it)
    assert not any(isinstance(e, str) and "fail.jpg" in e for e in m["missing_materials"])

    r = client.post(f"/v1/items/{item_id}/refetch", json={"include_images": True},
                    headers=auth(user_a["desktop"]["token"]))
    assert r.status_code == 202
    _drain(session_factory)

    it2 = _get_item(client, user_a["desktop"]["token"], item_id)
    assert it2["source_revision"] == 3  # 图片缺失不阻塞正文发布
    m2 = _manifest(client, user_a["desktop"]["token"], it2)
    image_missing = [e for e in m2["missing_materials"] if isinstance(e, str) and "fail.jpg" in e]
    assert len(image_missing) == 1
    assert "正文图片未取得" in image_missing[0]
    assert any("正文图片" in w for w in m2["warnings"])
    paths = [f["relative_path"] for f in m2["files"]]
    assert any(p.endswith("img-001.png") for p in paths)  # 成功的图已归档


def test_xiaohongshu_not_handled_as_webpage(client, user_a, session_factory, web_net):
    """小红书登录墙专项路径未提供前：不按普通网页抓取，保持等待适配器。"""
    net = web_net(FakeWebNet())
    c = _capture_url(client, user_a["phone"]["token"], "m4xhs",
                     url="https://www.xiaohongshu.com/explore/abc123")
    item_id = c.json()["item_id"]
    _drain(session_factory)

    it = _get_item(client, user_a["desktop"]["token"], item_id)
    assert it["pipeline_state"] == "needs_input"
    assert "适配器" in (it.get("state_detail") or "")
    assert net.calls == []  # 全程未触网


def test_bilibili_url_untouched_by_web_adapter(client, user_a, session_factory, web_net):
    """B 站链接仍由字幕适配器处理，网页适配器不接管。"""
    net = web_net(FakeWebNet(pages={
        "https://www.bilibili.com/video/BV1testBV000/": (200, "text/html", b"<html><body>video page</body></html>"),
    }))
    c = _capture_url(client, user_a["phone"]["token"], "m4webbili",
                     url="https://www.bilibili.com/video/BV1testBV000/")
    item_id = c.json()["item_id"]
    _drain(session_factory, max_rounds=6)

    it = _get_item(client, user_a["desktop"]["token"], item_id)
    # 字幕适配器接管：view API 被调用（假网络对其拒绝 → 任务重试），视频 HTML 页面从未被网页适配器抓取
    assert any("web-interface/view" in u for u in net.calls)
    assert "https://www.bilibili.com/video/BV1testBV000/" not in net.calls
    assert it["pipeline_state"] != "needs_input" or "字幕轨" in (it.get("state_detail") or "")
