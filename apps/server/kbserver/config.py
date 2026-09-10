"""应用配置。所有配置来自环境变量；.env.example 只列变量名与非秘密默认值。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    # 基础
    public_base_url: str = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000")
    database_url: str = os.environ.get("DATABASE_URL", "sqlite:///./data/db/app.db")
    objects_dir: Path = Path(os.environ.get("OBJECTS_DIR", "./data/objects"))
    tmp_dir: Path = Path(os.environ.get("TMP_DIR", "./data/tmp"))
    deletions_dir: Path = Path(os.environ.get("DELETIONS_DIR", "./data/deletions"))

    # 密钥（只读 secret 文件，不进代码/镜像/数据库）
    master_key_file: Path = Path(os.environ.get("MASTER_KEY_FILE", "./data/secrets/master.key"))
    master_key_version: int = int(os.environ.get("MASTER_KEY_VERSION", "1"))
    session_secret_file: Path = Path(os.environ.get("SESSION_SECRET_FILE", "./data/secrets/session.key"))

    # 容量限制（docs/02 §14.1 工程初始值）
    max_single_file_bytes: int = int(os.environ.get("MAX_SINGLE_FILE_BYTES", str(100 * 1024 * 1024)))
    max_image_bytes: int = int(os.environ.get("MAX_IMAGE_BYTES", str(20 * 1024 * 1024)))
    max_json_body_bytes: int = int(os.environ.get("MAX_JSON_BODY_BYTES", str(1024 * 1024)))
    max_attachments_per_capture: int = int(os.environ.get("MAX_ATTACHMENTS_PER_CAPTURE", "10"))
    max_capture_total_bytes: int = int(os.environ.get("MAX_CAPTURE_TOTAL_BYTES", str(100 * 1024 * 1024)))
    html_download_limit: int = int(os.environ.get("HTML_DOWNLOAD_LIMIT", str(5 * 1024 * 1024)))
    subtitle_download_limit: int = int(os.environ.get("SUBTITLE_DOWNLOAD_LIMIT", str(10 * 1024 * 1024)))
    # 音频主体独立额度（docs/13 §7.1）：普通附件限额不放宽
    max_audio_upload_bytes: int = int(os.environ.get("MAX_AUDIO_UPLOAD_BYTES", str(8 * 1024 * 1024 * 1024)))
    audio_upload_chunk_bytes: int = int(os.environ.get("AUDIO_UPLOAD_CHUNK_BYTES", str(16 * 1024 * 1024)))
    audio_upload_session_ttl_hours: int = int(os.environ.get("AUDIO_UPLOAD_SESSION_TTL_HOURS", "24"))

    # 流式读取（docs/11 §5.2）：HTTP→FFmpeg 管道的块大小
    stream_chunk_bytes: int = int(os.environ.get("STREAM_CHUNK_BYTES", str(64 * 1024)))

    # 本地 ASR（docs/11 §9.3）：默认关闭，验收后由部署显式开启
    asr_enabled: bool = os.environ.get("ASR_ENABLED", "false").lower() in ("1", "true", "yes")
    asr_model: str = os.environ.get("ASR_MODEL", "dolphin")  # dolphin | sense_voice（部署级默认）
    asr_model_dir: Path = Path(os.environ.get("ASR_MODEL_DIR", "/opt/models"))
    asr_engine_bin: str = os.environ.get("ASR_ENGINE_BIN", "/opt/sherpa/bin/sherpa-onnx-offline")
    asr_ffmpeg_bin: str = os.environ.get("ASR_FFMPEG_BIN", "ffmpeg")
    asr_threads: int = int(os.environ.get("ASR_THREADS", "1"))
    asr_chunk_seconds: int = int(os.environ.get("ASR_CHUNK_SECONDS", "20"))
    asr_chunk_context_seconds: float = float(os.environ.get("ASR_CHUNK_CONTEXT_SECONDS", "1"))
    asr_max_duration_seconds: int = int(os.environ.get("ASR_MAX_DURATION_SECONDS", "36000"))
    asr_max_input_bytes: int = int(os.environ.get("ASR_MAX_INPUT_BYTES", str(8 * 1024 * 1024 * 1024)))
    asr_tmp_ttl_hours: int = int(os.environ.get("ASR_TMP_TTL_HOURS", "24"))
    asr_chunk_timeout_seconds: int = int(os.environ.get("ASR_CHUNK_TIMEOUT_SECONDS", "900"))
    # 服务器空闲准入（docs/11 §6.2）：整机 CPU 忙碌比例阈值与可用内存（MiB）
    asr_idle_cpu_start: float = float(os.environ.get("ASR_IDLE_CPU_START", "0.25"))
    asr_idle_cpu_stop: float = float(os.environ.get("ASR_IDLE_CPU_STOP", "0.70"))
    asr_idle_hold_seconds: int = int(os.environ.get("ASR_IDLE_HOLD_SECONDS", "60"))
    asr_busy_cooldown_seconds: int = int(os.environ.get("ASR_BUSY_COOLDOWN_SECONDS", "120"))
    asr_idle_min_available_mib: int = int(os.environ.get("ASR_IDLE_MIN_AVAILABLE_MIB", "800"))
    asr_busy_min_available_mib: int = int(os.environ.get("ASR_BUSY_MIN_AVAILABLE_MIB", "256"))

    # 保留与清理（docs/02 §14.3）
    unreferenced_upload_ttl_hours: int = int(os.environ.get("UNREFERENCED_UPLOAD_TTL_HOURS", "24"))
    unacked_bundle_retention_days: int = int(os.environ.get("UNACKED_BUNDLE_RETENTION_DAYS", "30"))
    acked_bundle_retention_days: int = int(os.environ.get("ACKED_BUNDLE_RETENTION_DAYS", "7"))
    idempotency_key_ttl_days: int = int(os.environ.get("IDEMPOTENCY_KEY_TTL_DAYS", "90"))
    event_retention_days: int = int(os.environ.get("EVENT_RETENTION_DAYS", "90"))

    # 身份
    token_ttl_days: int = int(os.environ.get("TOKEN_TTL_DAYS", "90"))
    pairing_code_ttl_minutes: int = int(os.environ.get("PAIRING_CODE_TTL_MINUTES", "10"))
    web_session_ttl_days: int = int(os.environ.get("WEB_SESSION_TTL_DAYS", "7"))

    # 中心认证（docs/05 §4.1）：统一登录由 jerrythealpaca.cn 的 Ledger 提供。
    # 为空表示未接入中心认证：Web Cookie 通道返回 503，插件/设备 Bearer 不受影响。
    auth_session_url: str = os.environ.get("AUTH_SESSION_URL", "")
    auth_login_url: str = os.environ.get("AUTH_LOGIN_URL", "")
    auth_logout_url: str = os.environ.get("AUTH_LOGOUT_URL", "")
    # 中心会话 Cookie 名称；AUTH_COOKIE_DOMAIN 用于校验续期 Cookie 的域（为空则要求无域属性）
    auth_cookie_name: str = os.environ.get("AUTH_COOKIE_NAME", "__Secure-session")
    auth_cookie_domain: str = os.environ.get("AUTH_COOKIE_DOMAIN", "")
    auth_timeout_seconds: float = float(os.environ.get("AUTH_TIMEOUT_SECONDS", "5"))
    # KB 代理退出时转发给中心 logout 的固定 Origin（不转发任意来源）
    auth_forward_origin: str = os.environ.get("AUTH_FORWARD_ORIGIN", "")

    # 插件设备授权流程（docs/05 §4.5）
    device_auth_ttl_seconds: int = int(os.environ.get("DEVICE_AUTH_TTL_SECONDS", "300"))
    device_auth_poll_interval_seconds: int = int(os.environ.get("DEVICE_AUTH_POLL_INTERVAL_SECONDS", "2"))

    # Worker
    job_lease_seconds: int = int(os.environ.get("JOB_LEASE_SECONDS", "120"))
    job_max_lifetime_days: int = int(os.environ.get("JOB_MAX_LIFETIME_DAYS", "7"))
    worker_poll_seconds: float = float(os.environ.get("WORKER_POLL_SECONDS", "2"))
    cleanup_sweep_seconds: int = int(os.environ.get("CLEANUP_SWEEP_SECONDS", str(6 * 3600)))

    # 模型供应商（docs/02 §9.3：模型配置默认只允许经批准的 HTTPS origin）
    # 环境变量 PROVIDER_ALLOWED_ORIGINS：逗号分隔 host 列表；"*" 表示放行任意 HTTPS host（自部署自担风险）
    provider_allowed_origins: tuple[str, ...] = tuple(
        s.strip()
        for s in os.environ.get("PROVIDER_ALLOWED_ORIGINS", "").split(",")
        if s.strip()
    ) or (
        "api.openai.com",
        "api.deepseek.com",
        "api.moonshot.cn",
        "open.bigmodel.cn",
        "dashscope.aliyuncs.com",
        "api.siliconflow.cn",
        "openrouter.ai",
        "api.mistral.ai",
        "api.groq.com",
        "api.together.xyz",
        "api.anthropic.com",
        "api.x.ai",
    )

    def ensure_dirs(self) -> None:
        for d in (self.objects_dir, self.tmp_dir, self.deletions_dir, self.database_url_path()):
            d.mkdir(parents=True, exist_ok=True)

    def database_url_path(self) -> Path:
        # sqlite:///./data/db/app.db -> ./data/db
        prefix = "sqlite:///"
        if self.database_url.startswith(prefix):
            return Path(self.database_url[len(prefix):]).parent
        return Path(".")

    def load_master_key(self) -> bytes:
        """主密钥：32 字节，base64 或 hex 文本，或原始 32 字节文件。"""
        raw = self.master_key_file.read_bytes().strip()
        if len(raw) == 32:
            return raw
        import base64
        try:
            key = base64.urlsafe_b64decode(raw)
            if len(key) == 32:
                return key
        except Exception:
            pass
        key = bytes.fromhex(raw.decode("ascii"))
        if len(key) != 32:
            raise RuntimeError("MASTER_KEY 必须是 32 字节")
        return key


def get_settings() -> Settings:
    # 中心认证、对外地址与 ASR 配置在调用时读取环境变量：部署配置可随时调整，
    # 测试也能注入替身；其余字段沿用模块加载时的值（与既有行为一致）。
    return Settings(
        public_base_url=os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000"),
        auth_session_url=os.environ.get("AUTH_SESSION_URL", ""),
        auth_login_url=os.environ.get("AUTH_LOGIN_URL", ""),
        auth_logout_url=os.environ.get("AUTH_LOGOUT_URL", ""),
        auth_cookie_name=os.environ.get("AUTH_COOKIE_NAME", "__Secure-session"),
        auth_cookie_domain=os.environ.get("AUTH_COOKIE_DOMAIN", ""),
        auth_forward_origin=os.environ.get("AUTH_FORWARD_ORIGIN", ""),
        asr_enabled=os.environ.get("ASR_ENABLED", "false").lower() in ("1", "true", "yes"),
        asr_model=os.environ.get("ASR_MODEL", "dolphin"),
        asr_model_dir=Path(os.environ.get("ASR_MODEL_DIR", "/opt/models")),
        asr_engine_bin=os.environ.get("ASR_ENGINE_BIN", "/opt/sherpa/bin/sherpa-onnx-offline"),
        asr_ffmpeg_bin=os.environ.get("ASR_FFMPEG_BIN", "ffmpeg"),
        asr_threads=int(os.environ.get("ASR_THREADS", "1")),
        asr_chunk_seconds=int(os.environ.get("ASR_CHUNK_SECONDS", "20")),
        asr_chunk_context_seconds=float(os.environ.get("ASR_CHUNK_CONTEXT_SECONDS", "1")),
        asr_max_duration_seconds=int(os.environ.get("ASR_MAX_DURATION_SECONDS", "36000")),
        asr_max_input_bytes=int(os.environ.get("ASR_MAX_INPUT_BYTES", str(8 * 1024 * 1024 * 1024))),
        max_audio_upload_bytes=int(os.environ.get("MAX_AUDIO_UPLOAD_BYTES", str(8 * 1024 * 1024 * 1024))),
        audio_upload_chunk_bytes=int(os.environ.get("AUDIO_UPLOAD_CHUNK_BYTES", str(16 * 1024 * 1024))),
        audio_upload_session_ttl_hours=int(os.environ.get("AUDIO_UPLOAD_SESSION_TTL_HOURS", "24")),
        asr_tmp_ttl_hours=int(os.environ.get("ASR_TMP_TTL_HOURS", "24")),
        asr_chunk_timeout_seconds=int(os.environ.get("ASR_CHUNK_TIMEOUT_SECONDS", "900")),
        asr_idle_cpu_start=float(os.environ.get("ASR_IDLE_CPU_START", "0.25")),
        asr_idle_cpu_stop=float(os.environ.get("ASR_IDLE_CPU_STOP", "0.70")),
        asr_idle_hold_seconds=int(os.environ.get("ASR_IDLE_HOLD_SECONDS", "60")),
        asr_busy_cooldown_seconds=int(os.environ.get("ASR_BUSY_COOLDOWN_SECONDS", "120")),
        asr_idle_min_available_mib=int(os.environ.get("ASR_IDLE_MIN_AVAILABLE_MIB", "800")),
        asr_busy_min_available_mib=int(os.environ.get("ASR_BUSY_MIN_AVAILABLE_MIB", "256")),
    )
