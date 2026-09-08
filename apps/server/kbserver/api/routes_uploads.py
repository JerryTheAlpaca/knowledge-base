"""单文件 multipart 上传（docs/02 §10.1）：流式写临时文件、校验后落对象存储，返回 SHA-256 与大小。

未被 Capture 引用的上传由清理任务按 24 小时过期（worker.cleanup）。
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, File, Header, UploadFile as FastapiUploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..domain.errors import ApiError
from ..api.deps import require_scope
from ..models import Upload, utcnow
from ..repositories import core as repo
from ..storage.objects import ObjectStore, PayloadTooLarge

router = APIRouter(prefix="/v1/uploads", tags=["uploads"])


class UploadResult(BaseModel):
    upload_id: str
    sha256: str
    bytes: int
    mime: str
    filename: str


@router.post("", response_model=UploadResult, status_code=201)
def upload(
    file: FastapiUploadFile = File(...),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal=Depends(require_scope("uploads:create")),
    db: Session = Depends(get_db),
) -> UploadResult:
    user = principal.user
    settings = get_settings()
    store = ObjectStore()

    # 幂等：同键同内容摘要直接返回原上传
    if idempotency_key:
        existing = repo.idempotency_lookup(db, user.id, "POST /v1/uploads", idempotency_key)
        if existing and existing.response_json.get("sha256"):
            up = repo.get_upload(db, user.id, existing.response_json["upload_id"])
            if up:
                return UploadResult(upload_id=up.id, sha256=up.sha256, bytes=up.bytes, mime=up.mime, filename=up.filename)

    try:
        sha, key, size = store.put_stream(file.file, max_bytes=settings.max_single_file_bytes)
    except PayloadTooLarge as exc:
        raise ApiError("PAYLOAD_TOO_LARGE", str(exc), status_code=413) from exc

    mime = file.content_type or "application/octet-stream"
    if mime.startswith("image/") and size > settings.max_image_bytes:
        raise ApiError("PAYLOAD_TOO_LARGE", f"单图上限 {settings.max_image_bytes} 字节", status_code=413)

    up = Upload(
        user_id=user.id,
        state="completed",
        storage_key=key,
        filename=(file.filename or "attachment.bin")[:255],
        sha256=sha,
        bytes=size,
        mime=mime,
        expires_at=utcnow() + timedelta(hours=settings.unreferenced_upload_ttl_hours),
    )
    db.add(up)
    db.flush()

    if idempotency_key:
        repo.idempotency_save(
            db, user.id, "POST /v1/uploads", idempotency_key, sha,
            {"upload_id": up.id, "sha256": sha, "bytes": size}, 201,
            ttl_days=settings.idempotency_key_ttl_days,
        )
    return UploadResult(upload_id=up.id, sha256=sha, bytes=size, mime=mime, filename=up.filename)
