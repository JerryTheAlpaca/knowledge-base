"""平台识别：仅用于 source_hint=unknown 时的初始分类提示，不是强制路由（docs/02 §6.2）。"""
from __future__ import annotations

from urllib.parse import urlparse

_PATTERNS = [
    ("bilibili", ("bilibili.com", "b23.tv")),
    ("wechat_mp", ("mp.weixin.qq.com",)),
    # 视频号与公众号是两个来源（docs/18 §6），不能因为都来自微信而合并；
    # 短链 weixin.qq.com/sph/{id} 形态由真实样本确认（docs/18 §7.4）。
    # wechat_mp 在前，mp.weixin.qq.com 不会落到视频号。
    ("wechat_channels", ("channels.weixin.qq.com", "weixin.qq.com")),
    ("zhihu", ("zhihu.com",)),
    ("xiaohongshu", ("xiaohongshu.com", "xhslink.com", "xhslink.cn")),
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
