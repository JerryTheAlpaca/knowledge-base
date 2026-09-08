"""SQLAlchemy 模型：docs/02 §7.2 核心表。

原则：
- 所有对象引用用 user_id + id 查询，复合唯一/外键保证不跨租户。
- Token/配对码只存 SHA-256 摘要。
- source_revisions / bundle_revisions 不可变；版本递增在短事务中完成。
- 二进制不入库，文件在对象存储，files 表做 file_id -> 对象登记。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """SQLite 不保留时区：写入统一转 UTC，读出补 UTC tzinfo。"""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default="active")  # active|disabled
    settings_json: Mapped[dict] = mapped_column(JSON, default=dict)  # 默认模型等本地偏好
    # 中心认证服务的不可变 user.id；SSO 首次登录时绑定，唯一约束收敛并发首次访问
    auth_subject: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)


class Device(Base, TimestampMixin):
    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # phone|desktop|web
    name: Mapped[str] = mapped_column(String(120))
    consumer_epoch: Mapped[int] = mapped_column(Integer, default=0)
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class Token(Base, TimestampMixin):
    __tablename__ = "tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)  # sha256 hex
    scopes_json: Mapped[list] = mapped_column(JSON, default=list)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class PairingCode(Base, TimestampMixin):
    __tablename__ = "pairing_codes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    device_kind: Mapped[str] = mapped_column(String(20))
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    scopes_json: Mapped[list] = mapped_column(JSON, default=list)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class ProviderProfile(Base, TimestampMixin):
    __tablename__ = "provider_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # llm|vision_ocr|bilibili_session
    adapter: Mapped[str] = mapped_column(String(40))  # openai-compatible|bilibili-web|...
    endpoint: Mapped[str] = mapped_column(String(512))
    model: Mapped[str] = mapped_column(String(120))
    capabilities_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # 适配器自有元数据（如 B 站登录态最近检测结果）；不再存价格（docs/05 §5）
    meta_json: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)


class Credential(Base, TimestampMixin):
    __tablename__ = "credentials"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    profile_id: Mapped[str] = mapped_column(ForeignKey("provider_profiles.id"), index=True)
    encrypted_secret: Mapped[str] = mapped_column(Text)  # base64 密文
    encrypted_dek: Mapped[str] = mapped_column(Text)
    nonces_json: Mapped[dict] = mapped_column(JSON, default=dict)
    master_key_version: Mapped[int] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, default=1)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class Capture(Base, TimestampMixin):
    __tablename__ = "captures"
    __table_args__ = (UniqueConstraint("user_id", "client_capture_id", name="uq_capture_client"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    client_capture_id: Mapped[str] = mapped_column(String(64))
    request_hash: Mapped[str] = mapped_column(String(64))
    input_json: Mapped[dict] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Upload(Base, TimestampMixin):
    __tablename__ = "uploads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    state: Mapped[str] = mapped_column(String(20), default="completed")  # receiving|completed|expired
    storage_key: Mapped[str] = mapped_column(String(200))
    filename: Mapped[str] = mapped_column(String(255), default="")
    sha256: Mapped[str] = mapped_column(String(64))
    bytes: Mapped[int] = mapped_column(Integer)
    mime: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class Item(Base, TimestampMixin):
    __tablename__ = "items"
    __table_args__ = (
        UniqueConstraint("user_id", "capture_id", name="uq_item_capture"),
        Index("ix_items_user_state", "user_id", "pipeline_state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    capture_id: Mapped[str] = mapped_column(ForeignKey("captures.id"))
    source_revision: Mapped[int] = mapped_column(Integer, default=1)
    bundle_revision: Mapped[int] = mapped_column(Integer, default=0)  # 最新已发布 Bundle
    pipeline_state: Mapped[str] = mapped_column(String(30), default="queued")
    state_detail: Mapped[str] = mapped_column(String(200), default="")
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class SourceRevision(Base):
    """不可变来源版本。"""

    __tablename__ = "source_revisions"
    __table_args__ = (UniqueConstraint("user_id", "item_id", "revision", name="uq_source_rev"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    item_id: Mapped[str] = mapped_column(ForeignKey("items.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    artifacts_json: Mapped[dict] = mapped_column(JSON, default=dict)  # file_id 列表及角色
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class BundleRevision(Base):
    """不可变投递快照。"""

    __tablename__ = "bundle_revisions"
    __table_args__ = (
        UniqueConstraint("user_id", "item_id", "revision", name="uq_bundle_rev"),
        Index("ix_bundle_user_item", "user_id", "item_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    item_id: Mapped[str] = mapped_column(ForeignKey("items.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    source_revision: Mapped[int] = mapped_column(Integer)
    manifest_key: Mapped[str] = mapped_column(String(200))  # 对象存储 key
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    processing_state: Mapped[str] = mapped_column(String(30), default="original_only")
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class StoredFile(Base):
    """file_id -> 不可变对象登记。下载只按登记对象查询，不接受任意路径。"""

    __tablename__ = "files"
    __table_args__ = (UniqueConstraint("user_id", "file_id", name="uq_file_user"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    file_id: Mapped[str] = mapped_column(String(64))  # 清单内稳定标识
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    item_id: Mapped[str] = mapped_column(ForeignKey("items.id"), index=True)
    role: Mapped[str] = mapped_column(String(40))  # original_submission|source_material|generated|preview
    relative_path: Mapped[str] = mapped_column(String(300))
    mime: Mapped[str] = mapped_column(String(120))
    bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Job(Base, TimestampMixin):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("user_id", "item_id", "source_revision", "stage", "recipe_hash", name="uq_job"),
        Index("ix_jobs_state_not_before", "state", "not_before"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    item_id: Mapped[str] = mapped_column(ForeignKey("items.id"), index=True)
    source_revision: Mapped[int] = mapped_column(Integer)
    stage: Mapped[str] = mapped_column(String(40))  # extract|enrich
    recipe_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(20), default="queued")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    not_before: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ProviderOperation(Base, TimestampMixin):
    """模型调用执行状态（不含金额；docs/05 §5.2）。

    prepared=已规划未发送；sent=请求已发出（结果未知前不再自动重发）；
    succeeded / failed / unknown_outcome。中断恢复语义见 workers/enrich.py。
    """

    __tablename__ = "provider_operations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"), index=True, nullable=True)  # 连接测试等无任务调用为空
    profile_id: Mapped[str | None] = mapped_column(ForeignKey("provider_profiles.id"), nullable=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    provider_task_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    state: Mapped[str] = mapped_column(String(30), default="prepared")  # prepared|sent|succeeded|failed|unknown_outcome
    detail: Mapped[str] = mapped_column(String(200), default="")  # 结束原因的简短说明（不含敏感信息）


class DeviceAuthRequest(Base, TimestampMixin):
    """插件浏览器授权请求（docs/05 §4.5）：浏览器批准，插件轮询领取设备 Token。

    poll_secret 只存 SHA-256 摘要；browser_url 仅含 request_id；
    一条请求只能被批准一次，Token 领取时原子消费。
    """

    __tablename__ = "device_auth_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    device_name: Mapped[str] = mapped_column(String(120))
    poll_secret_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(20), default="pending")  # pending|approved|consumed|expired|cancelled
    # 批准时绑定的中心账号与本地用户
    auth_subject: Mapped[str | None] = mapped_column(String(64), nullable=True)
    central_username: Mapped[str | None] = mapped_column(String(120), nullable=True)
    local_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    device_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # 批准时创建，领取后回填
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class Receipt(Base, TimestampMixin):
    __tablename__ = "receipts"
    __table_args__ = (
        UniqueConstraint("user_id", "item_id", "bundle_revision", "device_id", name="uq_receipt"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    item_id: Mapped[str] = mapped_column(ForeignKey("items.id"), index=True)
    bundle_revision: Mapped[int] = mapped_column(Integer)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"), index=True)
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    local_commit_id: Mapped[str] = mapped_column(String(120), default="")
    received_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (Index("ix_events_user_seq", "user_id", "seq"),)

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    item_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    bundle_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event_type: Mapped[str] = mapped_column(String(40))
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class IdempotencyRecord(Base, TimestampMixin):
    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("user_id", "endpoint", "key_hash", name="uq_idem"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    endpoint: Mapped[str] = mapped_column(String(120))
    key_hash: Mapped[str] = mapped_column(String(64))
    request_hash: Mapped[str] = mapped_column(String(64))
    response_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status_code: Mapped[int] = mapped_column(Integer, default=200)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())


class SuppressedItem(Base):
    """本机删除笔记后的 suppression：停止自动复建（docs/02 §8.2）。"""

    __tablename__ = "suppressed_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    item_id: Mapped[str] = mapped_column(ForeignKey("items.id"), index=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
