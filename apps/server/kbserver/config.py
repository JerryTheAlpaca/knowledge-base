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

    # Worker
    job_lease_seconds: int = int(os.environ.get("JOB_LEASE_SECONDS", "120"))
    job_max_lifetime_days: int = int(os.environ.get("JOB_MAX_LIFETIME_DAYS", "7"))
    worker_poll_seconds: float = float(os.environ.get("WORKER_POLL_SECONDS", "2"))

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
    return Settings()
