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
    original_url: str | None = None
    share_text: str | None = None
    text: str | None = None
    user_note: str | None = None
    content_scope: str | None = "unknown"
    upload_ids: list[str] = Field(default_factory=list)
    processing_profile_id: str | None = "profile-default"
    archive_policy: str = "source_materials"
    captured_at: datetime | None = None


class CaptureAccepted(BaseModel):
    capture_id: str
    item_id: str
    durable: bool
    pipeline_state: str
    received_at: datetime


def _parse_captured_at(value: str | None) -> str | None:
    if not value:
        return None
    return value


@router.post("", response_model=CaptureAccepted, status_code=202)
def create_capture(
    body: CaptureInput,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal=Depends(require_scope("captures:create")),
    db: Session = Depends(get_db),
) -> CaptureAccepted:
    user, _device, _token = principal
    if not idempotency_key:
        raise ApiError("SCHEMA_INVALID", "缺少 Idempotency-Key 请求头", status_code=422)

    payload = body.model_dump(mode="json")
    request_hash = pipeline.sha256_hex(pipeline.canonical_json(payload))

    existing = repo.idempotency_lookup(db, user.id, "POST /v1/captures", idempotency_key)
    if existing:
        if existing.request_hash != request_hash:
            raise ApiError("IDEMPOTENCY_CONFLICT", "同一幂等键对应不同请求内容", status_code=409)
        return CaptureAccepted(**existing.response_json)

    uploads_index = {}
    for uid in payload.get("upload_ids") or []:
        up = repo.get_upload(db, user.id, uid)
        if up is None or up.state != "completed":
            raise ApiError("SCHEMA_INVALID", f"upload_id 不存在或未完成：{uid}")
        if up.expires_at and up.expires_at <= utcnow():
            raise ApiError("SCHEMA_INVALID", f"upload_id 已过期：{uid}")
        uploads_index[uid] = up

    # 逻辑 ID 已存在时不新建 Item（docs/02 §6.2）：即使幂等键不同
    from ..models import Capture as CaptureModel

    existing_capture = (
        db.query(CaptureModel)
        .filter(CaptureModel.user_id == user.id, CaptureModel.client_capture_id == payload["client_capture_id"])
        .one_or_none()
    )
    if existing_capture is not None:
        item = db.query(Item).filter(Item.capture_id == existing_capture.id).one()
        repo.idempotency_save(
            db, user.id, "POST /v1/captures", idempotency_key, request_hash,
            {
                "capture_id": existing_capture.id,
                "item_id": item.id,
                "durable": True,
                "pipeline_state": item.pipeline_state,
                "received_at": existing_capture.received_at.isoformat(),
            },
            202,
            ttl_days=repo_settings_ttl(),
        )
        return CaptureAccepted(
            capture_id=existing_capture.id,
            item_id=item.id,
            durable=True,
            pipeline_state=item.pipeline_state,
            received_at=existing_capture.received_at,
        )

    pipeline.validate_capture_payload(payload, uploads_index)

    captured_at = _parse_captured_at(payload.get("captured_at"))
    if captured_at:
        payload["captured_at"] = captured_at

    store = ObjectStore()
    capture, item = pipeline.create_capture(db, store, user_id=user.id, payload=payload, uploads=uploads_index)

    result = {
        "capture_id": capture.id,
        "item_id": item.id,
        "durable": True,
        "pipeline_state": item.pipeline_state,
        "received_at": capture.received_at.isoformat(),
    }
    repo.idempotency_save(
        db, user.id, "POST /v1/captures", idempotency_key, request_hash, result, 202,
        ttl_days=repo_settings_ttl(),
    )
    return CaptureAccepted(**result)


def repo_settings_ttl() -> int:
    from ..config import get_settings
    return get_settings().idempotency_key_ttl_days
