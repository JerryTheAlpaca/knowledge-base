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
    CheckConstraint,
    DateTime,
    Float,
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
    # 40 字符：wechat_channels_session 等平台会话 kind 超过原 20（docs/18 §7.2）
    kind: Mapped[str] = mapped_column(String(40))  # llm|vision_ocr|bilibili_session|xiaohongshu_session|wechat_channels_session|zhihu_session
    adapter: Mapped[str] = mapped_column(String(40))  # openai-compatible|bilibili-web|...
    # llm 配置的角色：digest=整理文本（默认/NULL，历史行）、optimize=优化文本（纠错与分段）
    role: Mapped[str | None] = mapped_column(String(20), nullable=True)
    endpoint: Mapped[str] = mapped_column(String(512))
    model: Mapped[str] = mapped_column(String(120))
    capabilities_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # 去计费前的历史列（docs/05 §5.3）：生产库仍保留 NOT NULL 且无默认值，
    # 模型必须带 Python 默认值随 INSERT 写入，否则任何新建配置都会 IntegrityError
    prices_json: Mapped[dict] = mapped_column(JSON, default=dict)
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


class AudioUploadSession(Base, TimestampMixin):
    """音频大文件的分块续传会话（docs/13 §6.2）。

    - staging_path 是受控临时目录里的相对文件名，不使用用户路径；
    - offset 只按已确认块原子推进；同一 offset 同摘要重传幂等；
    - 完成时用 ObjectStore 的「同卷 staging 原子收纳」变成不可变对象并登记
      Upload，会话只保留 upload_id 供 Capture 引用；
    - 临时输入不是 object store 中的"已完成原件"，24 小时无活动过期。
    """

    __tablename__ = "audio_upload_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255), default="")
    mime: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    total_bytes: Mapped[int] = mapped_column(Integer)
    offset: Mapped[int] = mapped_column(Integer, default=0)
    chunk_size: Mapped[int] = mapped_column(Integer, default=16 * 1024 * 1024)
    staging_path: Mapped[str] = mapped_column(String(300))
    # 已完成块的累积摘要（增量 SHA-256），避免完成时二次读取整文件
    digest_state: Mapped[str] = mapped_column(String(64), default="")
    state: Mapped[str] = mapped_column(String(20), default="receiving")  # receiving|completed|cancelled|expired
    upload_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class AudioAsset(Base, TimestampMixin):
    """音频原件引用（docs/13 §6.3）：用户上传的原始材料不被当临时 PCM 清理。

    记录原件与条目/来源版本的绑定；ASR 取消、模型报错、空间等待、文字稿已
    投递都不解除引用。默认随有效条目保留，用户删除条目时才按删除流程清理。
    """

    __tablename__ = "audio_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    item_id: Mapped[str] = mapped_column(ForeignKey("items.id"), index=True)
    source_revision: Mapped[int] = mapped_column(Integer, default=1)
    upload_id: Mapped[str] = mapped_column(ForeignKey("uploads.id"), index=True)
    stored_file_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sha256: Mapped[str] = mapped_column(String(64))
    bytes: Mapped[int] = mapped_column(Integer, default=0)
    filename: Mapped[str] = mapped_column(String(255), default="")
    mime: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    role: Mapped[str] = mapped_column(String(40), default="original_audio")
    retention_state: Mapped[str] = mapped_column(String(20), default="retained")  # retained|released


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
    # 等待原因的机器码（login_required / deleted / blocked …）：界面动作与
    # 定向重排队按它判定，不解析 state_detail 的中文文案（审查 C-14）
    state_reason: Mapped[str] = mapped_column(String(32), default="")
    # 用户在网页下载原文文件时的 Bundle 版本；0 表示没有下载过。
    # 与 Obsidian 回执同样算作已经拿到手（docs/17 §5.2 的终态）。
    original_download_bundle: Mapped[int] = mapped_column(Integer, default=0)
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
    # 用户点名要整理（手动「开始整理」/「重新加工」）：为真时忽略「AI 自动整理」
    # 开关。自动入队的任务为假，按当时的开关决定做整理还是只做文字优化。
    digest_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 本次调用的固定输入与任务内 R 引用表（docs/24 §7）：进程重启后按同一绑定恢复，
    # 不能在中途把 R1 换成另一段原文（docs/23 §4.1 规则 4）。只存引用表与块边界，
    # 不存原文正文——原文在对象存储的固定版本里。
    input_json: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(20), default="queued")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    not_before: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ProviderOperation(Base, TimestampMixin):
    """模型调用执行状态（不含金额；docs/05 §5.2、docs/20 §11.5）。

    prepared=已规划未发送；sent=请求已发出（结果未知前不再自动重发）；
    succeeded / failed / unknown_outcome。中断恢复语义见 workers/enrich.py。
    分享任务用 share_run_id + step_key 归属（与 job_id 互斥），一轮里的整理、
    代码生成、修复、澄清各自可追踪；job_id 与 share_run_id 都为空仍是
    现有的模型连接测试。
    """

    __tablename__ = "provider_operations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"), index=True, nullable=True)  # 连接测试等无任务调用为空
    share_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("share_runs.id"), index=True, nullable=True
    )
    step_key: Mapped[str] = mapped_column(String(40), default="")  # clarify|synthesis|page_source|repair-N|...
    profile_id: Mapped[str | None] = mapped_column(ForeignKey("provider_profiles.id"), nullable=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    provider_task_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    state: Mapped[str] = mapped_column(String(30), default="prepared")  # prepared|sent|succeeded|failed|unknown_outcome
    detail: Mapped[str] = mapped_column(String(200), default="")  # 结束原因的简短说明（不含敏感信息）
    # 会话续接与缓存诊断（docs/20 §6.5.5）：把这次回复绑定到准确的历史版本。
    # usage_json 只含技术计数，不保存供应商原始响应与用户文本。
    conversation_id: Mapped[str | None] = mapped_column(
        ForeignKey("share_conversations.id"), index=True, nullable=True
    )
    context_epoch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_message_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prefix_hash: Mapped[str] = mapped_column(String(64), default="")
    usage_json: Mapped[dict] = mapped_column(JSON, default=dict)

    __table_args__ = (
        CheckConstraint(
            "not (job_id is not null and share_run_id is not null)",
            name="ck_provider_ops_single_owner",
        ),
    )


class LocalKeyBinding(Base, TimestampMixin):
    """线上 Key 向本人设备的下发绑定（docs/08 §8.3）。

    - 一条绑定关联（用户、设备、线上配置）；解绑只删除本机绑定，
      不替用户撤销线上或供应商 Key。
    - 只记录绑定关系与下发的配置/凭据版本，不保存明文 Key。
    - 服务端撤销绑定会阻止再次领取，但无法远程收回已下发的供应商 Key。
    """

    __tablename__ = "local_key_bindings"
    __table_args__ = (
        UniqueConstraint("user_id", "device_id", "profile_id", name="uq_local_binding"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"), index=True)
    profile_id: Mapped[str] = mapped_column(ForeignKey("provider_profiles.id"), index=True)
    # 最近一次成功下发的版本，用于「仅为该绑定更新版本」
    profile_version: Mapped[int] = mapped_column(Integer, default=0)
    credential_version: Mapped[int] = mapped_column(Integer, default=0)
    last_bound_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # revoked_at = 用户在本机解绑：允许再次绑定；
    # blocked_at = 服务端撤销绑定：阻止再次领取（docs/08 §8.3）
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    blocked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class DeviceAuthRequest(Base, TimestampMixin):
    """插件浏览器授权请求（docs/05 §4.5）：浏览器批准，插件轮询领取设备 Token。

    poll_secret 只存 SHA-256 摘要；browser_url 仅含 request_id；
    一条请求只能被批准一次，Token 领取时原子消费。
    """

    __tablename__ = "device_auth_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    device_name: Mapped[str] = mapped_column(String(120))
    poll_secret_hash: Mapped[str] = mapped_column(String(64))
    # 插件在授权时申请的额外权限（docs/08 §8.3）：仅允许 OPTIONAL_DEVICE_SCOPES 中的项
    requested_scopes_json: Mapped[list] = mapped_column(JSON, default=list)
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


class AsrRun(Base, TimestampMixin):
    """本地 ASR 任务级检查点（docs/11 §6.1）。

    - 唯一键 (user,item,revision,recipe)：切换模型产生新 recipe/run，
      两种模型的输出不混作同一次识别。
    - next_chunk_index 只在逐段结果原子落盘后的短事务中推进；恢复时校验
      文件摘要，仅重做未提交的段。
    - 处理清单一经提交（manifest_json）不可变。
    """

    __tablename__ = "asr_runs"
    __table_args__ = (
        UniqueConstraint("user_id", "item_id", "source_revision", "recipe_hash", name="uq_asr_run"),
        Index("ix_asr_runs_user_item", "user_id", "item_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    item_id: Mapped[str] = mapped_column(ForeignKey("items.id"), index=True)
    source_revision: Mapped[int] = mapped_column(Integer)
    recipe_hash: Mapped[str] = mapped_column(String(64))
    model_alias: Mapped[str] = mapped_column(String(40))   # dolphin | sense_voice
    model_id: Mapped[str] = mapped_column(String(120))     # 完整模型制品 ID
    state: Mapped[str] = mapped_column(String(20), default="queued")
    # queued|preparing|transcribing|paused|succeeded|failed|cancelled
    pause_reason: Mapped[str] = mapped_column(String(40), default="")
    # idle_wait|resource_busy|disabled|metrics_unavailable|idle_window_filling|
    # cpu_busy|memory_low|normal_jobs_active|selection_required|""（审查 C-26：排队期间暴露门禁原因）
    next_chunk_index: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    processed_seconds: Mapped[float] = mapped_column(Float, default=0.0)  # 已处理音频秒（累计）
    requested_by: Mapped[str] = mapped_column(String(20), default="manual")  # auto|manual
    work_dir: Mapped[str] = mapped_column(String(300), default="")  # 相对 tmp 的受控目录
    manifest_json: Mapped[dict] = mapped_column(JSON, default=dict)  # 已提交的 PCM 清单摘要
    # 冻结的本次输入（docs/13 §8）：只存来源稳定定位或对象引用，
    # 不存敏感 headers / 带时效签名的 URL。
    input_kind: Mapped[str] = mapped_column(String(20), default="remote")  # remote|object
    input_json: Mapped[dict] = mapped_column(JSON, default=dict)
    input_fingerprint: Mapped[str] = mapped_column(String(120), default="")
    last_error: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


# ---- 旧产物 → ContentDocument v3 迁移台账（docs/24 §7）----


class ContentMigration(Base, TimestampMixin):
    """一条旧提炼产物转成 v3 的记录。

    唯一键 (user_id, item_id, input_sha256, converter_version)：同一份旧输入用
    同一版转换器重复执行不再生成新 Bundle（docs/23 §8.1 第 7 步）。转换器改动
    版本号后允许重新转换，旧记录继续保留，旧 Bundle 与历史回执不修改。
    """

    __tablename__ = "content_migrations"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "item_id", "input_sha256", "converter_version",
            name="uq_content_migration_input",
        ),
        Index("ix_content_migrations_user_item", "user_id", "item_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    item_id: Mapped[str] = mapped_column(ForeignKey("items.id"), index=True)
    input_sha256: Mapped[str] = mapped_column(String(64))
    converter_version: Mapped[str] = mapped_column(String(40))
    new_bundle_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="complete")  # complete|partial|unresolved|failed
    notes: Mapped[str] = mapped_column(String(500), default="")


# ---- 通用 AI 整合与 HTML 分享（docs/20 §11）----
#
# 多篇材料的作品不借用 Item/Job：任务归属、对象引用与会话历史各有独立表，
# 避免「拿第一篇材料充当整份作品的归属」（docs/20 §2.1）。
# share_works 上的三个当前指针列不建外键：与子表互相引用会让 SQLite 无法定序
# 建表，一致性由提交时的短事务核对（归属 + 版本 + 未删除）保证。


class ShareWork(Base, TimestampMixin):
    """一件作品：归属、最新可用草稿、已发布版本与分享状态。"""

    __tablename__ = "share_works"
    __table_args__ = (Index("ix_share_works_user_status", "user_id", "share_status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    latest_ready_revision_id: Mapped[str | None] = mapped_column(String(36), nullable=True, default=None)
    published_revision_id: Mapped[str | None] = mapped_column(String(36), nullable=True, default=None)
    # 同一作品首版最多一个未结束创作任务（含等待用户回答；等待不占执行并发）
    active_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, default=None)
    version: Mapped[int] = mapped_column(Integer, default=1)  # 乐观锁：修改、发布、撤销、删除
    # 分享令牌：摘要用于核验，密文让作者能再次复制；都不是账号 Token
    share_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    share_token_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    share_token_dek: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    share_token_nonces_json: Mapped[dict] = mapped_column(JSON, default=dict)
    share_master_key_version: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    share_status: Mapped[str] = mapped_column(String(20), default="private")  # private|published|revoked
    share_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class ShareRun(Base, TimestampMixin):
    """一次创作任务：状态机、检查点与租约（docs/20 §11.2、§13.1）。

    waiting_user / awaiting_confirmation 不属于领取候选，也不计入执行并发；
    等待期间清空 lease_token/lease_until，回答后重新排队，不因久未回复自动推进。
    """

    __tablename__ = "share_runs"
    __table_args__ = (
        Index("ix_share_runs_state_not_before", "state", "not_before"),
        Index("ix_share_runs_user_work", "user_id", "work_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    work_id: Mapped[str] = mapped_column(ForeignKey("share_works.id"), index=True)
    base_revision_id: Mapped[str | None] = mapped_column(String(36), nullable=True, default=None)
    request_text: Mapped[str] = mapped_column(Text, default="")  # 私有用户要求，不进公开作品

    content_conversation_id: Mapped[str | None] = mapped_column(
        ForeignKey("share_conversations.id"), nullable=True, default=None
    )
    code_conversation_id: Mapped[str | None] = mapped_column(
        ForeignKey("share_conversations.id"), nullable=True, default=None
    )

    brief_version: Mapped[int] = mapped_column(Integer, default=0)
    brief_key: Mapped[str | None] = mapped_column(String(200), nullable=True, default=None)
    pending_round_key: Mapped[str | None] = mapped_column(String(200), nullable=True, default=None)
    pending_round_id: Mapped[str | None] = mapped_column(String(36), nullable=True, default=None)
    confirmed_brief_version: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    confirmation_kind: Mapped[str | None] = mapped_column(
        String(24), nullable=True, default=None
    )  # confirm|delegate_preferences|explicit_modify
    confirmation_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True, default=None)

    resume_stage: Mapped[str] = mapped_column(String(30), default="")
    resume_checkpoint_hash: Mapped[str] = mapped_column(String(64), default="")

    state: Mapped[str] = mapped_column(String(30), default="queued")
    # queued|running|waiting_user|awaiting_confirmation|retry_wait|waiting_resources|
    # waiting_key|succeeded|failed|cancelled|unknown_outcome
    stage: Mapped[str] = mapped_column(String(30), default="preparing")
    # preparing|clarifying|synthesizing|generating|packaging|checking|repairing
    reason_code: Mapped[str] = mapped_column(String(32), default="")

    input_manifest_key: Mapped[str] = mapped_column(String(200), default="")
    input_hash: Mapped[str] = mapped_column(String(64), default="")

    profile_id: Mapped[str | None] = mapped_column(ForeignKey("provider_profiles.id"), nullable=True, default=None)
    profile_version: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    model_config_json: Mapped[dict] = mapped_column(JSON, default=dict)  # 非秘密配置，不存明文 Key

    runtime_version: Mapped[str] = mapped_column(String(40), default="")
    prompt_version: Mapped[str] = mapped_column(String(40), default="")
    recipe_hash: Mapped[str] = mapped_column(String(64), default="")

    attempt: Mapped[int] = mapped_column(Integer, default=0)
    repair_count: Mapped[int] = mapped_column(Integer, default=0)
    not_before: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True, default=None)
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True, default=None)

    checkpoint_json: Mapped[dict] = mapped_column(JSON, default=dict)  # 已完成步骤的对象引用/哈希
    cancel_requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True, default=None)
    error_detail: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class ShareRevision(Base, TimestampMixin):
    """不可变的可用版本：只有打包并检查通过的才成为版本（docs/20 §11.3）。"""

    __tablename__ = "share_revisions"
    __table_args__ = (UniqueConstraint("work_id", "revision", name="uq_share_revision"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    work_id: Mapped[str] = mapped_column(ForeignKey("share_works.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    run_id: Mapped[str] = mapped_column(ForeignKey("share_runs.id"), index=True)
    base_revision_id: Mapped[str | None] = mapped_column(String(36), nullable=True, default=None)
    confirmed_brief_key: Mapped[str] = mapped_column(String(200), default="")  # 本版本依据的不可变需求摘要
    input_manifest_key: Mapped[str] = mapped_column(String(200), default="")
    synthesis_key: Mapped[str] = mapped_column(String(200), default="")
    source_code_key: Mapped[str] = mapped_column(String(200), default="")
    html_key: Mapped[str] = mapped_column(String(200), default="")
    html_sha256: Mapped[str] = mapped_column(String(64), default="")
    html_bytes: Mapped[int] = mapped_column(Integer, default=0)
    public_references_key: Mapped[str] = mapped_column(String(200), default="")
    check_report_key: Mapped[str] = mapped_column(String(200), default="")
    runtime_version: Mapped[str] = mapped_column(String(40), default="")


class ShareArtifact(Base, TimestampMixin):
    """分享对象的商品引用（docs/20 §11.4）：复用 ObjectStore，不创建虚假 Item。

    同一个物理对象可被多条引用持有；解除一条引用不等于删除物理文件。
    默认 private，只有最终白名单产物可被公开路由按 ID 读取。
    """

    __tablename__ = "share_artifacts"
    __table_args__ = (
        Index("ix_share_artifacts_work", "user_id", "work_id"),
        Index("ix_share_artifacts_sha", "sha256"),
        # 回收时每批都要按 key 复查还有没有别的登记（share_retention._still_referenced）
        Index("ix_share_artifacts_storage_key", "storage_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    work_id: Mapped[str] = mapped_column(ForeignKey("share_works.id"), index=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("share_runs.id"), nullable=True, default=None)
    revision_id: Mapped[str | None] = mapped_column(ForeignKey("share_revisions.id"), nullable=True, default=None)
    role: Mapped[str] = mapped_column(String(40))
    storage_key: Mapped[str] = mapped_column(String(200))
    sha256: Mapped[str] = mapped_column(String(64))
    bytes: Mapped[int] = mapped_column(Integer, default=0)
    mime: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    visibility: Mapped[str] = mapped_column(String(12), default="private")  # private|public
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True, default=None)


class ShareConversation(Base, TimestampMixin):
    """可续接的模型会话（docs/20 §11.6）：真实消息历史，不是不断覆盖的摘要。"""

    __tablename__ = "share_conversations"
    __table_args__ = (UniqueConstraint("work_id", "purpose", name="uq_share_conversation"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    work_id: Mapped[str] = mapped_column(ForeignKey("share_works.id"), index=True)
    purpose: Mapped[str] = mapped_column(String(16))  # content|code
    profile_id: Mapped[str | None] = mapped_column(ForeignKey("provider_profiles.id"), nullable=True, default=None)
    profile_version: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    api_protocol: Mapped[str] = mapped_column(String(32), default="openai-compatible-chat")
    # 换 profile/endpoint/model/提示版本或上下文压缩时开启新 epoch（旧供应商缓存不可复用）
    context_epoch: Mapped[int] = mapped_column(Integer, default=1)
    prefix_artifact_key: Mapped[str] = mapped_column(String(200), default="")
    prefix_hash: Mapped[str] = mapped_column(String(64), default="")
    last_message_seq: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)  # 追加消息的乐观锁
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class ShareMessage(Base):
    """一条真实消息：内容是私有对象，按 (会话, epoch, seq) 唯一续接。"""

    __tablename__ = "share_messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "context_epoch", "seq", name="uq_share_message_seq"),
        Index("ix_share_messages_user_conv", "user_id", "conversation_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("share_conversations.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("share_runs.id"), index=True)
    context_epoch: Mapped[int] = mapped_column(Integer, default=1)
    seq: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(16))  # system|user|assistant（由服务器判定，不采信客户端 role）
    content_key: Mapped[str] = mapped_column(String(200))
    # 供应商协议必须原样回传的续接块（如签名/opaque reasoning item）：仅适配器可见
    protocol_metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    reply_to_round_id: Mapped[str | None] = mapped_column(String(36), nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
