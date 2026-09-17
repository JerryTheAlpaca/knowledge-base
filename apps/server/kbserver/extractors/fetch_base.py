"""全平台适配器共享抓取底座（docs/02 §9.3、docs/18 §7.1）。

B 站、公众号、普通网页、知乎、小红书、视频号的所有来源适配器共用：
统一错误语义、浏览器请求头、分享文字 URL 抽取、受限页面抓取
（用户托管 Cookie 只随首跳，跨主机重定向由 safe_fetch 剥离）、
正文图片限量下载。

不建插件框架、不做平台路由——平台识别与分发在 worker；各平台的
结构解析和特有探测（如 B 站 WBI 签名、知乎 zse 挑战）留在各自模块。

P0 实测结论（2026-09-16，本机直连与代理同结果）：
- 知乎匿名请求 403 + `zh-zse-ck` JS 挑战壳；完整浏览器 Cookie（含
  __zse_ck）仍 403 —— 该签名与浏览器环境绑定，仅复制 Cookie 不能
  稳定读取（docs/18 §5.2 预警的情况）。
- 小红书 /explore/{id} 匿名 302 → /404/sec_*（error_code=300031）。
因此页面类适配器的自动读取都按「先匿名尝试 → 明确登录限制时用
用户托管会话重试一次 → 仍失败如实进入补充材料」实现，不宣称已支持。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from ..security.safe_fetch import SafeFetchError, safe_fetch
from ..security.safe_fetch import post_json as safe_post_json

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# 正文图片扩展映射
IMG_EXT = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "image/bmp": "bmp",
    "image/avif": "avif",
}

# 从分享文字提取 URL 的宽松正则（B 站/网页/三平台同规则）
URL_IN_TEXT = re.compile(r"https?://[^\s，,、）)】\]]+")

# 单条最多下载的正文图片数；其余如实记入缺失清单
MAX_CONTENT_IMAGES = 24


class PlatformError(Exception):
    """来源适配失败。status 是稳定机器码，worker 据此决定行为：

    - network_error：临时网络失败 → 上抛走任务级有限退避；
    - blocked / login_required / deleted / structure_changed /
      unsupported_type / unsupported / empty_content / no_track /
      video_not_found：终态 → 进入补充材料（needs_input），不伪造正文。
    B 站/网页模块以别名（BilibiliError/WebpageError）引用同一类，
    历史语义不变。
    """

    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class ImageDownload:
    """成功下载的一张正文图片。"""
    url: str
    mime: str
    ext: str
    data: bytes


def browser_headers(referer: str | None = None) -> dict[str, str]:
    """通用页面请求头（HTML 抓取共用；B 站 API 头另有专有语义单独构造）。"""
    headers = {
        "User-Agent": BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if referer:
        headers["Referer"] = referer
    return headers


def extract_first_url(share_text: str | None) -> str | None:
    """从分享文字提取第一条 URL（分享文字本身由 capture.json 留存）。"""
    if not share_text:
        return None
    m = URL_IN_TEXT.search(share_text)
    return m.group(0).rstrip(".,;！!?？") if m else None


def page_slug(url: str) -> str:
    """URL 的确定性短标识，用于归档路径。"""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]


def fetch_page(url: str, *, cookies: dict[str, str] | None = None,
               referer: str | None = None, max_bytes: int,
               timeout: float = 20.0):
    """受限抓取一个页面；可选携带用户托管 Cookie（仅首跳同主机有效）。

    safe_fetch 对跨主机重定向自动剥离 Cookie/Authorization，登录态不会
    跟随跳转外泄（docs/18 §7.2 规则 4）。网络失败统一转译为
    PlatformError：NETWORK_ERROR → network_error（可重试），其余 → blocked。
    """
    headers = browser_headers(referer)
    if cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    try:
        return safe_fetch(url, max_bytes=max_bytes, timeout=timeout, headers=headers)
    except SafeFetchError as exc:
        if exc.code == "NETWORK_ERROR":
            raise PlatformError("network_error", f"页面下载失败：{exc}") from exc
        raise PlatformError("blocked", f"页面下载被拒绝：{exc}") from exc


def post_json(url: str, *, body: dict, referer: str | None = None,
              max_bytes: int | None = None, timeout: float = 20.0) -> dict:
    """受限 POST 一个公开 Web JSON 接口，返回解析后的对象。

    不携带用户托管 Cookie（公开元数据接口匿名即可；登录态不外发到
    非必要端点）。同源 API 普遍校验 Origin/Referer（视频号实测，
    docs/18 §7.4）：Origin 取目标 URL 自身 origin，Referer 用调用方
    传入的来源页。失败语义与 fetch_page 相同。
    """
    headers = browser_headers(referer)
    parsed = urlparse(url)
    headers["Origin"] = f"{parsed.scheme or 'https'}://{parsed.hostname}"
    headers["Content-Type"] = "application/json"
    try:
        return safe_post_json(url, json_body=body, max_bytes=max_bytes,
                              timeout=timeout, headers=headers)
    except SafeFetchError as exc:
        if exc.code == "NETWORK_ERROR":
            raise PlatformError("network_error", f"接口请求失败：{exc}") from exc
        raise PlatformError("blocked", f"接口请求被拒绝：{exc}") from exc


def download_images(urls: list[str], *, referer: str | None = None,
                    max_bytes_per_image: int) -> tuple[list["ImageDownload"], list[str]]:
    """限量下载图片，返回 ([ImageDownload...], [缺失说明...])。

    单图超限/失败如实写入缺失清单，不中断其余图片。
    """
    got: list[ImageDownload] = []
    missing: list[str] = []
    for u in urls:
        if len(got) >= MAX_CONTENT_IMAGES:
            missing.append(f"正文图片未下载（超出单条 {MAX_CONTENT_IMAGES} 张上限）：{u}")
            continue
        headers = browser_headers(referer)
        try:
            res = safe_fetch(u, max_bytes=max_bytes_per_image, timeout=20.0,
                             mime_prefixes=("image/",), headers=headers)
        except SafeFetchError as exc:
            missing.append(f"正文图片未取得：{u}（{exc}）")
            continue
        if res.status_code >= 400:
            missing.append(f"正文图片未取得：{u}（HTTP {res.status_code}）")
            continue
        got.append(ImageDownload(url=u, mime=res.mime,
                                 ext=IMG_EXT.get(res.mime, "img"), data=res.content))
    return got, missing
