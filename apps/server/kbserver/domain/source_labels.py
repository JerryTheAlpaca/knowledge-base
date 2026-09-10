"""来源类型派生（docs/13 §5）：platform + media_kind → 展示用 source_type/label/icon_key。

来源身份与「处理方式」分离：ASR 只是处理标记，不改变来源。平台/来源类型是
服务端派生并随 SourceRevision 冻结的展示字段，Web 与插件不得各自用 URL 正则猜。

展示标签是**纯文字，不带表情符号或图标符号**（用户 2026-09-10 明确要求）：

| platform | media_kind | source_type | 展示标签 | icon_key |
| --- | --- | --- | --- | --- |
| bilibili | video | bilibili | B 站 | bilibili_video |
| wechat_mp | text | wechat_mp | 微信公众号 | wechat_mp |
| xiaohongshu | text | xiaohongshu | 小红书 | xiaohongshu |
| web | text | web | 网页 | web_page |
| web / wechat_mp / xiaohongshu | audio | web_audio | 网页音频 | web_audio |
| audio_upload | audio | audio_upload | 上传录音 | audio_upload |

平台身份不被合并：微信公众号仍是 wechat_mp，不并进"网页"；只有普通网页才是"网页"。
icon_key 仍由服务端派生（留给客户端将来做图形标识），界面当前只显示文字，不用表情符号。
不建来源标签表：展示字段由 platform/media_kind 纯函数派生。
"""
from __future__ import annotations

# 平台别名归一：只处理同一来源的不同拼写，不改变来源身份
PLATFORM_ALIASES: dict[str, str] = {
    "weichat_mp": "wechat_mp",
    "wechat": "wechat_mp",
    "webpage": "web",
    "xhs": "xiaohongshu",
}

# (platform, media_kind) -> (source_type, label, icon_key)
SOURCE_MAP: dict[tuple[str, str], tuple[str, str, str]] = {
    ("bilibili", "video"): ("bilibili", "B 站", "bilibili_video"),
    ("wechat_mp", "text"): ("wechat_mp", "微信公众号", "wechat_mp"),
    ("xiaohongshu", "text"): ("xiaohongshu", "小红书", "xiaohongshu"),
    ("web", "text"): ("web", "网页", "web_page"),
    ("audio_upload", "audio"): ("audio_upload", "上传录音", "audio_upload"),
    ("web", "audio"): ("web_audio", "网页音频", "web_audio"),
    ("wechat_mp", "audio"): ("web_audio", "网页音频", "web_audio"),
    ("xiaohongshu", "audio"): ("web_audio", "网页音频", "web_audio"),
}

# 平台默认 media_kind（旧记录缺 media_kind 时的回退，docs/13 §5 末段）
PLATFORM_DEFAULT_KIND: dict[str, str] = {
    "bilibili": "video",
    "audio_upload": "audio",
    "web": "text",
    "wechat_mp": "text",
    "xiaohongshu": "text",
}

# 只有平台、没有登记组合时的标签回退（纯文字）
PLATFORM_LABELS: dict[str, str] = {
    "web": "网页",
    "wechat_mp": "微信公众号",
    "xiaohongshu": "小红书",
    "bilibili": "B 站",
    "audio_upload": "上传录音",
    "note": "笔记",
    "file": "文件",
    "unknown": "未知",
    # 旧客户端把采集渠道写进了 platform（docs/13 §5.2 的 web_inbox 是渠道不是平台）
    "web_inbox": "未知",
}

# 不是平台名的取值：不能据此判定来源，需回退到 URL
NON_PLATFORM_VALUES = frozenset({"", "unknown", "web_inbox"})

AUDIO_MEDIA_KINDS = frozenset({"video", "audio"})
UPLOAD_PLATFORM = "audio_upload"
WEB_PLATFORM = "web"
WEB_LIKE_PLATFORMS = frozenset({WEB_PLATFORM, "wechat_mp", "xiaohongshu"})


def normalize_platform(platform: str | None) -> str:
    """归一平台拼写别名；不合并不同来源（公众号仍是 wechat_mp，不并进网页）。"""
    p = (platform or "unknown").strip() or "unknown"
    return PLATFORM_ALIASES.get(p, p)


def resolve_platform(platform: str | None, url: str | None) -> str:
    """平台判定：platform 缺失或是渠道取值时按 URL 推断（渠道不能当平台）。"""
    from .platforms import guess_platform

    p = normalize_platform(platform)
    if p in NON_PLATFORM_VALUES:
        return normalize_platform(guess_platform(url or ""))
    return p


def default_media_kind(platform: str | None, *, has_audio: bool = False) -> str:
    """按平台推断 media_kind；无法确认是否音频的旧 web 记录保持 text（不强行回填）。"""
    p = normalize_platform(platform)
    if p == "bilibili":
        return "video"
    if p == UPLOAD_PLATFORM:
        return "audio"
    if p == WEB_PLATFORM and has_audio:
        return "audio"
    return PLATFORM_DEFAULT_KIND.get(p, "text")


def derive_source_type(platform: str | None, media_kind: str | None) -> str:
    """由 platform/media_kind 派生 source_type；未登记的组合作保守回退。"""
    p = normalize_platform(platform)
    kind = (media_kind or "").strip() or default_media_kind(platform)
    entry = SOURCE_MAP.get((p, kind))
    if entry is not None:
        return entry[0]
    if kind == "audio":
        return "web_audio" if p in WEB_LIKE_PLATFORMS else f"{p}_audio"
    return p


def display_for(source_type: str, platform: str | None = None,
                media_kind: str | None = None) -> dict:
    """展示字段：source_type、label（纯文字）、icon_key。缺失组合按平台名回退。"""
    for (_p, _kind), (st, label, icon_key) in SOURCE_MAP.items():
        if st == source_type:
            return {"source_type": st, "source_label": label, "icon_key": icon_key}
    p = normalize_platform(platform)
    return {"source_type": source_type, "source_label": PLATFORM_LABELS.get(p, p),
            "icon_key": f"{source_type}_generic"}


def source_fields(platform: str | None, media_kind: str | None) -> dict:
    """一次派生出全部展示字段；platform 保留原字段以兼容旧客户端。"""
    source_type = derive_source_type(platform, media_kind)
    out = display_for(source_type, platform, media_kind)
    out["platform"] = normalize_platform(platform)
    out["media_kind"] = (media_kind or default_media_kind(platform))
    return out


def is_audio_source(platform: str | None, media_kind: str | None) -> bool:
    kind = media_kind or default_media_kind(platform)
    return kind in AUDIO_MEDIA_KINDS
