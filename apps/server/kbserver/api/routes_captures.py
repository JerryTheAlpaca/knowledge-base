"""Capture 创建（docs/02 §6.2、§10.2）。

- 幂等：同用户同键同请求摘要返回原响应；同键不同内容 409。
- 只在原始输入、附件引用、任务与初始版本记录都持久化后返回 202 durable=true。
- 文件先落盘（对象存储），数据库提交边界内发布初始原始材料 Bundle。
"""
from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..db import get_db
from ..domain import pipeline
from ..domain.errors import ApiError
from ..api.deps import require_scope
from ..models import Capture, Item, utcnow
from ..repositories import core as repo
from ..storage.objects import ObjectStore

router = APIRouter(prefix="/v1/captures", tags=["captures"])


class CaptureInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = Field(default="1.0")
    client_capture_id: str = Field(min_length=8, max_length=64)
    input_kind: str
    source_hint: str | None = "unknown"
    # 采集渠道（web_inbox / plugin / phone…）：只作渠道记录，不是平台（docs/13 §5.2）
    capture_channel: str | None = None
    # 网页/公众号正文图片：默认不下载，勾选后本次提取才保存配图（docs/02 §5.1）
    include_images: bool = False
    # 提取音轨：网页/公众号条目提取完成后自动排队转写；上传录音始终自动转写，
    # B 站沿用「无字幕自动转写」设置，均不受此开关影响
    include_asr: bool = False
    original_url: str | None = None
    share_text: str | None = None
    text: str | None = None
    user_note: str | None = None
    content_scope: str | None = "unknown"
    upload_ids: list[str] = Field(default_factory=list)
    # 音频转写意图与主体录音（docs/13 §8）：旧请求默认 default，语义不变
    processing_intent: str = "default"
    primary_audio_upload_id: str | None = None
    processing_profile_id: str | None = "profile-default"
    archive_policy: str = "source_materials"
    captured_at: datetime | None = None


class CaptureAccepted(BaseModel):
    capture_id: str
    item_id: str
    durable: bool
    pipeline_state: str
    received_at: datetime


@router.post("", response_model=CaptureAccepted, status_code=202)
def create_capture(
    body: CaptureInput,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal=Depends(require_scope("captures:create")),
    db: Session = Depends(get_db),
) -> CaptureAccepted:
    payload = body.model_dump(mode="json")
    result = create_capture_impl(db, user_id=principal.user.id, payload=payload,
                                 idempotency_key=idempotency_key)
    return CaptureAccepted(**result)


def create_capture_impl(db: Session, *, user_id: str, payload: dict,
                        idempotency_key: str | None) -> dict:
    """采集落库的实现体：HTTP 路由与 MCP 工具（投递笔记/草稿）共用同一条幂等路径。

    返回可直接构造 `CaptureAccepted` 的字典。幂等键缺失一律拒绝：模型重试、
    网络抖动和用户手抖都会走这里，没有幂等键就是重复建条目。
    """
    if not idempotency_key:
        raise ApiError("SCHEMA_INVALID", "缺少 Idempotency-Key", status_code=422)

    request_hash = pipeline.sha256_hex(pipeline.canonical_json(payload))

    existing = repo.idempotency_lookup(db, user_id, "POST /v1/captures", idempotency_key)
    if existing:
        if existing.request_hash != request_hash:
            raise ApiError("IDEMPOTENCY_CONFLICT", "同一幂等键对应不同请求内容", status_code=409)
        return dict(existing.response_json)

    uploads_index = {}
    wanted = list(payload.get("upload_ids") or [])
    if payload.get("primary_audio_upload_id"):
        wanted.append(payload["primary_audio_upload_id"])
    for uid in dict.fromkeys(wanted):
        up = repo.get_upload(db, user_id, uid)
        if up is None or up.state != "completed":
            raise ApiError("SCHEMA_INVALID", f"upload_id 不存在或未完成：{uid}")
        if up.expires_at and up.expires_at <= utcnow():
            raise ApiError("SCHEMA_INVALID", f"upload_id 已过期：{uid}")
        uploads_index[uid] = up

    # 逻辑 ID 已存在时不新建 Item（docs/02 §6.2）：即使幂等键不同
    from ..models import Capture as CaptureModel

    existing_capture = (
        db.query(CaptureModel)
        .filter(CaptureModel.user_id == user_id, CaptureModel.client_capture_id == payload["client_capture_id"])
        .one_or_none()
    )
    if existing_capture is not None:
        # capture 正常只对应一条 Item；意外多条/缺失时不抛 500，给明确错误（审查 C-24）
        item = db.query(Item).filter(Item.capture_id == existing_capture.id).one_or_none()
        if item is None:
            raise ApiError("NOT_FOUND", "该 capture 对应的条目不存在", status_code=404)
        result = {
            "capture_id": existing_capture.id,
            "item_id": item.id,
            "durable": True,
            "pipeline_state": item.pipeline_state,
            "received_at": existing_capture.received_at.isoformat(),
        }
        repo.idempotency_save(
            db, user_id, "POST /v1/captures", idempotency_key, request_hash, result, 202,
            ttl_days=repo_settings_ttl(),
        )
        return result

    pipeline.validate_capture_payload(payload, uploads_index)

    store = ObjectStore()
    capture, item = pipeline.create_capture(db, store, user_id=user_id, payload=payload, uploads=uploads_index)

    result = {
        "capture_id": capture.id,
        "item_id": item.id,
        "durable": True,
        "pipeline_state": item.pipeline_state,
        "received_at": capture.received_at.isoformat(),
    }
    repo.idempotency_save(
        db, user_id, "POST /v1/captures", idempotency_key, request_hash, result, 202,
        ttl_days=repo_settings_ttl(),
    )
    return result


def repo_settings_ttl() -> int:
    from ..config import get_settings
    return get_settings().idempotency_key_ttl_days
