"""平台识别：仅用于 source_hint=unknown 时的初始分类提示，不是强制路由（docs/02 §6.2）。"""
from __future__ import annotations

from urllib.parse import urlparse

_PATTERNS = [
    ("bilibili", ("bilibili.com", "b23.tv")),
    ("wechat_mp", ("mp.weixin.qq.com",)),
    # 视频号与公众号是两个来源（docs/18 §6），不能因为都来自微信而合并；
    # wechat_mp 在前，mp.weixin.qq.com 不会落到视频号。
    ("wechat_channels", ("channels.weixin.qq.com",)),
    ("zhihu", ("zhihu.com",)),
    ("xiaohongshu", ("xiaohongshu.com", "xhslink.com", "xhslink.cn")),
]
# weixin.qq.com 只有 /sph/{id} 这一种形态是视频号短链（docs/18 §7.4 真实样本）；
# 该域的其他页面（开放平台文档、登录页等）不是视频号内容，按普通网页提取，
# 否则会被视频号适配器以「不是视频号分享链接」终态拒掉（审查 C-17）。
_SPH_HOSTS = ("weixin.qq.com",)


def guess_platform(url: str) -> str:
    if not url:
        return "unknown"
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return "unknown"
    if not host:
        return "unknown"
    for platform, domains in _PATTERNS:
        if any(host == d or host.endswith("." + d) for d in domains):
            return platform
    # 放在模式表之后：mp.weixin.qq.com 已被 wechat_mp 命中，不会被这条规则吃掉
    if any(host == d or host.endswith("." + d) for d in _SPH_HOSTS):
        return "wechat_channels" if (parsed.path or "").startswith("/sph/") else "web"
    return "web"
