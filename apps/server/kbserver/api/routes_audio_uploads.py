"""音频大文件上传：最小串行分块续传（docs/13 §6.2）与原件下载（§6.3）。

传输机制，不是通用上传服务：
- POST   /v1/audio-uploads                     声明 filename/总字节/mime，创建会话
- GET    /v1/audio-uploads/{id}                返回可恢复 offset/state（仅本人）
- PUT    /v1/audio-uploads/{id}/chunks         原始二进制块（X-Upload-Offset + X-Chunk-Sha256）
- POST   /v1/audio-uploads/{id}/complete       长度一致后对象化，返回标准 UploadResult
- DELETE /v1/audio-uploads/{id}                取消未完成会话（已完成对象按引用规则处理）
- GET    /v1/items/{id}/audio-original         鉴权流式下载上传原件（不进 Bundle 自动投递）

崩溃恢复：块写盘与 offset 更新之间中断时，按最后确认 offset 截去未提交尾部再重传；
完成阶段计算摘要与对象化都在事务外执行，进程中断可重试完成。
"""
from __future__ import annotations

import hashlib
import shutil
from datetime import timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..domain.errors import ApiError
from ..models import AudioAsset, AudioUploadSession, Upload, utcnow
from ..repositories import core as repo
from ..api.deps import require_scope
from ..storage.objects import ObjectStore

router = APIRouter(tags=["audio-uploads"])

# 预留余量：本次约 1.1GiB PCM + 结果与运行空间（docs/13 §7.2）
DISK_RESERVE_BYTES = 2 * 1024 * 1024 * 1024


class AudioUploadResult(BaseModel):
    upload_id: str
    sha256: str
    bytes: int
    mime: str
    filename: str


class AudioUploadSessionOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    state: str
    filename: str
    mime: str
    total_bytes: int
    offset: int
    chunk_size: int
    upload_id: str | None
    expires_at: str | None
    # 同时如实显示时长/字节上限（docs/13 §7.1 UI 提示）
    max_duration_seconds: int
    max_audio_bytes: int


class AudioUploadCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str
    total_bytes: int
    mime: str = "application/octet-stream"


def _session_out(sess: AudioUploadSession) -> AudioUploadSessionOut:
    settings = get_settings()
    return AudioUploadSessionOut(
        session_id=sess.id, state=sess.state, filename=sess.filename, mime=sess.mime,
        total_bytes=sess.total_bytes, offset=sess.offset, chunk_size=sess.chunk_size,
        upload_id=sess.upload_id,
        expires_at=sess.expires_at.isoformat() if sess.expires_at else None,
        max_duration_seconds=settings.asr_max_duration_seconds,
        max_audio_bytes=settings.max_audio_upload_bytes,
    )


def _require_session(db: Session, user_id: str, session_id: str) -> AudioUploadSession:
    sess = db.query(AudioUploadSession).filter(
        AudioUploadSession.id == session_id, AudioUploadSession.user_id == user_id
    ).one_or_none()
    if sess is None:
        raise ApiError("NOT_FOUND", "上传会话不存在", status_code=404)
    return sess


def _touch(sess: AudioUploadSession, settings) -> None:
    sess.expires_at = utcnow() + timedelta(hours=settings.audio_upload_session_ttl_hours)
    sess.updated_at = utcnow()


@router.post("/v1/audio-uploads", response_model=AudioUploadSessionOut, status_code=201)
def create_audio_upload(body: AudioUploadCreate,
                        principal=Depends(require_scope("uploads:create")),
                        db: Session = Depends(get_db)) -> AudioUploadSessionOut:
    user = principal.user
    settings = get_settings()
    if body.total_bytes <= 0:
        raise ApiError("SCHEMA_INVALID", "total_bytes 必须为正数", status_code=422)
    if body.total_bytes > settings.max_audio_upload_bytes:
        raise ApiError(
            "PAYLOAD_TOO_LARGE",
            f"单文件上限 {settings.max_audio_upload_bytes} 字节（{settings.max_audio_upload_bytes // (1024**3)}GiB）",
            status_code=413,
        )
    # 磁盘：原件预留 + 本次 PCM 约 1.1GiB + 结果与余量（docs/13 §7.2）
    store = ObjectStore()
    usage = shutil.disk_usage(str(store.tmp_dir))
    if usage.free < body.total_bytes + DISK_RESERVE_BYTES:
        raise ApiError("INSUFFICIENT_STORAGE", "服务器磁盘空间不足，暂时无法接收该录音", status_code=507)

    staging = store.new_staging_path()
    store.staging_file(staging).touch()
    sess = AudioUploadSession(
        user_id=user.id, filename=(body.filename or "audio.bin")[:255], mime=body.mime[:120],
        total_bytes=body.total_bytes, offset=0, chunk_size=settings.audio_upload_chunk_bytes,
        staging_path=staging,
    )
    _touch(sess, settings)
    db.add(sess)
    db.commit()
    db.refresh(sess)
    return _session_out(sess)


