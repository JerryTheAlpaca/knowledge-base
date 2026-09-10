"""本地 ASR 接口（docs/11 §9.2；docs/13 §8）。

- GET/PUT /v1/asr-settings：用户级「无字幕时自动转写」开关；deployment_enabled
  只读返回（部署开关 + 默认模型可用性），用户不能通过 API 打开部署开关。
- POST /v1/items/{id}/asr：手动触发/重试；可选 retry_model（别名）与
  audio_candidate_id（页面多候选时选择要转写的媒体）。
- GET /v1/items/{id}/asr：当前 run 的阶段、完成段数、暂停原因（不暴露服务器路径）。
- POST /v1/items/{id}/asr/cancel：取消并终止执行中的子进程（由 Worker 监测执行）。
- GET /v1/items/{id}/audio-sources：页面音频候选（Worker 首次解析后缓存）。

用户隔离：全部按 principal.user.id 过滤；他人条目一律 404。触发依据「可用的
audio input」而不是「是否 B 站」；状态与取消只依据条目所有权与已有 run，
不要求此刻来源网址仍可访问（docs/13 §8）。
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
from ..extractors import audio_sources
from ..models import AsrRun, Item, SourceRevision, User, utcnow
from ..repositories import core as repo
from ..workers import asr as asr_stage

router = APIRouter(tags=["asr"])


def _require_item(db: Session, user_id: str, item_id: str) -> Item:
    """状态/取消：只要条目属于本人即可访问，不因来源网址失效而失去入口。"""
    item = repo.get_item(db, user_id, item_id)
    if item is None:
        raise ApiError("NOT_FOUND", "条目不存在", status_code=404)
    if item.deleted_at is not None:
        raise ApiError("GONE", "条目已删除", status_code=410)
    return item


def _require_audio_item(db: Session, user_id: str, item_id: str) -> Item:
    """触发：除所有权外还要求条目具备可用的音频来源（不是"只有 B 站"）。"""
    item = _require_item(db, user_id, item_id)
    capable, _kind = audio_sources.audio_capability(db, item)
    if not capable:
        raise ApiError(
            "SCHEMA_INVALID",
            "该条目没有可转写的音频来源（需 B 站视频、网页音频、音频直链或上传录音）",
            status_code=422,
        )
    return item


def _latest_run(db: Session, item: Item) -> AsrRun | None:
    return (
        db.query(AsrRun)
        .filter(AsrRun.user_id == item.user_id, AsrRun.item_id == item.id)
        .order_by(AsrRun.updated_at.desc(), AsrRun.created_at.desc())
        .first()
    )


def _run_out(run: AsrRun | None, db: Session | None = None, item: Item | None = None) -> dict:
    settings = get_settings()
    base = {"exists": False, "deployment_enabled": asr_stage.asr_enabled(settings),
            "default_model": settings.asr_model,
            "deployed_models": asr_stage.deployed_aliases(),
            "max_duration_seconds": settings.asr_max_duration_seconds,
            "max_input_bytes": settings.asr_max_input_bytes}
    if run is None:
        return base
    frozen = (run.input_json or {}).get("source") or {}
    locator = (run.input_json or {}).get("locator") or {}
    candidates = (run.input_json or {}).get("candidates") or []
    out = {
        **base,
        "exists": True,
        "run_id": run.id,
        "state": run.state,
        "pause_reason": run.pause_reason,
        "done_chunks": run.next_chunk_index,
        "chunk_count": run.chunk_count,
        "processed_audio_seconds": round(float(run.processed_seconds or 0.0), 1),
        "total_audio_seconds": (run.manifest_json or {}).get("total_duration_s"),
        "model_alias": run.model_alias,
        "model_id": run.model_id,
        "requested_by": run.requested_by,
        "failed_count": run.failed_count,
        "last_error": run.last_error or None,
        "retryable": run.state in ("failed", "cancelled"),
        "input_kind": run.input_kind,
        "input_fingerprint": run.input_fingerprint or None,
        "audio_candidates": candidates,
        "updated_at": run.updated_at.isoformat(),
        "deployment_enabled": asr_stage.asr_enabled(settings),
    }
    if frozen:
        from ..domain.source_labels import source_fields

        fields = source_fields(frozen.get("platform"), frozen.get("media_kind"))
        out.update({
            "source_type": fields["source_type"],
            "source_label": fields["source_label"],
            "icon_key": fields["icon_key"],
            "source_platform": fields["platform"],
            "media_kind": fields["media_kind"],
        })
    if locator:
        out["adapter_id"] = locator.get("adapter_id")
    if item is not None and db is not None:
        capable, kind = audio_sources.audio_capability(db, item)
        out["asr_supported"] = capable
        out["audio_kind"] = kind
    return out


class AsrSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    auto_when_no_track: bool


class AsrTriggerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    retry_model: str | None = None  # 只允许别名：dolphin | sense_voice
    audio_candidate_id: str | None = None  # 页面多候选时选择要转写的媒体


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
        "max_duration_seconds": settings.asr_max_duration_seconds,
        "max_audio_upload_bytes": settings.max_audio_upload_bytes,
        "audio_upload_chunk_bytes": settings.audio_upload_chunk_bytes,
        "models": [
            {"alias": alias, "model_id": asr_stage.ASR_MODELS[alias],
             "deployed": asr_stage.model_available(alias)}
            for alias in asr_stage.ASR_MODELS
        ],
        "note": "开启后：B 站确认无字幕轨的条目会在服务器空闲时自动音频转写"
                "（机器转写，未经人工校对）。网页音频与上传录音由用户主动触发。",
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
    """手动触发音频转写；模型别名/选中的音频幂等，重复点击不重复建任务。"""
    item = _require_audio_item(db, principal.user.id, item_id)
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
    selection = (body.audio_candidate_id if body else None)
    run, created = asr_stage.start_asr(db, item=item, source=source,
                                       model_alias=alias, requested_by="manual",
                                       selection=selection)
    db.commit()
    out = _run_out(run, db, item)
    out["created"] = created
    return out


@router.get("/v1/items/{item_id}/audio-sources")
def get_audio_sources(item_id: str, principal=Depends(require_scope("items:read")),
                      db: Session = Depends(get_db)) -> dict:
    """页面音频候选：Worker 首次解析后缓存；返回 pending/ready/unsupported。

    只有一个逻辑音频时无需选择（ready 且 candidates 为空即"可直接转写"）。
    """
    item = _require_item(db, principal.user.id, item_id)
    run = _latest_run(db, item)
    payload = (run.input_json or {}) if run else {}
    candidates = payload.get("candidates")
    last_status = payload.get("last_prepare_status") or ""
    capable, kind = audio_sources.audio_capability(db, item)
    if candidates:
        state, detail = "ready", "页面存在多条音频，请选择要转写的一条。"
    elif not capable or last_status in ("audio_source_unsupported", "audio_stream_unsupported"):
        state, detail = "unsupported", (run.last_error if run and run.last_error
                                        else "该条目没有可转写的音频来源。")
    else:
        state, detail = "pending", "音频来源尚未解析；触发转写后由服务器解析。"
    return {"state": state, "state_detail": detail, "audio_kind": kind,
            "candidates": candidates or [],
            "max_duration_seconds": get_settings().asr_max_duration_seconds}


@router.get("/v1/items/{item_id}/asr")
def get_asr_status(item_id: str, principal=Depends(require_scope("items:read")),
                   db: Session = Depends(get_db)):
    item = _require_item(db, principal.user.id, item_id)
    return _run_out(_latest_run(db, item), db, item)


@router.post("/v1/items/{item_id}/asr/cancel")
def cancel_asr(item_id: str, principal=Depends(require_scope("items:edit")),
               db: Session = Depends(get_db)):
    item = _require_item(db, principal.user.id, item_id)
    run = _latest_run(db, item)
    if run is None or run.state in ("succeeded", "cancelled"):
        return {"cancelled": False, "note": "没有进行中的转写任务。"}
    asr_stage.cancel_asr(db, run)
    run.updated_at = utcnow()
    db.commit()
    return {"cancelled": True, "run_id": run.id}
