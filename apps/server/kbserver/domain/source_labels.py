"""来源类型派生（docs/13 §5）：platform + media_kind → 展示用 source_type/label/icon_key。

来源身份与「处理方式」分离：ASR 只是处理标记，不改变来源。平台/来源类型是
服务端派生并随 SourceRevision 冻结的展示字段，Web 与插件不得各自用 URL 正则猜。

| platform | media_kind | source_type | 展示标签 | icon_key |
| --- | --- | --- | --- | --- |
| bilibili | video | bilibili | B 站 | bilibili_video |
| web | audio | web_audio | 网页音频 | web_audio |
| audio_upload | audio | audio_upload | 上传录音 | audio_upload |
| web | text | web | 网页 | web_page |

标签是纯文本（不带表情符号）；图标由客户端按 icon_key 渲染，不能只靠颜色区分。
不建来源标签表：展示字段由 platform/media_kind 纯函数派生。
"""
from __future__ import annotations

# (platform, media_kind) -> (source_type, label, icon_key)
SOURCE_MAP: dict[tuple[str, str], tuple[str, str, str]] = {
    ("bilibili", "video"): ("bilibili", "B 站", "bilibili_video"),
    ("web", "audio"): ("web_audio", "网页音频", "web_audio"),
    ("audio_upload", "audio"): ("audio_upload", "上传录音", "audio_upload"),
    ("web", "text"): ("web", "网页", "web_page"),
}

# 平台默认 media_kind（旧记录缺 media_kind 时的回退，docs/13 §5 末段）
PLATFORM_DEFAULT_KIND: dict[str, str] = {
    "bilibili": "video",
    "audio_upload": "audio",
    "web": "text",
    "wechat_mp": "text",
    "xiaohongshu": "text",
}

AUDIO_MEDIA_KINDS = frozenset({"video", "audio"})
UPLOAD_PLATFORM = "audio_upload"
WEB_PLATFORM = "web"


def normalize_platform(platform: str | None) -> str:
    """公众号/小红书等具体平台归一到展示平台；未知保持原样。"""
    p = (platform or "unknown").strip() or "unknown"
    if p in ("wechat_mp", "xiaohongshu"):
        return WEB_PLATFORM
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
    return PLATFORM_DEFAULT_KIND.get(platform or "", "text")


def derive_source_type(platform: str | None, media_kind: str | None) -> str:
    """由 platform/media_kind 派生 source_type；未登记的组合作保守回退。"""
    p = normalize_platform(platform)
    kind = (media_kind or "").strip() or default_media_kind(platform)
    entry = SOURCE_MAP.get((p, kind))
    if entry is not None:
        return entry[0]
    if p == "bilibili":
        return "bilibili"
    if p == UPLOAD_PLATFORM:
        return "audio_upload"
    if kind == "audio":
        return "web_audio" if p == WEB_PLATFORM else f"{p}_audio"
    return p


def display_for(source_type: str, platform: str | None = None,
                media_kind: str | None = None) -> dict:
    """展示字段：source_type、label（纯文本）、icon_key。缺失组合按来源类型回退。"""
    for (_p, _kind), (st, label, icon_key) in SOURCE_MAP.items():
        if st == source_type:
            return {"source_type": st, "source_label": label, "icon_key": icon_key}
    # 未登记来源：如实显示平台名，不复用别人的图标
    label = normalize_platform(platform) if platform else source_type
    return {"source_type": source_type, "source_label": label,
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
