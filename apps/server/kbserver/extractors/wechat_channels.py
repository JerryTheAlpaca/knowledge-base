"""微信视频号适配器（docs/18 §7.4）。

视频号与公众号（wechat_mp）是两个来源，不合并。分享链接有两种形态
（P0 真实样本 2026-09-17 确认）：

- 短链 ``weixin.qq.com/sph/{id}`` → 302 展开到
  ``channels.weixin.qq.com/finder-preview/pages/sph?id={id}``；
- 直接的 ``channels.weixin.qq.com`` 页面链接。

sph 短 id 是稳定内容 ID（dynamicExportId 会过期，不用作 ID）。
finder-preview 播放壳本身是纯 JS 渲染，HTML 无元数据；页面自己调用
公开 Web 接口 ``/finder-preview/api/feed/get_feed_info``（匿名 POST，
无需登录/签名）返回 feedInfo/authorInfo 元数据。本适配器据此：

1. 保留完整分享文字（capture.json 已留存），从中分离 URL；
2. 展开分享链接并保存最终 URL 与原始 HTML；
3. 能解析出 sph id 时调上述公开接口读取元数据：作者/说明文字/
   发布时间/封面引用（coverage=metadata_only，说明文字不是视频转写）；
4. 接口失败或无 sph id 时退回页面 og 元数据解析；页面只给播放壳时
   如实标记 shell_only，不冒充已取得视频内容；
5. 不实现基于非公开接口的下载器；字幕/音频由用户补充走现有路径。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse

from ..config import get_settings
from . import fetch_base
from .fetch_base import (
    PlatformError,
    download_images,
    extract_first_url,
    fetch_page,
    page_slug,
)

EXTRACTOR_VERSION = "wechat_channels_page-1.1.0"

_CHANNEL_HOST = "channels.weixin.qq.com"
_SHORT_HOST = "weixin.qq.com"

# sph 短 id：短链路径 /sph/{id}，或展开后 finder-preview/pages/sph?id={id}
_SPH_PATH_RE = re.compile(r"/sph/([A-Za-z0-9]+)")
_SPH_QUERY_RE = re.compile(r"[?&]id=([A-Za-z0-9]+)")

# 播放壳特征：页面要求在微信客户端打开或只提供播放器
_ENV_MARKS = ("请在微信客户端打开", "仅支持微信内播放", "环境校验失败")
_LOGIN_MARKS = ("请先登录", "登录后查看", "需要在微信中登录")

_TITLE_MARKS = (
    re.compile(r'<meta[^>]+property="og:title"[^>]+content="([^"]{1,300})"'),
    re.compile(r"<title[^>]*>([^<]{1,300})"),
)
_DESC_MARKS = (
    re.compile(r'<meta[^>]+property="og:description"[^>]+content="([^"]{1,2000})"'),
)
_AUTHOR_MARKS = (
    re.compile(r'<meta[^>]+property="og:author"[^>]+content="([^"]{1,120})"'),
    re.compile(r'"nickname"\s*:\s*"([^"]{1,120})"'),
)

# 公开 Web 元数据接口（finder-preview 页面自用；匿名 POST，无签名）
_FEED_API_URL = f"https://{_CHANNEL_HOST}/finder-preview/api/feed/get_feed_info"


@dataclass
class ChannelsExtraction:
    """成功提取结果：元数据（可读取时）+ 原始响应。"""
    content_id: str  # sph 稳定 ID；解析不出时用确定性 slug
    canonical_url: str
    title: str | None
    author: str | None
    published_at: str | None
    raw_html: bytes
    raw_mime: str
    segments: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    missing_materials: list[str] = field(default_factory=list)
    images: list = field(default_factory=list)  # fetch_base.ImageDownload
    login_state_used: bool = False
    shell_only: bool = False  # 播放壳：无可读取文字

    @property
    def coverage(self) -> str:
        return "metadata_only"

    @property
    def media_kind(self) -> str:
        return "video"

    @property
    def extractor_name(self) -> str:
        return "wechat_channels_page"

    @property
    def original_path(self) -> str:
        return f"originals/wechat_channels/{self.content_id}/page.html"

    @property
    def source_locator(self) -> dict:
        return {"type": "wechat_channels_post", "content_id": self.content_id,
                "final_url": self.canonical_url}


def parse_sph_id(url: str | None) -> str | None:
    """从短链或展开后的视频号页面 URL 解析 sph 稳定 ID；解析不出返回 None。"""
    if not url:
        return None
    parsed = urlparse(url)
    m = _SPH_PATH_RE.search(parsed.path or "")
    if m:
        return m.group(1)
    m = _SPH_QUERY_RE.search(url)
    if m:
        return m.group(1)
    return None


def extract(url: str | None, *, share_text: str | None = None,
            cookies: dict[str, str] | None = None,
            include_images: bool = False) -> ChannelsExtraction:
    """提取视频号分享链接的可见元数据；失败抛 PlatformError。

    有 sph id 时优先走公开元数据接口；接口失败如实回落页面解析或
    进入补充材料，不把分享文字或标题冒充视频内容。include_images
    控制封面图下载；原视频不下载。
    """
    settings = get_settings()
    target = (url or "").strip() or extract_first_url(share_text)
    if not target:
        raise PlatformError("unsupported_type", "没有可处理的视频号链接")
    host = (urlparse(target).hostname or "").lower()
    if host != _SHORT_HOST and _CHANNEL_HOST not in host:
        raise PlatformError(
            "unsupported_type",
            "不是视频号分享链接；请从微信「分享-复制链接」重新复制。",
        )

    # 首跳（分享短链在此展开）；登录/环境限制且有会话 → 带会话重试一次
    res = fetch_page(target, max_bytes=settings.html_download_limit)
    login_state_used = False
    if _needs_retry(res) and cookies:
        res = fetch_page(res.url if res.status_code == 200 else target,
                         cookies=cookies, max_bytes=settings.html_download_limit)
        login_state_used = True

    final_host = (urlparse(res.url).hostname or "").lower()
    if _CHANNEL_HOST not in final_host:
        raise PlatformError(
            "unsupported_type",
            "链接最终未落在视频号页面；请从微信「分享-复制链接」重新复制。",
        )
    if res.status_code >= 400:
        raise PlatformError("deleted", "视频号内容已不可见；如仍存在，可补充文字摘录或截图。")

    body = res.content.decode("utf-8", errors="replace")
    sph_id = parse_sph_id(res.url) or parse_sph_id(target)
    missing: list[str] = ["原视频未保存：视频号只保存说明文字等元数据，不下载视频内容。"]
    warnings = ["已留存原始 HTML 响应；视频画面、音频与弹幕未归档，不构成视频内容存档。"]

    # 公开元数据接口（sph 链接专用；失败如实回落页面解析）
    feed: dict | None = None
    if sph_id:
        feed, api_warning = _fetch_feed_info(sph_id, referer=res.url)
        if api_warning:
            warnings.append(api_warning)

    # 元数据不可得且页面本身在登录/环境墙内：如实进入补充材料（老语义）
    if feed is None and _blocked_by_login_or_env(res):
        raise PlatformError(
            "login_required",
            "视频号页面要求在微信内打开或登录校验，服务器无法直接读取；"
            "链接和分享文字已保存，可补充文字摘录、字幕或音频。",
        )

    title = author = published_at = None
    desc = ""
    cover_url: str | None = None
    if feed is not None:
        author = _clean(feed.get("authorInfo", {}).get("nickname"), 120)
        desc = _clean(feed.get("feedInfo", {}).get("description"), 4000) or ""
        published_at = _iso_ts(feed.get("feedInfo", {}).get("createtime"))
        cover_url = _clean(feed.get("feedInfo", {}).get("coverUrl"), 2000)
        warnings.append("说明文字来自视频号公开元数据接口，不是视频转写；"
                        "如需完整内容请补充字幕、音频或文字摘录。")
        if not desc:
            warnings.append("元数据接口未返回说明文字。")
    else:
        # 无 sph id 或接口失败：退回页面 og 元数据（老路径）
        title = _first(_TITLE_MARKS, body)
        author = author or _first(_AUTHOR_MARKS, body)
        desc = _first(_DESC_MARKS, body) or ""

    # 播放壳判定：没有任何可读取文字
    shell_only = (not desc) and (not title or _is_bare_title(title, res.url))

    segments: list[dict] = []
    if not shell_only and desc:
        for i, line in enumerate([ln.strip() for ln in desc.split("\n") if ln.strip()], start=1):
            segments.append({
                "segment_id": f"s{i:04d}",
                "text": line,
                "artifact_file_id": None,
                "locator": {"type": "wechat_channels_post",
                            "content_id": sph_id, "index": i},
                "origin": "platform_content",
                "confidence": None,
                "kind": "paragraph",
            })

    images: list = []
    if shell_only:
        warnings.append("页面只提供播放壳，未取得可读取的文字内容。")
    if cover_url and include_images:
        got, img_missing = download_images(
            [cover_url], referer=f"https://{_CHANNEL_HOST}/",
            max_bytes_per_image=settings.max_image_bytes,
        )
        images.extend(got)
        missing.extend(img_missing)
    elif cover_url:
        warnings.append("视频封面未下载（默认不提取图片，需要时可在条目管理点「提取图片」重新提取）。")
    if not sph_id:
        warnings.insert(1, "未能解析出 sph 稳定内容 ID，归档路径使用确定性散列标识。")

    return ChannelsExtraction(
        content_id=sph_id or page_slug(res.url),
        canonical_url=res.url,
        title=None if shell_only else title,
        author=author,
        published_at=published_at,
        raw_html=res.content,
        raw_mime=res.mime or "text/html",
        segments=segments,
        warnings=warnings,
        missing_materials=missing,
        images=images,
        login_state_used=login_state_used,
        shell_only=shell_only,
    )


def _fetch_feed_info(sph_id: str, *, referer: str) -> tuple[dict | None, str | None]:
    """调 finder-preview 公开元数据接口；返回 (feed数据或None, 警告或None)。

    接口失败不中断采集：链接与页面已留存，回落页面解析或播放壳语义。
    """
    try:
        data = fetch_base.post_json(
            _FEED_API_URL,
            body={"baseReq": {"generalToken": ""}, "shortUri": sph_id},
            referer=referer,
        )
    except PlatformError as exc:
        return None, f"视频号元数据接口读取失败：{exc.message}"
    if data.get("errCode") != 0:
        err = data.get("errMsg")
        return None, f"视频号元数据接口返回错误（errCode={data.get('errCode')}：{err}）。"
    inner = data.get("data")
    feed = inner.get("feedInfo") if isinstance(inner, dict) else None
    if not isinstance(feed, dict):
        return None, "视频号元数据接口返回结构未知（无 feedInfo），已按页面内容处理。"
    author_info = inner.get("authorInfo") if isinstance(inner.get("authorInfo"), dict) else {}
    return {"feedInfo": feed, "authorInfo": author_info}, None


def _clean(value, limit: int) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()[:limit]
    return None


def _iso_ts(ts) -> str | None:
    """视频号 createtime 是秒级时间戳。"""
    if isinstance(ts, (int, float)) and ts > 0:
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _needs_retry(res: object) -> bool:
    """首跳后判断是否值得带会话重试：登录/环境墙，或被中转到登录域。"""
    if res.status_code in (401, 403):  # type: ignore[attr-defined]
        return True
    return _blocked_by_login_or_env(res)


def _blocked_by_login_or_env(res: object) -> bool:
    if res.status_code in (401, 403):  # type: ignore[attr-defined]
        return True
    body = res.content.decode("utf-8", errors="replace")  # type: ignore[attr-defined]
    return any(mark in body for mark in _ENV_MARKS + _LOGIN_MARKS)


def _first(marks, body: str) -> str | None:
    for rx in marks:
        m = rx.search(body)
        if m:
            value = m.group(1).strip()
            if value:
                return value
    return None


def _is_bare_title(title: str, url: str) -> bool:
    """标题只是站点名/视频号占位时视为无标题。"""
    bare = (title in ("视频号", "微信视频号", "视频号主页")
            or title.strip() == (urlparse(url).hostname or ""))
    return bare
