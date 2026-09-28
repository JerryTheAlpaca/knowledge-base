"""上传音视频的格式支持判定（docs/13 §6.2）：一份清单，两处引用。

单一事实来源：Web 采集框按接口给的清单决定「这个文件走转写还是走普通附件」，
分块上传接口在收第一个字节之前按同一份判定拒绝——传完 2GB 才发现不支持是最差的
结果。判定结论只到「容器/扩展名层面能不能试」，真正能否解出音轨仍由服务端 FFmpeg
决定，解不出来在条目里如实显示为不支持。

扩展名优先、浏览器 MIME 兜底：桌面浏览器给 .mkv/.ts 的 file.type 经常是空串或
video/unknown，只看 MIME 会把能转写的文件当成普通附件；反过来 .mp4 在部分安卓
浏览器里报 audio/mp4，也不能让 MIME 覆盖扩展名。
"""
from __future__ import annotations

import os

MEDIA_AUDIO = "audio"
MEDIA_VIDEO = "video"
# 采集输入的媒体类型（CaptureInput.input_kind 与 media_kind 共用这套拼写）
MEDIA_KINDS = frozenset({MEDIA_AUDIO, MEDIA_VIDEO})

# 常见录音容器（docs/13 §1 上传录音沿用同一份判定）
AUDIO_EXTS = (
    ".mp3", ".m4a", ".m4b", ".aac", ".wav", ".flac", ".ogg", ".oga",
    ".opus", ".mka", ".wma", ".amr", ".aif", ".aiff", ".caf",
)
# 常见视频容器：只列服务端 FFmpeg 构建确实能解流的那批，拿不准的不写进来
VIDEO_EXTS = (
    ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".flv", ".f4v",
    ".ts", ".mts", ".m2ts", ".wmv", ".mpg", ".mpeg", ".mpe", ".ogv", ".3gp",
)
# 明确不接受的音视频容器。它们与 .txt/.png 不同：用户就是想传一段影像，收进普通
# 附件只会留下一条永远没有正文的条目。界面按这份清单说「不支持」，别说成「认不出」。
UNSUPPORTED_MEDIA_EXTS = (
    ".rm", ".rmvb", ".vob", ".ogm", ".divx", ".m2v", ".asf", ".mxf", ".rec",
)

_SUPPORTED_LABEL = {
    MEDIA_AUDIO: "MP3、M4A、WAV 等音频",
    MEDIA_VIDEO: "MP4、MOV、MKV、WebM 等视频",
}
# 界面与服务端共用的一句可上传范围提示（改了这里，采集框与错误文案一起跟着变）
SUPPORTED_HINT = (
    f"可以上传 {_SUPPORTED_LABEL[MEDIA_VIDEO]}，或 {_SUPPORTED_LABEL[MEDIA_AUDIO]}"
)


def suffix(filename: str | None) -> str:
    """小写扩展名（含点）；没有扩展名返回空串。"""
    if not filename:
        return ""
    base = filename.replace("\\", "/").rsplit("/", 1)[-1].strip().lower()
    ext = os.path.splitext(base)[1]
    return ext if 1 < len(ext) <= 10 else ""


def classify(filename: str | None, mime: str | None = None) -> str | None:
    """这个文件是音频还是视频；都不是（或认不出来）返回 None。"""
    ext = suffix(filename)
    if ext in AUDIO_EXTS:
        return MEDIA_AUDIO
    if ext in VIDEO_EXTS:
        return MEDIA_VIDEO
    if ext:
        # 有扩展名但不在清单里：按不支持处理，不用 MIME 把它捞回来
        # （.rmvb 之类浏览器可能报 video/*，而我们的构建未必能解）
        return None
    lowered = (mime or "").split(";")[0].strip().lower()
    if lowered.startswith("audio/"):
        return MEDIA_AUDIO
    if lowered.startswith("video/"):
        return MEDIA_VIDEO
    return None


def is_supported(filename: str | None, mime: str | None = None) -> bool:
    return classify(filename, mime) is not None


def formats_payload() -> dict:
    """GET /v1/media-formats 的响应体：Web 采集框据此路由文件。

    清单分两份给：接受的容器，以及明确不接受的音视频容器。界面要能把「用户想传
    影像但我们不接受」和「这本来就不是音视频（图片、PDF、文字）」说成两句话。
    """
    return {
        "audio": list(AUDIO_EXTS),
        "video": list(VIDEO_EXTS),
        "rejected": list(UNSUPPORTED_MEDIA_EXTS),
        "hint": SUPPORTED_HINT,
    }


def unsupported_message(filename: str | None, mime: str | None = None) -> str:
    """用户文案：说清这个文件为什么没接受，以及可以传什么。"""
    ext = suffix(filename)
    what = f"（{ext}）" if ext else "（认不出是音频还是视频）"
    return f"暂不支持这种文件格式{what}。{SUPPORTED_HINT}。"