@router.get("/v1/audio-uploads/{session_id}", response_model=AudioUploadSessionOut)
def get_audio_upload(session_id: str, principal=Depends(require_scope("uploads:create")),
                     db: Session = Depends(get_db)) -> AudioUploadSessionOut:
    return _session_out(_require_session(db, principal.user.id, session_id))


@router.put("/v1/audio-uploads/{session_id}/chunks", response_model=AudioUploadSessionOut)
async def put_audio_chunk(session_id: str, request: Request,
                          principal=Depends(require_scope("uploads:create")),
                          db: Session = Depends(get_db)) -> AudioUploadSessionOut:
    """一次一个块；同一 offset 同摘要重传幂等，不同摘要冲突，不接受跳段/重叠。"""
    user = principal.user
    settings = get_settings()
    sess = _require_session(db, user.id, session_id)
    if sess.state != "receiving":
        raise ApiError("CONFLICT", f"会话状态为 {sess.state}，不能继续写入", status_code=409)

    raw_offset = request.headers.get("X-Upload-Offset")
    chunk_sha = (request.headers.get("X-Chunk-Sha256") or "").strip().lower()
    if raw_offset is None or not chunk_sha:
        raise ApiError("SCHEMA_INVALID", "缺少 X-Upload-Offset 或 X-Chunk-Sha256", status_code=422)
    try:
        offset = int(raw_offset)
    except ValueError:
        raise ApiError("SCHEMA_INVALID", "X-Upload-Offset 必须是整数", status_code=422) from None
    if len(chunk_sha) != 64:
        raise ApiError("SCHEMA_INVALID", "X-Chunk-Sha256 必须是 64 位十六进制", status_code=422)

    if offset != sess.offset:
        raise ApiError(
            "CONFLICT",
            f"块偏移不匹配：当前可恢复 offset 为 {sess.offset}",
            status_code=409,
            details={"expected_offset": sess.offset},
        )

    store = ObjectStore()
    # 恢复：截去上次崩溃留下的未提交尾部，再追加本块
    store.truncate_staging(sess.staging_path, sess.offset)

    digest = hashlib.sha256()
    written = sess.offset
    over = False
    with open(store.staging_file(sess.staging_path), "ab") as out:
        async for chunk in request.stream():
            if not chunk:
                continue
            digest.update(chunk)
            written += len(chunk)
            if written > sess.total_bytes:
                over = True
                break
            out.write(chunk)
        out.flush()
    if over:
        store.truncate_staging(sess.staging_path, sess.offset)
        raise ApiError("PAYLOAD_TOO_LARGE", "块超出声明的总字节数", status_code=413)
    if digest.hexdigest() != chunk_sha:
        store.truncate_staging(sess.staging_path, sess.offset)
        raise ApiError("SCHEMA_INVALID", "块摘要校验失败，请重传该块", status_code=422)

    prev = sess.digest_state or ""
    sess.digest_state = hashlib.sha256((prev + chunk_sha).encode("ascii")).hexdigest()
    sess.offset = written
    _touch(sess, settings)
    db.commit()
    db.refresh(sess)
    return _session_out(sess)


def _file_sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


