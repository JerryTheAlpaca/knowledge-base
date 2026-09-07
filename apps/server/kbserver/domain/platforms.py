"""平台识别：仅用于 source_hint=unknown 时的初始分类提示，不是强制路由（docs/02 §6.2）。"""
from __future__ import annotations

from urllib.parse import urlparse

_PATTERNS = [
    ("bilibili", ("bilibili.com", "b23.tv")),
    ("wechat_mp", ("mp.weixin.qq.com",)),
    ("xiaohongshu", ("xiaohongshu.com", "xhslink.com")),
]


def guess_platform(url: str) -> str:
    if not url:
        return "unknown"
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return "unknown"
    for platform, domains in _PATTERNS:
        if any(host == d or host.endswith("." + d) for d in domains):
            return platform
    return "web" if host else "unknown"
