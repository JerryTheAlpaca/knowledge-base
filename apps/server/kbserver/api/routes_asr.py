"""本地 ASR 接口（docs/11 §9.2）。

- GET/PUT /v1/asr-settings：用户级「无字幕时自动转写」开关；deployment_enabled
  只读返回（部署开关 + 默认模型可用性），用户不能通过 API 打开部署开关。
- POST /v1/items/{id}/asr：手动触发/重试；只接受模型别名，不接受路径或参数。
- GET /v1/items/{id}/asr：当前 run 的阶段、完成段数、暂停原因（不暴露服务器路径）。
- POST /v1/items/{id}/asr/cancel：取消并终止执行中的子进程（由 Worker 监测执行）。

用户隔离：全部按 principal.user.id 过滤；他人条目一律 404。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..api.deps import require_scope
from ..domain import pipeline
from ..domain.errors import ApiError
from ..models import AsrRun, Item, SourceRevision, User, utcnow
from ..repositories import core as repo
from ..workers import asr as asr_stage

router = APIRouter(tags=["asr"])


def _require_bilibili_item(db: Session, user_id: str, item_id: str) -> Item:
    item = repo.get_item(db, user_id, item_id)
    if item is None:
        raise ApiError("NOT_FOUND", "条目不存在", status_code=404)
    if item.deleted_at is not None:
        raise ApiError("GONE", "条目已删除", status_code=410)
    from .routes_bilibili import _is_bilibili_item

    if not _is_bilibili_item(db, item):
        raise ApiError("SCHEMA_INVALID", "只有 B 站条目支持音频转写", status_code=422)
    return item


def _latest_run(db: Session, item: Item) -> AsrRun | None:
    return (
        db.query(AsrRun)
        .filter(AsrRun.user_id == item.user_id, AsrRun.item_id == item.id)
        .order_by(AsrRun.updated_at.desc(), AsrRun.created_at.desc())
        .first()
    )


def _run_out(run: AsrRun | None) -> dict:
    settings = get_settings()
    if run is None:
        return {"exists": False, "deployment_enabled": asr_stage.asr_enabled(settings),
                "default_model": settings.asr_model,
                "deployed_models": asr_stage.deployed_aliases()}
    return {
        "exists": True,
        "run_id": run.id,
        "state": run.state,
        "pause_reason": run.pause_reason,
        "done_chunks": run.next_chunk_index,
        "chunk_count": run.chunk_count,
        "processed_audio_seconds": round(float(run.processed_seconds or 0.0), 1),
        "model_alias": run.model_alias,
        "model_id": run.model_id,
        "requested_by": run.requested_by,
        "failed_count": run.failed_count,
        "last_error": run.last_error or None,
        "updated_at": run.updated_at.isoformat(),
        "deployment_enabled": asr_stage.asr_enabled(settings),
        "default_model": settings.asr_model,
        "deployed_models": asr_stage.deployed_aliases(),
    }


class AsrSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    auto_when_no_track: bool


class AsrTriggerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    retry_model: str | None = None  # 只允许别名：dolphin | sense_voice


@router.get("/v1/asr-settings")
def get_asr_settings(principal=Depends(require_scope("profiles:manage")),
                     db: Session = Depends(get_db)):
    settings = get_settings()
    user: User = principal.user
    pref = (user.settings_json or {}).get("asr") or {}
    return {
        "auto_when_no_track": bool(pref.get("auto_when_no_track")),
        "deployment_enabled": asr_stage.asr_enabled(settings),
        "default_model": settings.asr_model,
        "deployed_models": asr_stage.deployed_aliases(),
        "models": [
            {"alias": alias, "model_id": asr_stage.ASR_MODELS[alias],
             "deployed": asr_stage.model_available(alias)}
            for alias in asr_stage.ASR_MODELS
        ],
        "note": "开启后：B 站确认无字幕轨的条目会在服务器空闲时自动音频转写（机器转写，未经人工校对）。",
    }


@router.put("/v1/asr-settings")
def put_asr_settings(body: AsrSettingsIn, principal=Depends(require_scope("profiles:manage")),
                     db: Session = Depends(get_db)):
    user: User = principal.user
    settings_json = dict(user.settings_json or {})
    asr_pref = dict(settings_json.get("asr") or {})
    asr_pref["auto_when_no_track"] = body.auto_when_no_track
    settings_json["asr"] = asr_pref
    user.settings_json = settings_json
    db.commit()
    db.refresh(user)
    pipeline.emit_event(db, user.id, item_id=None, bundle_revision=None,
                        event_type="asr_settings_changed",
                        payload={"auto_when_no_track": body.auto_when_no_track})
    db.commit()
    return {"auto_when_no_track": bool((user.settings_json or {}).get("asr", {}).get("auto_when_no_track")),
            "deployment_enabled": asr_stage.asr_enabled(get_settings())}


@router.post("/v1/items/{item_id}/asr")
def trigger_asr(item_id: str, body: AsrTriggerIn | None = None,
                principal=Depends(require_scope("items:edit")),
                db: Session = Depends(get_db)):
    """手动触发音频转写；模型别名幂等，重复点击不重复建任务。"""
    item = _require_bilibili_item(db, principal.user.id, item_id)
    settings = get_settings()
    if not asr_stage.asr_enabled(settings):
        raise ApiError("FORBIDDEN", "服务端未启用本地 ASR（部署开关未开启）", status_code=403)
    alias = (body.retry_model if body else None) or settings.asr_model
    if alias not in asr_stage.ASR_MODELS:
        raise ApiError("SCHEMA_INVALID", f"未知模型别名：{alias}", status_code=422)
    if not asr_stage.model_available(alias):
        raise ApiError("SCHEMA_INVALID",
                       f"模型未部署：{asr_stage.ASR_MODELS[alias]}。请先在服务器放置模型文件。",
                       status_code=422)
    source = (
        db.query(SourceRevision)
        .filter(SourceRevision.item_id == item.id,
                SourceRevision.revision == item.source_revision)
        .one_or_none()
    )
    if source is None:
        raise ApiError("NOT_FOUND", "来源版本缺失", status_code=500)
    run, created = asr_stage.start_asr(db, item=item, source=source,
                                       model_alias=alias, requested_by="manual")
    db.commit()
    out = _run_out(run)
    out["created"] = created
    return out


@router.get("/v1/items/{item_id}/asr")
def get_asr_status(item_id: str, principal=Depends(require_scope("items:read")),
                   db: Session = Depends(get_db)):
    item = _require_bilibili_item(db, principal.user.id, item_id)
    return _run_out(_latest_run(db, item))


@router.post("/v1/items/{item_id}/asr/cancel")
def cancel_asr(item_id: str, principal=Depends(require_scope("items:edit")),
               db: Session = Depends(get_db)):
    item = _require_bilibili_item(db, principal.user.id, item_id)
    run = _latest_run(db, item)
    if run is None or run.state in ("succeeded", "cancelled"):
        return {"cancelled": False, "note": "没有进行中的转写任务。"}
    asr_stage.cancel_asr(db, run)
    run.updated_at = utcnow()
    db.commit()
    return {"cancelled": True, "run_id": run.id}
