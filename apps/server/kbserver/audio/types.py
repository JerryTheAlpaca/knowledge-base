"""通用音频输入/准备类型（docs/13 §3）。

来源身份（AudioSource）与获取方式（RemoteAudioInput/ObjectAudioInput）分离：

- source：来源事实（platform、media_kind、适配器、原始/规范地址、标题作者、
  定位信息、时长提示），可持久化并冻结在本次 run 的输入中；
- input：本次获取方式——远程 URL + 受限请求策略（只用于本次获取），或
  服务端解析出的已授权对象文件路径（持久有效）。

转写清单保留 input_fingerprint：B 站由 BV/cid/音轨稳定标识组成，网页由页面
与选中媒体标识组成，上传由原件 SHA-256 组成。带时效签名的 URL 不作永久身份。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# 获取方式（写进 asr 元数据，不是来源）
ACQ_PLAYER_STREAM = "player_audio_stream"   # B 站播放接口独立音轨
ACQ_WEB_STREAM = "web_audio_stream"         # 普通网页/直链音频
ACQ_UPLOADED = "uploaded_audio"             # 用户上传录音原件

REMOTE_KIND = "remote"
OBJECT_KIND = "object"


class AudioSourceError(Exception):
    """来源解析失败（页面/直链/播放接口）。status 由调用方区分重试与补充材料。

    取代通用层对 BilibiliAudioError 的依赖；B 站错误在适配器边界转换成本类。
    """

    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class AudioPrepareError(Exception):
    """音频准备（读取/解码）失败。status 语义同上。"""

    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class AudioPrepareAborted(Exception):
    """准备被外部中止（让出或取消），不是错误；reason 为 yield|cancel。"""

    def __init__(self, reason: str):
        super().__init__(f"音频准备被中止（{reason}）")
        self.reason = reason


class AudioSourceSelectionRequired(Exception):
    """页面存在多条独立音频，需要用户选择一次（docs/13 §4.3）。"""

    def __init__(self, candidates: list[dict]):
        super().__init__("页面存在多条音频，需要选择要转写的一条")
        self.candidates = candidates


@dataclass(frozen=True)
class AudioSource:
    """来源事实；不含任何模型/转写结果。"""

    platform: str
    media_kind: str
    adapter_id: str
    adapter_version: str
    original_url: str | None = None
    canonical_url: str | None = None
    title: str | None = None
    author: str | None = None
    published_at: str | None = None
    source_locator: dict = field(default_factory=dict)
    duration_hint: float | None = None
    acquisition: str = ""

    def locator(self) -> dict:
        """冻结进 PCM 清单 / SourceRevision 的来源描述（不含敏感头与签名 URL）。"""
        out = {
            "type": self.source_locator.get("type") or f"{self.platform}_{self.media_kind}",
            "platform": self.platform,
            "media_kind": self.media_kind,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "canonical_url": self.canonical_url,
            "acquisition": self.acquisition,
        }
        out.update({k: v for k, v in self.source_locator.items() if k not in out})
        return out


@dataclass
class RemoteAudioInput:
    """远程音频输入：URL + 本次有效的受限请求策略。"""

    source: AudioSource
    urls: list[str]
    headers: dict[str, str] = field(default_factory=dict)
    duration_hint: float | None = None
    stream_meta: dict = field(default_factory=dict)
    # 上游声明时长可信时才做「解码时长必须吻合」校验（防 B 站静默截断）
    strict_duration: bool = False
    kind: str = REMOTE_KIND


@dataclass
class ObjectAudioInput:
    """对象音频输入：服务端解析出的已授权本地对象文件（可 seek）。"""

    source: AudioSource
    path: Path
    size_bytes: int = 0
    sha256: str = ""
    duration_hint: float | None = None
    kind: str = OBJECT_KIND


AudioInput = RemoteAudioInput | ObjectAudioInput


@dataclass
class ResolvedAudioSource:
    """一次解析结果：来源事实 + 本次输入 + 幂等指纹。"""

    source: AudioSource
    input: AudioInput
    input_fingerprint: str


@dataclass
class PreparedChunk:
    """一个已解码 WAV 段；时间为原音频绝对秒。"""

    index: int
    path: str          # 相对工作目录
    core_start: float
    core_end: float
    duration: float    # 按实际采样数计算
    bytes: int
    sha256: str


@dataclass
class PreparedAudio:
    """完整准备结果：原子提交清单前不得作为转写输入（docs/11 §9.1）。"""

    chunks_dir: str
    chunks: list[PreparedChunk]
    total_duration: float
    expected_duration: float
    source_bytes: int
    meta: dict = field(default_factory=dict)  # 归属信息（来源定位/音轨标识等）