@router.post("/v1/audio-uploads/{session_id}/complete", response_model=AudioUploadResult)
def complete_audio_upload(session_id: str, principal=Depends(require_scope("uploads:create")),
                          db: Session = Depends(get_db)) -> AudioUploadResult:
    """长度一致后分块计算整文件 SHA-256，并把 staging 原子收纳为不可变对象。"""
    user = principal.user
    settings = get_settings()
    sess = _require_session(db, user.id, session_id)

    if sess.state == "completed" and sess.upload_id:
        up = repo.get_upload(db, user.id, sess.upload_id)
        if up is not None:  # 幂等：重复完成返回同一 Upload
            return AudioUploadResult(upload_id=up.id, sha256=up.sha256, bytes=up.bytes,
                                     mime=up.mime, filename=up.filename)
    if sess.state != "receiving":
        raise ApiError("CONFLICT", f"会话状态为 {sess.state}，不能完成", status_code=409)
    if sess.offset != sess.total_bytes:
        raise ApiError(
            "CONFLICT",
            f"尚未收齐：已确认 {sess.offset}/{sess.total_bytes} 字节",
            status_code=409,
            details={"expected_offset": sess.offset},
        )

    store = ObjectStore()
    staging = store.staging_file(sess.staging_path)
    if not staging.exists():
        raise ApiError("NOT_FOUND", "上传临时文件已丢失，请重新上传", status_code=409)
    # 摘要计算在事务外（可能耗时）；进程中断可重试完成
    sha = _file_sha256(staging)
    _sha, key, size = store.adopt_staging(sess.staging_path, sha)

    up = Upload(
        user_id=user.id, state="completed", storage_key=key,
        filename=sess.filename or "audio.bin", sha256=sha, bytes=size, mime=sess.mime,
        expires_at=utcnow() + timedelta(hours=settings.unreferenced_upload_ttl_hours),
    )
    db.add(up)
    db.flush()
    sess.state = "completed"
    sess.upload_id = up.id
    sess.sha256 = sha
    _touch(sess, settings)
    db.commit()
    return AudioUploadResult(upload_id=up.id, sha256=sha, bytes=size,
                             mime=up.mime, filename=up.filename)


@router.delete("/v1/audio-uploads/{session_id}")
def cancel_audio_upload(session_id: str, principal=Depends(require_scope("uploads:create")),
                        db: Session = Depends(get_db)) -> dict:
    """取消未完成会话释放临时占用；已完成对象按引用规则处理，不删共享对象。"""
    user = principal.user
    sess = _require_session(db, user.id, session_id)
    if sess.state == "receiving":
        ObjectStore().discard_staging(sess.staging_path)
        sess.state = "cancelled"
        db.commit()
        return {"session_id": sess.id, "cancelled": True, "released_staging": True}
    return {"session_id": sess.id, "cancelled": False,
            "note": "会话已完成，对象按引用规则保留，请通过条目删除流程处理。"}


@router.get("/v1/items/{item_id}/audio-original")
def download_audio_original(item_id: str, principal=Depends(require_scope("items:read")),
                            db: Session = Depends(get_db)):
    """鉴权流式下载上传原件：只按登记的 AudioAsset 定位，不暴露服务器路径。"""
    user = principal.user
    item = repo.get_item(db, user.id, item_id)
    if item is None:
        raise ApiError("NOT_FOUND", "条目不存在", status_code=404)
    asset = (
        db.query(AudioAsset)
        .filter(AudioAsset.user_id == user.id, AudioAsset.item_id == item.id,
                AudioAsset.retention_state == "retained")
        .order_by(AudioAsset.created_at.desc())
        .first()
    )
    if asset is None:
        raise ApiError("NOT_FOUND", "该条目没有保留的上传原件", status_code=404)
    up = repo.get_upload(db, user.id, asset.upload_id)
    if up is None or up.state != "completed":
        raise ApiError("NOT_FOUND", "原件引用已失效", status_code=404)
    store = ObjectStore()
    path = store.object_path(up.storage_key)
    if not path.exists():
        raise ApiError("NOT_FOUND", "原件在对象存储中缺失", status_code=404)

    def _iter():
        with open(path, "rb") as f:
            while True:
                block = f.read(1024 * 1024)
                if not block:
                    break
                yield block

    filename = (asset.filename or up.filename or "audio.bin").replace('"', "")
    ascii_name = filename.encode("ascii", "ignore").decode("ascii") or "audio.bin"
    quoted = quote(filename, safe="")
    return StreamingResponse(
        _iter(), media_type=up.mime or "application/octet-stream",
        headers={"Content-Length": str(up.bytes),
                 # HTTP 头只能 latin-1：非 ASCII 文件名按 RFC 5987 编码，附 ASCII 兜底
                 "Content-Disposition":
                     f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quoted}'},
    )
