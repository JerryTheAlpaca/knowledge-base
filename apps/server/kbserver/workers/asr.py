"""本地 ASR 执行模块（docs/11 §5、§6、§9.1）。

结构：
- start_asr：手动/自动入队入口（幂等；同 recipe 重跑复位检查点，换模型新 run）。
- execute_prepare：一次领取完成整条音频准备（受限流 → FFmpeg → 短 WAV 段），
  流 EOF / FFmpeg 成功 / 总时长覆盖三项都通过后原子提交清单。
- execute_transcribe：一次领取只识别一段；成功后原子落盘并在短事务推进
  检查点，再把任务重新排队让出给普通任务；全部段完成后合并发布 ASR 来源
  版本并入 enrich。

事务边界：下载、FFmpeg、识别期间不持有数据库事务；父进程周期续租，
续租失败/取消/忙碌让出都终止子进程（POSIX 回收整个进程组）。

失败语义：忙碌让出与正常推进不消耗失败重试额度；连续业务失败 ≥5 次该 run
进入 failed 并把条目送回补充材料；发布前复查来源版本，用户补充字幕后旧
ASR 结果作废（不覆盖新材料）。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
import wave
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from ..audio import prepare as audio_prepare
from ..audio.types import (
    ACQ_UPLOADED,
    AudioPrepareAborted,
    AudioPrepareError,
    AudioSourceError,
    ObjectAudioInput,
    PreparedAudio,
    RemoteAudioInput,
    ResolvedAudioSource,
)
from ..config import get_settings
from ..domain import pipeline
from ..domain.source_labels import source_fields
from ..extractors import audio_sources
from ..extractors import bilibili as bili
from ..extractors import subtitles as subfmt
from ..models import (
    AsrRun,
    Item,
    Job,
    SourceRevision,
    new_id,
    utcnow,
)
from ..security import credentials as cred_crypto
from ..storage.objects import ObjectStore
from . import idle as idle_mod
from .publish import publish_segments_revision

# docs/11 §2 固定制品：主模型 dolphin；备用 sense_voice 仅质量复测时部署
ASR_MODELS = {
    "dolphin": "sherpa-onnx-dolphin-base-ctc-multi-lang-int8-2025-04-02",
    "sense_voice": "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17",
}
ASR_MODEL_FILES = ("model.int8.onnx", "tokens.txt")
ASR_SCHEMA = "asr-prepared-v1"
MAX_RUN_FAILURES = 5
FAIL_BACKOFF_BASE_S = 30
LEASE_REFRESH_S = 20.0
RUNNING_CHECK_INTERVAL_S = 5.0


class AsrEngineError(Exception):
    """识别引擎执行失败或输出不可解析。"""


# ---- 模型与 recipe ----

def model_dir(alias: str) -> Path:
    return get_settings().asr_model_dir / ASR_MODELS[alias]


def model_available(alias: str) -> bool:
    """部署探测：模型目录里两个制品都在才算可用；不满足时 API 如实拒绝。"""
    d = model_dir(alias)
    return all((d / name).exists() for name in ASR_MODEL_FILES)


def deployed_aliases() -> list[str]:
    return [a for a in ASR_MODELS if model_available(a)]


def compute_recipe_hash(alias: str, settings=None) -> str:
    settings = settings or get_settings()
    doc = {
        "recipe": "asr-v1",
        "model_alias": alias,
        "model_id": ASR_MODELS[alias],
        "chunk_seconds": settings.asr_chunk_seconds,
        "context_seconds": settings.asr_chunk_context_seconds,
        "sample_rate": 16000,
        "channels": 1,
        "sample_fmt": "s16",
        "decoding": "greedy_search",
        "use_itn": alias == "sense_voice",
        "threads": settings.asr_threads,
    }
    return pipeline.sha256_hex(pipeline.canonical_json(doc))[:16]


def asr_enabled(settings=None) -> bool:
    return (settings or get_settings()).asr_enabled


def user_auto_enabled(db: Session, user_id: str) -> bool:
    from ..models import User

    user = db.get(User, user_id)
    if user is None:
        return False
    asr_pref = (user.settings_json or {}).get("asr") or {}
    return bool(asr_pref.get("auto_when_no_track"))


# ---- 入队（docs/11 §9.2）----

def _get_asr_job(db: Session, run: AsrRun, stage: str) -> Job | None:
    return db.query(Job).filter(
        Job.user_id == run.user_id, Job.item_id == run.item_id,
        Job.source_revision == run.source_revision,
        Job.stage == stage, Job.recipe_hash == run.recipe_hash,
    ).one_or_none()


def _enqueue_asr_stage(db: Session, run: AsrRun, stage: str, *,
                       not_before=None, reset_attempt: bool = False) -> Job:
    """幂等入队 ASR stage（jobs UNIQUE 含 stage+recipe_hash；复位旧行不插新行）。"""
    job = _get_asr_job(db, run, stage)
    if job is not None:
        if job.state != "running":
            job.state = "queued"
            job.not_before = not_before or utcnow()
            job.lease_token = None
            job.lease_until = None
            if reset_attempt:
                job.attempt = 0
        return job
    job = Job(
        user_id=run.user_id, item_id=run.item_id,
        source_revision=run.source_revision, stage=stage,
        recipe_hash=run.recipe_hash, state="queued",
        not_before=not_before or utcnow(),
    )
    db.add(job)
    db.flush()
    return job


def start_asr(db: Session, *, item: Item, source: SourceRevision, model_alias: str,
              requested_by: str, selection: str | None = None) -> tuple[AsrRun, bool]:
    """为条目创建/复位 ASR run 并入队准备任务；返回 (run, created)。

    幂等：同 (user,item,revision,recipe) 已有排队/执行中的 run 直接返回；
    已结束（成功/失败/取消）的 run 重跑时复位检查点（同 recipe 重新识别，
    发布幂等由 content_hash 保证，不产生重复版本）。
    选择不同音频候选（selection 变化）时重置检查点：不能继续旧的部分结果。
    """
    settings = get_settings()
    if model_alias not in ASR_MODELS:
        raise ValueError(f"未知模型别名：{model_alias}")
    recipe_hash = compute_recipe_hash(model_alias, settings)
    run = db.query(AsrRun).filter(
        AsrRun.user_id == item.user_id, AsrRun.item_id == item.id,
        AsrRun.source_revision == source.revision, AsrRun.recipe_hash == recipe_hash,
    ).one_or_none()
    created = False
    selection_changed = bool(run is not None
                             and (run.input_json or {}).get("selection") != selection)
    if run is None:
        run = AsrRun(
            user_id=item.user_id, item_id=item.id, source_revision=source.revision,
            recipe_hash=recipe_hash, model_alias=model_alias,
            model_id=ASR_MODELS[model_alias], state="queued",
            requested_by=requested_by, work_dir=f"asr/{new_id()}",
            input_json={"selection": selection},
        )
        db.add(run)
        db.flush()
        created = True
    elif run.state in ("succeeded", "failed", "cancelled") or selection_changed:
        # 重跑/换媒体：清空工作目录与检查点，从头准备
        _remove_work_dir(settings, run)
        run.state = "queued"
        run.pause_reason = ""
        run.next_chunk_index = 0
        run.chunk_count = 0
        run.failed_count = 0
        run.processed_seconds = 0.0
        run.manifest_json = {}
        run.last_error = ""
        run.requested_by = requested_by
        run.input_json = {"selection": selection}
        run.input_fingerprint = ""
        db.flush()
    _enqueue_asr_stage(db, run, "asr_prepare", reset_attempt=True)
    item.pipeline_state = "queued"
    item.state_detail = "等待音频转写（服务器空闲时执行）"
    pipeline.emit_event(
        db, item.user_id, item_id=item.id, bundle_revision=item.bundle_revision,
        event_type="asr_state_changed",
        payload=_run_event_payload(run, extra={"requested_by": requested_by}),
    )
    return run, created


def cancel_asr(db: Session, run: AsrRun) -> None:
    """取消：置 run 状态并取消排队中任务；执行中的任务由 Worker 监测后终止子进程。"""
    run.state = "cancelled"
    run.pause_reason = ""
    run.updated_at = utcnow()
    for stage in ("asr_prepare", "asr_transcribe"):
        job = _get_asr_job(db, run, stage)
        if job is not None and job.state in ("queued", "retry_wait"):
            job.state = "cancelled"
    item = db.get(Item, run.item_id)
    if item is not None and item.pipeline_state in ("queued", "extracting"):
        item.pipeline_state = "needs_input"
        item.state_detail = "音频转写已取消；可补充字幕/正文或重新触发转写。"
    pipeline.emit_event(db, run.user_id, item_id=run.item_id,
                        bundle_revision=None, event_type="asr_state_changed",
                        payload=_run_event_payload(run))


def _remove_work_dir(settings, run: AsrRun) -> None:
    if not run.work_dir:
        return
    base = (settings.tmp_dir / run.work_dir).resolve()
    tmp_root = settings.tmp_dir.resolve()
    if str(base).startswith(str(tmp_root)) and base.exists():  # 只清理受控工作目录
        shutil.rmtree(base, ignore_errors=True)


# ---- 共享小工具 ----

@dataclass
class RunContext:
    job: Job
    run: AsrRun
    item: Item
    source: SourceRevision


def _load_run_for_job(db: Session, job: Job) -> AsrRun | None:
    return db.query(AsrRun).filter(
        AsrRun.user_id == job.user_id,
        AsrRun.item_id == job.item_id,
        AsrRun.source_revision == job.source_revision,
        AsrRun.recipe_hash == job.recipe_hash,
    ).one_or_none()


def _load_context(db: Session, job_id: str, lease_token: str) -> RunContext | None:
    """短事务内校验任务租约与检查点归属；失效返回 None（任务已被接管/取消）。"""
    job = db.get(Job, job_id)
    if job is None or job.lease_token != lease_token or job.state != "running":
        return None
    run = _load_run_for_job(db, job)
    if run is None:
        return None
    item = db.get(Item, job.item_id)
    if item is None or item.deleted_at is not None:
        return None
    source = (
        db.query(SourceRevision)
        .filter(SourceRevision.item_id == item.id, SourceRevision.revision == job.source_revision)
        .one_or_none()
    )
    if source is None:
        return None
    return RunContext(job=job, run=run, item=item, source=source)


def _lease_refresh(session_factory, job_id: str, lease_token: str) -> bool:
    """按任务 ID + lease_token + running 状态续租；失败返回 False（已被接管）。"""
    from sqlalchemy import text

    now = utcnow().replace(tzinfo=None)
    with session_factory() as db:
        row = db.execute(
            text("UPDATE jobs SET lease_until=:lu WHERE id=:jid AND lease_token=:lt AND state='running'"),
            {"lu": now + timedelta(seconds=get_settings().job_lease_seconds),
             "jid": job_id, "lt": lease_token},
        )
        db.commit()
        return (row.rowcount or 0) > 0


def _still_owned(session_factory, job_id: str, lease_token: str) -> bool:
    with session_factory() as db:
        job = db.get(Job, job_id)
        return job is not None and job.lease_token == lease_token and job.state == "running"


def _run_event_payload(run: AsrRun, extra: dict | None = None) -> dict:
    payload = {
        "run_id": run.id,
        "state": run.state,
        "pause_reason": run.pause_reason,
        "done_chunks": run.next_chunk_index,
        "chunk_count": run.chunk_count,
        "model_alias": run.model_alias,
        "last_error": (run.last_error or "")[:200],
    }
    if extra:
        payload.update(extra)
    return payload


def _emit_state(db: Session, run: AsrRun) -> None:
    pipeline.emit_event(db, run.user_id, item_id=run.item_id,
                        bundle_revision=None, event_type="asr_state_changed",
                        payload=_run_event_payload(run))


def _pause_run(db: Session, run: AsrRun, job: Job, reason: str, cooldown_s: int) -> None:
    """忙碌让出：不算错误，不消耗失败重试额度（docs/11 §6.1）。"""
    run.state = "paused"
    run.pause_reason = reason
    run.updated_at = utcnow()
    job.state = "queued"
    job.attempt = 0
    job.not_before = utcnow() + timedelta(seconds=cooldown_s)
    _emit_state(db, run)


def _fail_run(db: Session, run: AsrRun, job: Job, item: Item, error: str, *,
              final: bool) -> None:
    """业务失败：连续 ≥5 次终态化；未终态时有限退避重试。"""
    run.failed_count += 1
    run.last_error = error[:500]
    run.updated_at = utcnow()
    final = final or run.failed_count >= MAX_RUN_FAILURES
    if final:
        run.state = "failed"
        run.pause_reason = ""
        job.state = "failed"
        job.last_error = error[:200]
        item.pipeline_state = "needs_input"
        item.state_detail = f"音频转写失败：{error[:120]}"
        pipeline.emit_event(db, item.user_id, item_id=item.id,
                            bundle_revision=item.bundle_revision,
                            event_type="item_needs_input",
                            payload={"reason": "asr_failed", "detail": error[:200], "stage": "asr"})
    else:
        backoff = min(300, FAIL_BACKOFF_BASE_S * (2 ** (run.failed_count - 1)))
        job.state = "retry_wait"
        job.last_error = error[:200]
        job.not_before = utcnow() + timedelta(seconds=backoff)
    _emit_state(db, run)


def _user_sessdata(db: Session, user_id: str) -> tuple[str | None, str | None]:
    """读取用户托管的 B 站登录态（与字幕路径同机制；明文不落日志/库）。"""
    from ..models import Credential, ProviderProfile

    row = (
        db.query(ProviderProfile, Credential)
        .join(Credential, Credential.profile_id == ProviderProfile.id)
        .filter(
            ProviderProfile.user_id == user_id,
            ProviderProfile.kind == "bilibili_session",
            Credential.revoked_at.is_(None),
        )
        .order_by(Credential.created_at.desc())
        .first()
    )
    if row is None:
        return None, None
    profile, cred = row
    try:
        value = cred_crypto.decrypt_secret(
            cred.encrypted_secret, cred.encrypted_dek, cred.nonces_json,
            get_settings().load_master_key(),
            user_id=user_id, profile_id=profile.id, credential_version=cred.version,
        )
    except Exception:
        return None, "B 站登录凭据解密失败；请重新提交 SESSDATA。"
    return value, None


def _make_monitor(session_factory, job_id: str, lease_token: str,
                  gate: idle_mod.AsrGate | None):
    """进度回调工厂：节流执行续租、空闲检查与归属检查；返回 "yield"/"cancel"/None。"""
    settings = get_settings()
    state = {"last_check": 0.0, "last_lease": 0.0}

    def monitor(bytes_read: int):
        now = time.monotonic()
        if now - state["last_check"] < RUNNING_CHECK_INTERVAL_S:
            return None
        state["last_check"] = now
        if now - state["last_lease"] >= LEASE_REFRESH_S:
            state["last_lease"] = now
            if not _lease_refresh(session_factory, job_id, lease_token):
                return "cancel"
        if gate is not None and not gate.check_running(settings):
            gate.note_busy()
            return "yield"
        if not _still_owned(session_factory, job_id, lease_token):
            return "cancel"
        return None

    return monitor


def _abort_ctx(session_factory, job_id: str, lease_token: str, reason: str) -> None:
    """中止后的落库：cancel → 取消 run；yield → 冷却暂停。"""
    settings = get_settings()
    with session_factory() as db:
        ctx = _load_context(db, job_id, lease_token)
        if ctx is None:
            return
        if reason == "cancel":
            cancel_asr(db, ctx.run)
        else:
            _pause_run(db, ctx.run, ctx.job, "resource_busy",
                       settings.asr_busy_cooldown_seconds)
        db.commit()


# ---- prepare（docs/11 §5.2、§5.3）----

def execute_prepare(session_factory, job_id: str, lease_token: str,
                    gate: idle_mod.AsrGate | None) -> None:
    settings = get_settings()
    # Phase A：短事务校验并落 preparing
    with session_factory() as db:
        ctx = _load_context(db, job_id, lease_token)
        if ctx is None:
            return
        if not asr_enabled(settings):
            ctx.item.state_detail = "音频已保存；转写等待部署启用（ASR 开关未开启）"
            _pause_run(db, ctx.run, ctx.job, "disabled", 300)
            db.commit()
            return
        ctx.run.state = "preparing"
        ctx.run.pause_reason = ""
        ctx.item.pipeline_state = "extracting"
        ctx.item.state_detail = "正在读取音频（服务器空闲时执行）"
        _emit_state(db, ctx.run)
        db.commit()
        plan = {
            "user_id": ctx.item.user_id,
            "work_dir": ctx.run.work_dir,
            "item_id": ctx.item.id,
            "selection": (ctx.run.input_json or {}).get("selection"),
        }

    # Phase B：事务外完成来源解析、网络与解码
    try:
        prepared, attempt_name, resolved = _prepare_outside(
            session_factory, job_id, lease_token, plan, gate, settings)
    except AudioPrepareAborted as exc:
        _abort_ctx(session_factory, job_id, lease_token, exc.reason)
        return
    except audio_sources.AudioSourceSelectionRequired as exc:
        _park_for_selection(session_factory, job_id, lease_token, exc.candidates)
        return
    except (AudioSourceError, AudioPrepareError, bili.BilibiliError) as exc:
        _handle_prepare_failure(session_factory, job_id, lease_token, exc)
        return

    # Phase C：原子提交清单（docs/11 §5.3）
    with session_factory() as db:
        ctx = _load_context(db, job_id, lease_token)
        if ctx is None:
            return  # 租约已丢失：清单未提交，下次 prepare 用新 attempt 重做
        _commit_manifest(db, ctx.run, prepared, attempt_name, settings, resolved)
        ctx.job.state = "succeeded"
        _enqueue_asr_stage(db, ctx.run, "asr_transcribe")
        ctx.run.state = "transcribing"
        ctx.item.pipeline_state = "extracting"
        ctx.item.state_detail = f"音频已就绪，共 {len(prepared.chunks)} 段（服务器空闲时转写）"
        _emit_state(db, ctx.run)
        db.commit()


def _prepare_outside(session_factory, job_id: str, lease_token: str, plan: dict,
                     gate, settings) -> tuple[PreparedAudio, str, ResolvedAudioSource]:
    """解析来源并把整条音频准备成短 WAV 段；期间不持有数据库事务。

    来源解析（B 站/直链/网页/上传原件）在 audio_sources 分发，识别阶段不感知网站。
    """
    monitor = _make_monitor(session_factory, job_id, lease_token, gate)
    with session_factory() as db:
        item = db.get(Item, plan["item_id"])
        if item is None:
            raise AudioSourceError("network_error", "条目不存在，无法准备音频。")
        resolved = audio_sources.resolve_audio_source(db, item, selection=plan.get("selection"))

    work_dir = settings.tmp_dir / plan["work_dir"]
    work_dir.mkdir(parents=True, exist_ok=True)
    attempt_name = f"attempt-{int(time.time() * 1000)}"
    attempt_dir = work_dir / attempt_name
    attempt_dir.mkdir(parents=True, exist_ok=True)
    prepared = audio_prepare.prepare_audio(
        resolved.input, attempt_dir,
        limits=audio_prepare.AudioLimits(
            ffmpeg_bin=settings.asr_ffmpeg_bin,
            chunk_seconds=settings.asr_chunk_seconds,
            max_bytes=settings.asr_max_input_bytes,
            max_duration_s=settings.asr_max_duration_seconds,
        ),
        monitor=monitor,
    )
    # 归属信息随清单提交：来源事实 + 音轨标识（不写临时签名 URL）
    meta = dict(resolved.source.locator())
    meta["adapter_id"] = resolved.source.adapter_id
    meta["adapter_version"] = resolved.source.adapter_version
    if isinstance(resolved.input, RemoteAudioInput):
        meta.update(resolved.input.stream_meta)
    prepared.meta = meta
    return prepared, attempt_name, resolved


def _park_for_selection(session_factory, job_id: str, lease_token: str,
                        candidates: list[dict]) -> None:
    """页面有多条音频：暂停 run 并缓存候选，等用户在详情里选择一次（docs/13 §4.3）。"""
    with session_factory() as db:
        ctx = _load_context(db, job_id, lease_token)
        if ctx is None:
            return
        payload = dict(ctx.run.input_json or {})
        payload["candidates"] = audio_sources.candidate_list(candidates)
        ctx.run.input_json = payload
        ctx.run.state = "paused"
        ctx.run.pause_reason = "selection_required"
        ctx.job.state = "succeeded"  # 不再自动重试，等用户选择
        ctx.item.pipeline_state = "needs_input"
        ctx.item.state_detail = "页面有多条音频，请在详情中选择要转写的一条。"
        _emit_state(db, ctx.run)
        pipeline.emit_event(
            db, ctx.item.user_id, item_id=ctx.item.id, bundle_revision=ctx.item.bundle_revision,
            event_type="asr_state_changed",
            payload={**_run_event_payload(ctx.run), "reason": "audio_source_selection"},
        )
        db.commit()


def _handle_prepare_failure(session_factory, job_id: str, lease_token: str,
                            exc: Exception) -> None:
    """准备失败分类：网络错误有限退避；其余终态进补充材料（docs/11 §5.3）。"""
    status = getattr(exc, "status", "unknown")
    message = getattr(exc, "message", str(exc))
    retryable = status in ("network_error",)
    with session_factory() as db:
        ctx = _load_context(db, job_id, lease_token)
        if ctx is None:
            return
        # 记录失败分类，供 /audio-sources 区分 unsupported 与暂时失败
        ctx.run.input_json = {**(ctx.run.input_json or {}), "last_prepare_status": status}
        _fail_run(db, ctx.run, ctx.job, ctx.item, message, final=not retryable)
        db.commit()


def _commit_manifest(db: Session, run: AsrRun, prepared: PreparedAudio,
                     attempt_name: str, settings, resolved: ResolvedAudioSource) -> None:
    """完整准备成功后原子提交清单：先磁盘 manifest，再短事务更新检查点。"""
    manifest = {
        "schema": ASR_SCHEMA,
        "model_alias": run.model_alias,
        "model_id": run.model_id,
        "recipe_hash": run.recipe_hash,
        "chunks_dir": f"{attempt_name}/chunks",
        "expected_duration_s": prepared.expected_duration,
        "total_duration_s": prepared.total_duration,
        "source_bytes": prepared.source_bytes,
        "chunk_seconds": settings.asr_chunk_seconds,
        "sample_rate": 16000,
        "source_locator": dict(prepared.meta or {}),
        "chunks": [
            {"index": c.index, "path": c.path, "core_start": c.core_start,
             "core_end": c.core_end, "duration": c.duration,
             "bytes": c.bytes, "sha256": c.sha256}
            for c in prepared.chunks
        ],
    }
    work_dir = settings.tmp_dir / run.work_dir
    manifest_path = work_dir / "manifest.json"
    tmp_path = work_dir / "manifest.json.tmp"
    tmp_path.write_bytes(pipeline.canonical_json(manifest))
    os.replace(tmp_path, manifest_path)  # 原子提交：此后清单不可变

    manifest_sha = pipeline.sha256_hex(pipeline.canonical_json(manifest))
    run.manifest_json = {
        "schema": ASR_SCHEMA,
        "manifest_sha256": manifest_sha,
        "chunk_count": len(prepared.chunks),
        "total_duration_s": prepared.total_duration,
        "expected_duration_s": prepared.expected_duration,
        "source_locator": dict(prepared.meta or {}),
    }
    # 冻结本次输入：只存来源稳定定位或对象引用（不存 headers/签名 URL）
    locator = resolved.source.locator()
    run.input_kind = resolved.input.kind
    run.input_json = {
        **(run.input_json or {}),
        "source": {
            "platform": resolved.source.platform,
            "media_kind": resolved.source.media_kind,
            "adapter_id": resolved.source.adapter_id,
            "adapter_version": resolved.source.adapter_version,
            "original_url": resolved.source.original_url,
            "canonical_url": resolved.source.canonical_url,
            "title": resolved.source.title,
            "author": resolved.source.author,
            "published_at": resolved.source.published_at,
            "acquisition": resolved.source.acquisition,
        },
        "locator": locator,
        "object_ref": ({"storage_sha256": resolved.input.sha256,
                        "bytes": resolved.input.size_bytes}
                       if isinstance(resolved.input, ObjectAudioInput) else None),
    }
    run.input_fingerprint = resolved.input_fingerprint
    run.chunk_count = len(prepared.chunks)
    run.next_chunk_index = 0
    run.updated_at = utcnow()


def _load_manifest(run: AsrRun, settings) -> dict | None:
    """读取已提交清单并校验摘要；缺失/不匹配返回 None（需重新准备）。"""
    summary = run.manifest_json or {}
    if not summary or not run.work_dir:
        return None
    manifest_path = settings.tmp_dir / run.work_dir / "manifest.json"
    try:
        raw = manifest_path.read_bytes()
    except OSError:
        return None
    if pipeline.sha256_hex(raw) != summary.get("manifest_sha256"):
        return None
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if manifest.get("schema") != ASR_SCHEMA:
        return None
    return manifest


# ---- transcribe（docs/11 §6.1：一次领取识别一段）----

def execute_transcribe(session_factory, job_id: str, lease_token: str,
                       gate: idle_mod.AsrGate | None) -> None:
    settings = get_settings()
    with session_factory() as db:
        ctx = _load_context(db, job_id, lease_token)
        if ctx is None:
            return
        if not asr_enabled(settings):
            _pause_run(db, ctx.run, ctx.job, "disabled", 300)
            db.commit()
            return
        manifest = _load_manifest(ctx.run, settings)
        if manifest is None:
            # 清单缺失/损坏：回退到准备阶段重做（新 attempt，不复用旧 PCM）
            ctx.run.state = "queued"
            ctx.run.manifest_json = {}
            ctx.run.chunk_count = 0
            ctx.job.state = "succeeded"
            _enqueue_asr_stage(db, ctx.run, "asr_prepare", reset_attempt=True)
            _emit_state(db, ctx.run)
            db.commit()
            return
        index = ctx.run.next_chunk_index
        chunks = manifest.get("chunks") or []
        chunk_count = len(chunks)
        run_id = ctx.run.id
        work_dir_rel = ctx.run.work_dir
        model_alias = ctx.run.model_alias
        if index >= chunk_count:
            ctx.item.state_detail = "转写收尾中"
            db.commit()
            subtitle_ref = _fetch_platform_subtitle_ref(session_factory, run_id)
            _finish_if_complete(session_factory, job_id, lease_token,
                                subtitle_ref=subtitle_ref)
            return
        chunk = chunks[index]
        # 校验已提交段的文件摘要；损坏的已提交清单不静默续跑
        chunk_path = (settings.tmp_dir / work_dir_rel / manifest["chunks_dir"]
                      / Path(chunk["path"]).name)
        if not chunk_path.exists() or _file_sha256(chunk_path) != chunk["sha256"]:
            _fail_run(db, ctx.run, ctx.job, ctx.item,
                      "已准备的音频片段校验失败，需要重新准备。", final=True)
            db.commit()
            return
        if not model_available(model_alias):
            _fail_run(db, ctx.run, ctx.job, ctx.item,
                      f"识别模型未部署（{ASR_MODELS[model_alias]}）；请先在服务器放置模型文件。",
                      final=True)
            db.commit()
            return
        ctx.run.state = "transcribing"
        ctx.run.pause_reason = ""
        ctx.item.pipeline_state = "extracting"
        ctx.item.state_detail = f"正在转写，已完成 {index}/{chunk_count} 段"
        db.commit()

    # Phase B：事务外识别一段
    try:
        result = _transcribe_one(session_factory, job_id, lease_token, gate, settings,
                                 work_dir_rel=work_dir_rel, manifest=manifest,
                                 index=index, model_alias=model_alias)
    except AudioPrepareAborted as exc:
        _abort_ctx(session_factory, job_id, lease_token, exc.reason)
        return
    except AsrEngineError as exc:
        with session_factory() as db:
            ctx = _load_context(db, job_id, lease_token)
            if ctx is not None:
                _fail_run(db, ctx.run, ctx.job, ctx.item, str(exc), final=False)
                db.commit()
        return

    # Phase C：短事务推进检查点；同一段未提交前崩溃只重算该段
    with session_factory() as db:
        ctx = _load_context(db, job_id, lease_token)
        if ctx is None:
            return
        run = ctx.run
        chunk_duration = float(chunks[index].get("duration") or 0.0)
        results_dir = settings.tmp_dir / work_dir_rel / "results"
        committed = _commit_chunk_result(result, index, results_dir)
        if not committed:
            _fail_run(db, run, ctx.job, ctx.item, "识别结果落盘失败", final=False)
            db.commit()
            return
        run.processed_seconds = float(run.processed_seconds or 0.0) + chunk_duration
        run.next_chunk_index = index + 1
        run.updated_at = utcnow()
        if run.next_chunk_index >= chunk_count:
            ctx.item.state_detail = f"转写完成，共 {chunk_count} 段；正在整理发布"
            db.commit()
            subtitle_ref = _fetch_platform_subtitle_ref(session_factory, run_id)
            _finish_if_complete(session_factory, job_id, lease_token,
                                subtitle_ref=subtitle_ref)
            return
        # 同一逻辑任务重新排队：普通任务可随时插队（docs/11 §6.1）
        ctx.job.state = "queued"
        ctx.job.attempt = 0  # 正常推进不消耗失败重试额度
        ctx.job.not_before = utcnow()
        ctx.item.state_detail = f"正在转写，已完成 {run.next_chunk_index}/{chunk_count} 段"
        _emit_state(db, run)
        db.commit()


def _transcribe_one(session_factory, job_id: str, lease_token: str, gate, settings, *,
                    work_dir_rel: str, manifest: dict, index: int,
                    model_alias: str) -> dict:
    """识别一个约 20s 片段：拼 1s 上下文 → 引擎子进程 → 解析输出（事务外）。"""
    chunks = manifest["chunks"]
    chunk = chunks[index]
    work_dir = settings.tmp_dir / work_dir_rel
    chunks_base = work_dir / manifest["chunks_dir"]

    current_path = chunks_base / Path(chunk["path"]).name
    core_start = float(chunk["core_start"])
    input_start = core_start
    input_path = work_dir / f"input-{index:04d}.wav"
    context = settings.asr_chunk_context_seconds
    if index > 0 and context > 0:
        prev = chunks[index - 1]
        prev_path = chunks_base / Path(prev["path"]).name
        tail_s = min(context, float(prev.get("duration") or context))
        input_start = core_start - tail_s
        _concat_wav(prev_path, current_path, tail_s, input_path)
    else:
        shutil.copyfile(current_path, input_path)

    try:
        command = _engine_command(settings, model_alias, model_dir(model_alias), input_path)
        try:
            proc = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                **({"start_new_session": True} if os.name == "posix" else {}),
            )
        except (OSError, FileNotFoundError) as exc:
            raise AsrEngineError(f"识别引擎启动失败：{exc}") from exc
        out_chunks: list[bytes] = []
        reader = threading.Thread(
            target=lambda: out_chunks.append(proc.stdout.read()), daemon=True)
        reader.start()
        deadline = time.monotonic() + settings.asr_chunk_timeout_seconds
        while True:
            try:
                proc.wait(timeout=LEASE_REFRESH_S)
                break
            except subprocess.TimeoutExpired:
                pass
            if time.monotonic() > deadline:
                _terminate_tree(proc)
                reader.join(timeout=2)
                raise AsrEngineError(f"单段识别超时（{settings.asr_chunk_timeout_seconds}s）")
            if not _lease_refresh(session_factory, job_id, lease_token):
                _terminate_tree(proc)
                reader.join(timeout=2)
                raise AudioPrepareAborted("cancel")
            if gate is not None and not gate.check_running(settings):
                _terminate_tree(proc)
                gate.note_busy()
                reader.join(timeout=2)
                raise AudioPrepareAborted("yield")
            if not _still_owned(session_factory, job_id, lease_token):
                _terminate_tree(proc)
                reader.join(timeout=2)
                raise AudioPrepareAborted("cancel")
        reader.join(timeout=5)
        stdout = b"".join(out_chunks)
        if proc.returncode != 0:
            raise AsrEngineError(f"识别引擎退出码 {proc.returncode}")
        doc = _parse_engine_output(stdout)
    finally:
        try:
            os.unlink(input_path)
        except OSError:
            pass

    return {
        "index": index,
        "core_start": core_start,
        "core_end": float(chunk["core_end"]),
        "input_start": round(input_start, 3),
        "duration": float(chunk.get("duration") or 0.0),
        "text": str(doc.get("text") or ""),
        "tokens": doc.get("tokens") if isinstance(doc.get("tokens"), list) else None,
        "timestamps": doc.get("timestamps") if isinstance(doc.get("timestamps"), list) else None,
        "model_id": manifest.get("model_id"),
    }


def _engine_command(settings, alias: str, model_d: Path, wav_path: Path) -> list[str]:
    """按 §2 官方示例固定参数：CPU、1 线程、greedy；Dolphin 不套用 Whisper 参数。"""
    cmd = [settings.asr_engine_bin, f"--tokens={model_d / 'tokens.txt'}"]
    if alias == "sense_voice":
        cmd += [
            f"--sense-voice-model={model_d / 'model.int8.onnx'}",
            "--sense-voice-use-itn=1",
        ]
    else:
        cmd += [
            f"--dolphin-model={model_d / 'model.int8.onnx'}",
            "--decoding-method=greedy_search",
        ]
    cmd += [
        f"--num-threads={settings.asr_threads}",
        "--debug=0",
        str(wav_path),
    ]
    return cmd


def _parse_engine_output(stdout: bytes) -> dict:
    """解析 CLI 的结构化识别结果（JSON 行），不依赖 stderr 性能文本。"""
    text = stdout.decode("utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(doc, dict) and "text" in doc:
            return doc
    raise AsrEngineError("识别引擎没有输出可解析的结果")


def _concat_wav(prev_path: Path, current_path: Path, tail_s: float, out_path: Path) -> None:
    """从相邻磁盘 PCM 段拼出带上下文的识别输入（最多同时读两个小段）。"""
    with wave.open(str(prev_path), "rb") as w_prev, wave.open(str(current_path), "rb") as w_cur:
        rate = w_cur.getframerate()
        width = w_cur.getsampwidth()
        channels = w_cur.getnchannels()
        tail_bytes = int(tail_s * rate) * width * channels
        prev_data = w_prev.readframes(w_prev.getnframes())[-tail_bytes:]
        cur_data = w_cur.readframes(w_cur.getnframes())
    with wave.open(str(out_path), "wb") as w_out:
        w_out.setnchannels(channels)
        w_out.setsampwidth(width)
        w_out.setframerate(rate)
        w_out.writeframes(prev_data + cur_data)


def _terminate_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "posix":
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        else:
            proc.terminate()
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _commit_chunk_result(result: dict, index: int, results_dir: Path) -> bool:
    """逐段结果先写临时文件再原子改名；提交顺序保证崩溃只损失未提交段。"""
    try:
        results_dir.mkdir(parents=True, exist_ok=True)
        final_path = results_dir / f"chunk-{index:04d}.json"
        tmp_path = results_dir / f"chunk-{index:04d}.json.tmp"
        tmp_path.write_bytes(pipeline.canonical_json(result))
        os.replace(tmp_path, final_path)
        return True
    except OSError:
        return False


def _load_committed_results(settings, work_dir_rel: str, count: int) -> list[dict]:
    results_dir = settings.tmp_dir / work_dir_rel / "results"
    out = []
    for i in range(count):
        path = results_dir / f"chunk-{i:04d}.json"
        try:
            out.append(json.loads(path.read_bytes().decode("utf-8")))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return out[:i]  # 未提交的段丢弃（恢复时重算）
    return out


# ---- 合并与发布（docs/11 §7）----

# 分组阈值：token 间隔超过 GROUP_PAUSE_S 视为说话停顿；句末标点优先断句；
# 连续语流无停顿无标点时到 GROUP_MAX_TOKENS 才强制断（安全上限）。
# 2026-09-15 调整：旧实现按段独立分组 + 50 token 硬上限，会把跨段一句话
# 切成「所以没。/必要学。」这类碎片；现在分组跨段连续进行。
GROUP_PAUSE_S = 1.2
GROUP_MAX_TOKENS = 100
_GROUP_SENTENCE_END = set("。！？…；!?.")
# 参与边界去重的标点（含逗号等句中标点；不含引号括号等成对符号）
_GROUP_PUNCT = set("，。、！？…；：,.!?;:")
# ▁ 是分词器的词边界符，英文输出时替换回空格
_GROUP_TOKEN_SEP = "▁"


def _merge_results(manifest: dict, results: list[dict]) -> tuple[list[dict], list[list[float]]]:
    """段结果 → 原始记录（绝对秒）：有 token 时间用 token，无则段级粗粒度。

    相邻重叠边界只按 core 区间裁剪（上下文里的识别结果丢弃，由上一段的
    core 覆盖），禁止按相同文本全局去重；声音真实重复应保留。

    有 token 时间的段汇入同一条全局 token 流后统一分组（不按段重开）：
    说话停顿断句、句末标点优先断句、安全上限兜底，跨段连续语音不因
    段边界被腰斩。
    """
    records: list[dict] = []
    silence: list[list[float]] = []
    stream: list[tuple[float, str, int]] = []
    for seq, res in enumerate(results):
        core_start = float(res["core_start"])
        core_end = float(res["core_end"])
        tokens = res.get("tokens")
        stamps = res.get("timestamps")
        text = (res.get("text") or "").strip()
        if not text and not tokens:
            silence.append([core_start, core_end])
            continue
        if tokens and stamps and len(tokens) == len(stamps):
            # 模型时间 + 输入实际起点 = 视频绝对时间；只保留 core 区间
            added = 0
            for tok, ts in zip(tokens, stamps):
                try:
                    t = float(ts) + float(res["input_start"])
                except (TypeError, ValueError):
                    continue
                if core_start - 1e-6 <= t < core_end:
                    stream.append((t, str(tok), seq))
                    added += 1
            if not added:
                silence.append([core_start, core_end])
        else:
            # 无 token 时间：切段时间 + 标注粗粒度（不凭文字长度伪造逐字时间）
            records.append({"start_s": core_start, "end_s": core_end, "text": text})
    stream.sort(key=lambda item: (item[0], item[2]))
    # token 是否为本段 core 的最后一个 token（跨段边界判定用）
    chunk_final = [False] * len(stream)
    for i in range(len(stream) - 1):
        if stream[i][2] != stream[i + 1][2]:
            chunk_final[i] = True

    def _flush(group: list[tuple[float, str]], start: float, end: float) -> None:
        joined = "".join(tok for _t, tok in group).replace(_GROUP_TOKEN_SEP, " ").strip()
        if joined:
            records.append({"start_s": start, "end_s": end + 0.5, "text": joined})

    group: list[tuple[float, str]] = []
    group_start = 0.0
    last_t = 0.0
    for i, (t, tok, _seq) in enumerate(stream):
        if group and (t - last_t > GROUP_PAUSE_S or len(group) >= GROUP_MAX_TOKENS):
            _flush(group, group_start, last_t)
            group = []
        if not group:
            group_start = t
        # 段边界伪收尾：句末标点恰好是本段 core 的最后一个 token，且下一段
        # 第一个 token 紧随其后（连续语音）——模型对切段边界的虚假句读，
        # 丢弃标点让句子跨段续在一起（「所以没。/必要学。」→「所以没必要学。」）。
        # 真实的句末停顿间隔 > GROUP_PAUSE_S，不受影响。
        if tok in _GROUP_SENTENCE_END and chunk_final[i] and i + 1 < len(stream) \
                and stream[i + 1][0] - t <= GROUP_PAUSE_S:
            last_t = t
            continue
        # 段边界重识别产生的重复标点（「。，」「。。」）：连续标点只保留第一个
        if tok in _GROUP_PUNCT:
            tail = group[-1][1] if group else (records[-1]["text"][-1:] if records else "")
            if tail and tail[-1] in _GROUP_PUNCT:
                last_t = t
                continue
        group.append((t, tok))
        last_t = t
        if tok in _GROUP_SENTENCE_END:
            _flush(group, group_start, last_t)
            group = []
    if group:
        _flush(group, group_start, last_t)
    records.sort(key=lambda rec: rec["start_s"])
    return records, silence


def _fetch_platform_subtitle_ref(session_factory, run_id: str) -> list[dict] | None:
    """ASR 收尾时尝试抓取平台字幕作听写校对参考；失败一律静默跳过。

    只对 B 站来源尝试（用用户托管的 SESSDATA，匿名也可）。平台字幕没有
    标点、可能省略语气词，不能当正文，但用词准确，供 AI 修正听错字词对照。
    """
    try:
        platform = url = None
        sessdata = None
        with session_factory() as db:
            run = db.get(AsrRun, run_id)
            if run is None:
                return None
            frozen = (run.input_json or {}).get("source") or {}
            platform = frozen.get("platform")
            url = frozen.get("canonical_url")
            if platform == "bilibili" and url:
                sessdata, _err = _user_sessdata(db, run.user_id)
        if platform != "bilibili" or not url:
            return None
        ext = bili.extract(url, sessdata=sessdata)
        records = [
            {"start_ms": int(s["start_ms"]), "end_ms": int(s["end_ms"]),
             "text": (s.get("text") or "").strip()}
            for s in ext.segments if (s.get("text") or "").strip()
        ]
        return records or None
    except Exception:  # noqa: BLE001 —— 字幕参考是尽力而为，失败不影响发布
        return None


def _finish_if_complete(session_factory, job_id: str, lease_token: str,
                        subtitle_ref: list[dict] | None = None) -> None:
    """全部段已提交：合并 → 发布 ASR 来源版本 → 入 enrich → 清理 PCM。

    发布在单个短事务内完成（与 enrich Phase C 同模式）；发布前复查租约、
    来源版本与删除状态。run 已 succeeded 时直接返回（幂等）。
    """
    settings = get_settings()
    run_ref: dict | None = None
    with session_factory() as db:
        job = db.get(Job, job_id)
        if job is None or job.lease_token != lease_token:
            return
        run = _load_run_for_job(db, job)
        if run is None or run.state in ("succeeded", "cancelled", "failed"):
            return
        item = db.get(Item, job.item_id)
        if item is None:
            return
        if item.deleted_at is not None or item.source_revision != run.source_revision:
            # 用户已删除条目或补充了新材料：旧 ASR 结果作废，不覆盖（docs/11 §6.3）
            run.state = "cancelled"
            run.last_error = "来源已更新或条目已删除，ASR 结果作废"
            job.state = "cancelled"
            item.pipeline_state = "needs_input"
            item.state_detail = "音频转写结果作废：来源已更新（已有新材料或已删除）。"
            _emit_state(db, run)
            db.commit()
            return
        manifest = _load_manifest(run, settings)
        if manifest is None:
            _fail_run(db, run, job, item, "转写完成但清单缺失，需要重新准备。", final=True)
            db.commit()
            return
        results = _load_committed_results(settings, run.work_dir, run.chunk_count)
        if len(results) < run.chunk_count:
            _fail_run(db, run, job, item, "部分识别结果缺失，需要重新转写。", final=True)
            db.commit()
            return

        records, silence = _merge_results(manifest, results)
        video_duration = float(manifest.get("total_duration_s") or 0) or None
        segments, warnings = subfmt.normalize_records(
            records, source="machine_asr", video_duration_s=video_duration
        )
        for seg in segments:
            seg["origin"] = "asr"
            seg["confidence"] = None
        if not segments:
            # 全片无有效语音：进待补充，不用标题生成正文（docs/11 §7）
            _fail_run(db, run, job, item, "音频中没有识别到有效语音内容。", final=True)
            db.commit()
            _remove_work_dir(settings, run)
            return

        warnings = list(warnings) + [
            "文字稿由本地语音识别生成：机器转写，未经人工校对。",
        ]
        failed_ranges = _failed_ranges(manifest, results)
        locator = manifest.get("source_locator") or {}
        frozen = (run.input_json or {}).get("source") or {}
        platform = frozen.get("platform") or locator.get("platform") or "bilibili"
        media_kind = (frozen.get("media_kind") or locator.get("media_kind")
                      or ("video" if platform == "bilibili" else "audio"))
        fields = source_fields(platform, media_kind)
        # 只有真实留存的上传原件才为 true（docs/13 §6.3）
        retained = bool(run.input_kind == "object")
        asr_meta = _asr_meta(run, manifest, silence, failed_ranges, retained=retained)
        meta_updates = {
            "platform": fields["platform"],
            "media_kind": fields["media_kind"],
            "source_type": fields["source_type"],
            "source_label": fields["source_label"],
            "icon_key": fields["icon_key"],
            "title": frozen.get("title") or locator.get("title"),
            "author": frozen.get("author") or locator.get("author"),
            "published_at": frozen.get("published_at") or locator.get("published_at"),
            "canonical_url": frozen.get("canonical_url") or locator.get("canonical_url"),
            "coverage": "partial_text" if failed_ranges else "full_text",
            "original_media_retained": retained,
            "source_locator": locator,
            "asr": asr_meta,
            "extractor": {
                "name": frozen.get("adapter_id") or locator.get("adapter_id") or "audio_asr",
                "version": frozen.get("adapter_version") or locator.get("adapter_version") or "1.0.0",
                "discovery_status": "available",
                "login_state_used": platform == "bilibili",
            },
        }
        store = ObjectStore()
        extra_files = _register_asr_files(db, store, item, run, manifest, results, segments,
                                          retained=retained, subtitle_ref=subtitle_ref)
        publish_segments_revision(
            db, store, job, item, ctx_source(db, item, run),
            segments=segments, warnings=warnings, extra_files=extra_files,
            meta_updates=meta_updates,
        )
        run.state = "succeeded"
        run.pause_reason = ""
        run.updated_at = utcnow()
        _emit_state(db, run)
        run_ref = {"work_dir": run.work_dir}
        db.commit()
    if run_ref:
        _cleanup_by_work_dir(settings, run_ref["work_dir"])


def ctx_source(db: Session, item: Item, run: AsrRun) -> SourceRevision:
    source = (
        db.query(SourceRevision)
        .filter(SourceRevision.item_id == item.id, SourceRevision.revision == run.source_revision)
        .one_or_none()
    )
    if source is None:
        # _load_context 语义：发布前已确认存在；到不了就是数据不一致，
        # 抛带上下文的显式错误（assert 在 -O 下会被剥离，审查 C-15）
        raise RuntimeError(
            f"SourceRevision 缺失：item={item.id} revision={run.source_revision}"
        )
    return source


def _failed_ranges(manifest: dict, results: list[dict]) -> list[list[float]]:
    """已提交段里文本为空的区间（可能是静音或漏识；如实记录不承诺完整）。"""
    ranges = []
    by_index = {r["index"]: r for r in results}
    for chunk in manifest.get("chunks") or []:
        res = by_index.get(chunk["index"])
        if res is None or not (res.get("text") or "").strip():
            ranges.append([float(chunk["core_start"]), float(chunk["core_end"])])
    return ranges


def _asr_meta(run: AsrRun, manifest: dict, silence: list, failed_ranges: list,
              *, retained: bool = False) -> dict:
    settings = get_settings()
    locator = manifest.get("source_locator") or {}
    return {
        "source": "asr",
        "engine": "sherpa-onnx",
        "engine_bin": settings.asr_engine_bin,
        "model_id": run.model_id,
        "model_alias": run.model_alias,
        "quantization": "int8",
        "recipe_hash": run.recipe_hash,
        "chunk_seconds": manifest.get("chunk_seconds"),
        "context_seconds": settings.asr_chunk_context_seconds,
        "audio_duration_s": manifest.get("total_duration_s"),
        "processed_audio_seconds": round(float(run.processed_seconds or 0.0), 1),
        "chunk_count": run.chunk_count,
        "pcm_manifest_sha256": (run.manifest_json or {}).get("manifest_sha256"),
        # 获取方式按真实来源赋值；audio_retained 与真实存储状态一致
        "acquisition": locator.get("acquisition") or (
            ACQ_UPLOADED if run.input_kind == "object" else "player_audio_stream"),
        "audio_retained": retained,
        "input_fingerprint": run.input_fingerprint or None,
        "timestamp_kind": "estimated",
        "silence_ranges": silence,
        "failed_ranges": failed_ranges,
    }


def _register_asr_files(db, store, item, run, manifest, results, segments,
                        *, retained: bool = False,
                        subtitle_ref: list[dict] | None = None) -> list:
    """原始模型输出与执行清单进 Bundle；原件（大录音）不进自动投递文件列表。"""
    raw_doc = {
        "schema": "asr-raw-v1",
        "model_id": run.model_id,
        "recipe_hash": run.recipe_hash,
        "chunks": results,
    }
    asr_manifest = {
        "schema": "asr-manifest-v1",
        "model_id": run.model_id,
        "recipe_hash": run.recipe_hash,
        "input_kind": run.input_kind,
        "input_fingerprint": run.input_fingerprint or None,
        "pcm_manifest": manifest,
        "note": ("上传原件在服务器保留，可在条目详情下载；本次转写使用其本地副本。"
                 if retained else
                 "音频为临时输入，未长期保留；无法离线重新听原音频。"),
    }
    files = []
    for path, data, mime in (
        ("asr/asr_raw.json", pipeline.canonical_json(raw_doc), "application/json"),
        ("asr/asr_manifest.json", pipeline.canonical_json(asr_manifest), "application/json"),
        ("transcript.srt", subfmt.segments_to_srt(segments).encode("utf-8"), "application/x-subrip"),
    ):
        files.append(pipeline.register_file(
            db, store, user_id=item.user_id, item_id=item.id,
            data=data, relative_path=path, role="source_material", mime=mime,
        ))
    if subtitle_ref:
        # 平台字幕对照参考：无标点不作正文，仅供 AI 修正听错字词时对照用词
        ref_doc = {
            "schema": "asr-subtitle-ref-v1",
            "note": "平台字幕对照参考（无标点、可能省略语气词），不作为正文；"
                    "用词供 AI 修正语音识别听错字词时对照。",
            "records": subtitle_ref,
        }
        files.append(pipeline.register_file(
            db, store, user_id=item.user_id, item_id=item.id,
            data=pipeline.canonical_json(ref_doc),
            relative_path="asr/subtitle_ref.json", role="source_material",
            mime="application/json",
        ))
    if retained:
        # 只投递一个原件引用说明，不把 GB 级音频放进 Bundle（docs/13 §6.3）
        ref_doc = {
            "schema": "audio-original-ref-v1",
            "retained": True,
            "download": f"/v1/items/{item.id}/audio-original",
            "note": "原件保留在服务器；插件默认不自动下载大文件。",
        }
        files.append(pipeline.register_file(
            db, store, user_id=item.user_id, item_id=item.id,
            data=pipeline.canonical_json(ref_doc),
            relative_path="asr/original_audio.json", role="generated",
            mime="application/json",
        ))
    return files


def _cleanup_by_work_dir(settings, work_dir_rel: str) -> None:
    """发布成功后清理本任务 PCM 与临时输入（结果已进 Bundle）。"""
    base = (settings.tmp_dir / work_dir_rel).resolve()
    tmp_root = settings.tmp_dir.resolve()
    if str(base).startswith(str(tmp_root)) and base.exists():
        shutil.rmtree(base, ignore_errors=True)


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()
