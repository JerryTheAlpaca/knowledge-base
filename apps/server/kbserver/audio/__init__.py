"""通用音频接入（docs/13）：来源解析 → 通用准备 → 现有 ASR 链路。

- types：来源/输入/准备结果与通用错误；
- prepare：远程流与本地对象两种输入的共用解码、切段、时长硬限与取消逻辑。

本包不感知模型与转写结果；来源适配器在 extractors 层，识别仍在 workers/asr。
"""
from __future__ import annotations

from .prepare import AudioLimits, prepare_audio
from .types import (
    ACQ_PLAYER_STREAM,
    ACQ_UPLOADED,
    ACQ_WEB_STREAM,
    AudioInput,
    AudioPrepareAborted,
    AudioPrepareError,
    AudioSource,
    AudioSourceError,
    AudioSourceSelectionRequired,
    ObjectAudioInput,
    PreparedAudio,
    PreparedChunk,
    RemoteAudioInput,
    ResolvedAudioSource,
)

__all__ = [
    "ACQ_PLAYER_STREAM",
    "ACQ_UPLOADED",
    "ACQ_WEB_STREAM",
    "AudioInput",
    "AudioLimits",
    "AudioPrepareAborted",
    "AudioPrepareError",
    "AudioSource",
    "AudioSourceError",
    "AudioSourceSelectionRequired",
    "ObjectAudioInput",
    "PreparedAudio",
    "PreparedChunk",
    "RemoteAudioInput",
    "ResolvedAudioSource",
    "prepare_audio",
]
