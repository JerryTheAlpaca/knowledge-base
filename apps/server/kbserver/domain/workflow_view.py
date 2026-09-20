"""WorkflowView：把内部状态转换为面向 Web 的用户状态节点（docs/17 §10）。

节点：extract（提取）→ [process（语音识别，仅会用到 ASR 的条目渲染）] →
organize（整理）→ publish（发布）。节点数量按条目动态：非 ASR 条目只有三节点。
- 列表页与详情页共用同一转换；前端只渲染本视图，不再手写状态映射。
- reason_code 是稳定机器码，前端不得直接显示；message 是按 reason_code
  生成的用户文案，不透传 state_detail、异常字符串或第三方响应。
- 发布步骤认两种完成：当前最新 Bundle 的有效 Receipt（已落到 Obsidian），
  或用户在网页把当前版本的原文下载走。云端生成 Bundle 本身不等于已完成。
- 所有查询按条目集合批量执行，禁止逐条目查询（docs/17 §10.4）。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AsrRun, BundleRevision, Device, Item, Job, Receipt, SourceRevision
from . import platform_sessions

WORKFLOW_VERSION = "1.0"

STAGES = ("extract", "process", "organize", "publish")

# AsrRun 的活动中状态；此间加工阶段视为进行中/等待
_ASR_ACTIVE_STATES = ("queued", "preparing", "transcribing", "paused")
# 等待服务器资源类暂停（等待对象：服务器空闲）；selection_required 单独处理
_ASR_RESOURCE_PAUSES = {
    "idle_wait", "resource_busy", "disabled", "metrics_unavailable",
    "idle_window_filling", "cpu_busy", "memory_low", "normal_jobs_active",
}


def _step(step_id: str, status: str, reason: str, message: str,
          progress: int | None = None, label: str | None = None) -> dict:
    return {
        "id": step_id,
        # 阶段显示名由服务端权威生成：语音识别节点（仅 ASR 条目渲染）标注具体
        # 处理方式；None 时前端回退到固定阶段名
        "label": label,
        "status": status,  # pending|running|completed|skipped|waiting|attention|failed
        "reason_code": reason,
        "message": message,
        "progress_percent": progress,
    }


def _asr_progress(run: AsrRun) -> int | None:
    """真实百分比：floor(done/count×100)；总量未确定返回 None，不伪造。"""
    count = int(run.chunk_count or 0)
    if count <= 0:
        return None
    return min(99, int(run.next_chunk_index or 0) * 100 // count)


def _extract_attention(item: Item, platform: str, session_platforms: set[str]) -> dict:
    """提取阶段的「需要你处理」。

    登录墙按 Item.state_reason 机器码判定，不解析 state_detail 的中文文案
    （审查 C-14）；只有设置页确实开放了该平台登录态入口时才给连接/更新动作。
    """
    spec = platform_sessions.SPECS.get(platform)
    if (item.state_reason == "login_required" and spec is not None
            and platform in platform_sessions.SESSION_UI_PLATFORMS):
        if platform in session_platforms:
            return _step("extract", "attention", "WAITING_SESSION_UPDATE",
                         spec.session_update_hint.format(label=spec.label), label="提取")
        return _step("extract", "attention", "WAITING_PLATFORM_AUTH",
                     spec.auth_hint.format(label=spec.label), label="提取")
    return _step("extract", "attention", "NEEDS_CONTENT", "需要补充正文或字幕", label="提取")


def derive_item_workflow(
    *,
    item: Item,
    meta: dict,
    run: AsrRun | None,
    bundle: BundleRevision | None,
    receipt: Receipt | None,
    has_device: bool,
    active_job: Job | None,
    auto_enrich: bool,
    ai_paragraphing: bool,
    session_platforms: frozenset[str] = frozenset(),
) -> dict:
    """推导单个条目的 WorkflowView（输入均已按用户隔离批量取得）。"""
    ps = item.pipeline_state
    missing = meta.get("missing_materials") or []
    platform = meta.get("platform") or ""
    asr_active = bool(run and run.state in _ASR_ACTIVE_STATES)

    # ---- 提取 ----
    if asr_active:
        # 有转写 run：来源材料已定位落库，提取视为完成
        extract = _step("extract", "completed", "SOURCE_READY", "已取得原始内容", label="提取")
    elif ps == "queued":
        extract = _step("extract", "pending", "RECEIVED", "已接收，准备提取", label="提取")
    elif ps == "extracting":
        extract = _step("extract", "running", "EXTRACTING", "正在读取网页内容", label="提取")
    elif ps == "needs_input":
        extract = _extract_attention(item, platform, session_platforms)
    elif ps == "failed" and not (run and run.state == "failed"):
        extract = _step("extract", "failed", "EXTRACT_FAILED", "这次提取没有成功，可以重试", label="提取")
    else:
        extract = _step("extract", "completed", "SOURCE_READY", "已取得原始内容", label="提取")

    # ---- 语音识别（docs/17 §5.1）：仅会用到 ASR 的条目渲染此节点（录音、网页音轨、
    # B 站无字幕音轨转写）；网页正文/B 站字幕在提取时已是文字，不渲染该节点。
    # 未来 OCR 实装后，图片条目才出现「提取文字」节点。
    process = None
    if run is not None or meta.get("media_kind") == "audio" or "transcript" in missing:
        if run is not None and run.state == "succeeded":
            process = _step("process", "completed", "PROCESS_DONE", "转写完成", 100, label="语音识别")
        elif run is not None and run.state == "failed":
            process = _step("process", "failed", "TRANSCRIBE_FAILED",
                            "语音识别没有完成，可以重新识别", label="语音识别")
        elif run is not None and run.state == "cancelled":
            process = _step("process", "waiting", "PROCESS_CANCELLED",
                            "语音识别已取消，可以重新识别或补充内容", label="语音识别")
        elif run is not None and run.state == "paused" and run.pause_reason == "selection_required":
            process = _step("process", "attention", "SELECTION_REQUIRED",
                            "需要选择要识别的音频", label="语音识别")
        elif run is not None and run.state == "paused":
            # 资源等待：百分比冻结，只说等待对象
            process = _step("process", "waiting", "TRANSCRIBE_PAUSED",
                            "等待服务器空闲后继续", _asr_progress(run), label="语音识别")
        elif run is not None and run.state == "preparing":
            # 非终态失败只把任务放回 retry_wait，run 仍停在 preparing，看起来就是
            # 「一直卡在同一步」；retry_prepare 由 _fail_run 置位、下一次真正
            # 开始执行时清掉，用它区分「正在读取」和「读但一直失败」。
            if run.pause_reason == "retry_prepare":
                process = _step("process", "running", "PREPARING_RETRY",
                                f"读取音频没成功，正在第 {run.failed_count + 1} 次重试",
                                label="语音识别")
            else:
                process = _step("process", "running", "PREPARING_AUDIO", "正在准备音频",
                                label="语音识别")
        elif run is not None and run.state == "transcribing":
            progress = _asr_progress(run)
            if run.pause_reason == "retry_transcribe":
                process = _step("process", "running", "TRANSCRIBING_RETRY",
                                f"这一段识别没成功，正在第 {run.failed_count + 1} 次重试",
                                progress, label="语音识别")
            else:
                message = "正在语音识别" if progress is None else f"正在语音识别 {progress}%"
                process = _step("process", "running", "TRANSCRIBING", message, progress,
                                label="语音识别")
        elif run is not None:  # queued：排队等待执行
            process = _step("process", "running", "TRANSCRIBE_QUEUED", "语音识别排队中", label="语音识别")
        else:
            # 音频条目但还没有转写 run：等待用户触发或补充
            process = _step("process", "waiting", "TRANSCRIBE_PENDING", "等待语音识别", label="语音识别")

    # ---- 整理 ----
    bundle_ready = bool(bundle and bundle.processing_state == "ready")
    bundle_stale = bool(bundle_ready and bundle.source_revision != item.source_revision)
    # 「需不需要你处理」只看现在的开关，不追溯历史：自动整理关着时整理这一步对用户不存在，
    # 材料已经取到手的条目——包括当年开着开关时留下「等模型凭据」的、上一次自动整理失败的、
    # 原文后来更新过的——一律按已通过显示，条目落到待发布。要整理随时可以开开关或手动重新加工，
    # 那时它才重新变成需要你处理的事。
    organize_off = not auto_enrich and (
        ps in ("extracted", "waiting_key", "ready")
        or (bundle is not None and bundle.processing_state == "failed")
    )
    if bundle_ready and not bundle_stale:
        organize = _step("organize", "completed", "ORGANIZE_DONE", "已生成整理结果", label="整理")
    elif ps == "enriching" and not auto_enrich:
        # 自动整理关着但文字优化在跑：这一步不是「整理中」，照实说在做什么
        organize = _step("organize", "running", "OPTIMIZING_TEXT", "正在优化文字",
                         label="整理")
    elif organize_off:
        organize = _step("organize", "skipped", "AUTO_ORGANIZE_OFF", "自动整理已关闭，直接取用原文",
                         label="整理")
    elif bundle and bundle.processing_state == "failed" and bundle.source_revision == item.source_revision:
        organize = _step("organize", "failed", "ORGANIZE_FAILED", "这次整理没有成功，可以重新整理", label="整理")
    elif ps == "waiting_key":
        organize = _step("organize", "attention", "WAITING_MODEL", "需要选择整理模型", label="整理")
    elif ps == "unknown_outcome":
        organize = _step("organize", "waiting", "ORGANIZE_UNKNOWN",
                         "暂时无法确认整理是否完成，系统正在核对结果", label="整理")
    elif ps == "enriching":
        organize = _step("organize", "running", "ORGANIZING", "正在整理内容", label="整理")
    elif ps == "extracted":
        organize = _step("organize", "pending", "WAITING_FOR_ORGANIZE", "等待整理开始", label="整理")
    elif bundle_stale:
        organize = _step("organize", "attention", "STALE_ORGANIZE", "原始内容已经更新，需要重新整理", label="整理")
    elif ps == "failed" and run is not None and run.state == "failed":
        organize = _step("organize", "pending", "WAITING_FOR_PROCESS", "等待语音识别完成后整理", label="整理")
    elif ps == "failed":
        organize = _step("organize", "pending", "WAITING_FOR_EXTRACT", "等待提取完成后整理", label="整理")
    elif ps in ("needs_input",):
        organize = _step("organize", "pending", "WAITING_FOR_CONTENT", "等待补充内容后整理", label="整理")
    elif asr_active:
        organize = _step("organize", "pending", "WAITING_FOR_PROCESS", "等待语音识别完成", label="整理")
    elif ps in ("queued", "extracting"):
        organize = _step("organize", "pending", "WAITING_FOR_EXTRACT", "等待提取完成", label="整理")
    else:
        organize = _step("organize", "pending", "WAITING_FOR_ORGANIZE", "等待整理开始", label="整理")

    # ---- 发布（只认当前最新 Bundle 的有效回执）----
    receipt_valid = bool(
        receipt is not None and bundle is not None
        and receipt.manifest_sha256 == bundle.manifest_sha256
        and receipt.bundle_revision == bundle.revision
    )
    if bundle is None:
        publish = _step("publish", "pending", "WAITING_FOR_ORGANIZE", "等待整理完成", label="发布")
        delivery = {"status": "not_ready", "bundle_revision": None,
                    "received_at": None, "device_id": None, "device_name": None}
    elif receipt_valid:
        # 文案只说发生了什么：材料到了本机就是「已下载」，不写「已发布」
        publish = _step("publish", "completed", "PUBLISHED", "已下载到 Obsidian", 100, label="发布")
        delivery = {"status": "published", "bundle_revision": bundle.revision,
                    "received_at": receipt.received_at.isoformat(),
                    "device_id": receipt.device_id, "device_name": None}
    elif bundle.revision == item.original_download_bundle:
        # 用户在网页把原文下载走了：同样算拿到手，但没有经过 Obsidian
        publish = _step("publish", "completed", "SOURCE_DOWNLOADED", "原文已下载", 100, label="发布")
        delivery = {"status": "downloaded", "bundle_revision": bundle.revision,
                    "received_at": None, "device_id": None, "device_name": None}
    elif has_device:
        publish = _step("publish", "waiting", "WAITING_OBSIDIAN", "等待 Obsidian 下载", label="发布")
        delivery = {"status": "waiting_obsidian", "bundle_revision": bundle.revision,
                    "received_at": None, "device_id": None, "device_name": None}
    else:
        publish = _step("publish", "attention", "CONNECT_OBSIDIAN", "连接 Obsidian 后自动发布", label="发布")
        delivery = {"status": "connect_obsidian", "bundle_revision": bundle.revision,
                    "received_at": None, "device_id": None, "device_name": None}

    steps = [extract] + ([process] if process is not None else []) + [organize, publish]

    # ---- 聚合状态（docs/17 §5.2）----
    # skipped 与 pending 都不算需要处理，也不把条目停在原地：聚合按
    # failed > attention > working > published 取第一个命中的档位。
    overall = "working"
    if any(s["status"] == "failed" for s in steps):
        overall = "failed"
    elif any(s["status"] == "attention" for s in steps):
        overall = "attention"
    elif any(s["status"] in ("running", "waiting") for s in steps):
        overall = "working"
    elif publish["status"] == "completed":
        overall = "published"

    current = next((s for s in steps if s["status"] in ("running", "waiting", "attention", "failed")),
                   publish)

    primary = _primary_action(current, extract, organize, delivery)
    available = _available_actions(item, meta, steps, run, active_job, delivery, auto_enrich,
                                   ai_paragraphing)

    return {
        "version": WORKFLOW_VERSION,
        "overall_state": overall,
        "current_stage": current["id"],
        "reason_code": current["reason_code"],
        "message": current["message"],
        "progress_percent": current["progress_percent"] if overall == "working" else None,
        "requires_user_action": overall == "attention",
        # 是否真有任务在跑或排队：列表轮询按它决定 4s/30s（审查 C-06）。
        # 不能用 overall_state=="working" 代替——「等待 Obsidian 下载」也是
        # working，但那时服务器没有在干活，前台没必要每 4s 拉一次。
        "has_active_job": bool(active_job
                               and active_job.state in ("queued", "retry_wait", "running")),
        "primary_action": primary,
        "available_actions": available,
        "steps": steps,
        "delivery": delivery,
    }


def _primary_action(current: dict, extract: dict, organize: dict, delivery: dict) -> str | None:
    """同一时间最多一个主按钮（docs/17 §2.2）：从当前阶段推导唯一动作。"""
    status = current["status"]
    if status == "attention":
        if current["reason_code"] == "NEEDS_CONTENT":
            return "supplement"
        if current["reason_code"] == "WAITING_PLATFORM_AUTH":
            return "connect_platform"
        if current["reason_code"] == "WAITING_SESSION_UPDATE":
            return "update_session"
        if current["reason_code"] == "WAITING_MODEL":
            return "choose_model"
        if current["reason_code"] == "STALE_ORGANIZE":
            return "start_organize"
        if current["reason_code"] == "CONNECT_OBSIDIAN":
            return "connect_obsidian"
        if current["reason_code"] == "SELECTION_REQUIRED":
            return None  # 选择音频走「更多操作」，不占主按钮
    if status == "failed":
        return "retry"
    return None


def _available_actions(item: Item, meta: dict, steps: dict[str, dict] | list,
                       run: AsrRun | None, active_job: Job | None,
                       delivery: dict, auto_enrich: bool,
                       ai_paragraphing: bool) -> list[str]:
    """服务端按真实状态给出可用操作；前端不猜（docs/17 §10.2）。"""
    steps_by_id = {s["id"]: s for s in steps}
    extract = steps_by_id["extract"]
    organize = steps_by_id["organize"]
    acts = ["view_source"]
    if meta.get("original_url") and extract["status"] in ("completed", "attention", "failed"):
        acts.append("refetch")
    if run is not None and run.state in _ASR_ACTIVE_STATES:
        acts.append("cancel_process")
    if run is not None and run.state in ("failed", "cancelled"):
        acts.append("retry_process")
    if extract["status"] == "attention":
        acts.append("supplement")
        # 登录墙：先给连接/更新登录态，补充材料仍是兜底路径（docs/18 §7.7）
        if extract["reason_code"] == "WAITING_PLATFORM_AUTH":
            acts.append("connect_platform")
        elif extract["reason_code"] == "WAITING_SESSION_UPDATE":
            acts.append("update_session")
    if organize["reason_code"] == "WAITING_MODEL":
        acts.append("choose_model")
    if organize["reason_code"] in ("AUTO_ORGANIZE_OFF", "STALE_ORGANIZE", "ORGANIZE_FAILED"):
        acts.append("start_organize")
    # 手动「开始优化文本」：始终显示，除非条目还没提取完、需要补充材料、
    # 或已经整理完成（ready 状态说明已有整理结果，不需要再优化原文）
    if item.pipeline_state not in {"queued", "extracting", "needs_input", "ready"}:
        acts.append("start_optimize_text")
    if delivery["status"] == "connect_obsidian":
        acts.append("connect_obsidian")
    return acts


def collect_workflow_inputs(db: Session, user_id: str, items: list[Item]) -> dict:
    """批量取得推导所需数据（每类一次查询，禁止 N+1，docs/17 §10.4）。"""
    ids = [it.id for it in items]
    out: dict = {
        "metas": {}, "runs": {}, "bundles": {}, "receipts": {},
        "jobs": {}, "has_device": False, "device_names": {},
        "auto_enrich": True, "session_platforms": frozenset(),
    }
    if not ids:
        return out

    # 来源版本元数据（missing_materials / platform / media_kind 等）
    revs = sorted({it.source_revision for it in items})
    for r in db.query(SourceRevision).filter(
        SourceRevision.item_id.in_(ids), SourceRevision.revision.in_(revs)
    ).all():
        if r.item_id not in out["metas"] or r.revision > out["metas"][r.item_id].revision:
            out["metas"][r.item_id] = r

    # 每条目最新 AsrRun（updated_at 最新优先）
    for run in db.query(AsrRun).filter(
        AsrRun.user_id == user_id, AsrRun.item_id.in_(ids)
    ).order_by(AsrRun.updated_at.asc(), AsrRun.created_at.asc()).all():
        out["runs"][run.item_id] = run  # 升序遍历后覆盖 = 最新

    # 当前最新 Bundle（item.bundle_revision 即最新已发布版本）
    for b in db.query(BundleRevision).filter(
        BundleRevision.user_id == user_id, BundleRevision.item_id.in_(ids)
    ).order_by(BundleRevision.revision.asc()).all():
        out["bundles"][b.item_id] = b  # 升序覆盖 = 最新

    # 当前 Bundle 对应的回执（不限定设备：任一设备的有效回执即已发布）
    for r in db.query(Receipt).filter(
        Receipt.user_id == user_id, Receipt.item_id.in_(ids)
    ).order_by(Receipt.received_at.asc()).all():
        out["receipts"][(r.item_id, r.bundle_revision)] = r

    # 每条目最新任务（失败原因 / 重试等待用）
    for j in db.query(Job).filter(
        Job.user_id == user_id, Job.item_id.in_(ids)
    ).order_by(Job.created_at.asc(), Job.id.asc()).all():
        out["jobs"][j.item_id] = j

    # 用户可用桌面设备摘要
    devices = db.scalars(select(Device).where(
        Device.user_id == user_id, Device.kind == "desktop", Device.revoked_at.is_(None)
    )).all()
    out["has_device"] = bool(devices)
    out["device_names"] = {d.id: d.name for d in devices}
    # 每用户一次查询：哪些平台已托管活跃登录态（决定「连接」还是「更新」动作）
    out["session_platforms"] = platform_sessions.configured_platforms(db, user_id)

    from ..workers.publish import auto_enrich_enabled, ai_paragraphing_enabled
    out["auto_enrich"] = auto_enrich_enabled(db, user_id)
    out["ai_paragraphing"] = ai_paragraphing_enabled(db, user_id)
    return out


def build_workflow_map(db: Session, user_id: str, items: list[Item],
                       inputs: dict | None = None) -> dict[str, dict]:
    """批量推导 WorkflowView：{item_id: workflow}。"""
    if inputs is None:
        inputs = collect_workflow_inputs(db, user_id, items)
    result: dict[str, dict] = {}
    for it in items:
        meta_row = inputs["metas"].get(it.id)
        meta = (meta_row.metadata_json if meta_row is not None else {}) or {}
        bundle = inputs["bundles"].get(it.id)
        receipt = inputs["receipts"].get((it.id, it.bundle_revision)) if bundle else None
        wf = derive_item_workflow(
            item=it, meta=meta,
            run=inputs["runs"].get(it.id),
            bundle=bundle,
            receipt=receipt,
            has_device=inputs["has_device"],
            active_job=inputs["jobs"].get(it.id),
            auto_enrich=inputs["auto_enrich"],
            ai_paragraphing=inputs["ai_paragraphing"],
            session_platforms=inputs.get("session_platforms", frozenset()),
        )
        if wf["delivery"]["device_id"]:
            wf["delivery"]["device_name"] = inputs["device_names"].get(wf["delivery"]["device_id"])
        result[it.id] = wf
    return result


# ---- 首页三视图的候选状态集合（SQL 近似 + Python 精筛）----
# 提取没成功（needs_input / failed）不可能已经是终态，所以不进 published。
# waiting_key 是「提取成功、在等模型凭据」，它归哪一组只看现在的开关：自动整理开着时是
# 需要你处理，关着时整理这一步对用户不存在——材料直接取用原文，就是待发布，被插件取走
# 就是已完成。三种视图都得先把它捞进候选，再由聚合状态定归属。
VIEW_CANDIDATE_STATES = {
    "attention": ("needs_input", "waiting_key", "failed", "extracted", "ready"),
    "working": ("queued", "extracting", "enriching", "extracted", "ready", "waiting_key"),
    "published": ("enriching", "extracted", "ready", "waiting_key"),
}
VIEW_OVERALL = {
    "attention": {"attention", "failed"},
    "working": {"working"},
    "published": {"published"},
}


def build_diagnostics(db: Session, user_id: str, item: Item,
                      source: SourceRevision | None) -> dict:
    """处理记录（docs/17 §10.3）：白话摘要 + 解释 + 脱敏技术信息（默认折叠）。"""
    inputs = collect_workflow_inputs(db, user_id, [item])
    wf = build_workflow_map(db, user_id, [item], inputs)[item.id]
    stage_labels = {"extract": "提取", "process": "加工", "organize": "整理", "publish": "发布"}
    status_labels = {
        "pending": "未开始", "running": "进行中", "completed": "已完成", "skipped": "无需此步",
        "waiting": "等待中", "attention": "需要你处理", "failed": "失败",
    }
    summary = [
        {"stage": s["id"], "stage_label": s.get("label") or stage_labels[s["id"]],
         "status": s["status"], "status_label": status_labels.get(s["status"], s["status"]),
         "message": s["message"]}
        for s in wf["steps"]
    ]
    meta_row = inputs["metas"].get(item.id)
    meta = (meta_row.metadata_json if meta_row is not None else {}) or {}
    run = inputs["runs"].get(item.id)
    bundle = inputs["bundles"].get(item.id)
    job = inputs["jobs"].get(item.id)

    technical: dict = {
        "content_version": item.source_revision,
        "content_version_label": f"内容版本 {item.source_revision}",
        "digest_version": bundle.revision if bundle else 0,
        "digest_version_label": (f"整理结果版本 {bundle.revision}" if bundle else "尚无整理结果"),
        "pipeline_state": item.pipeline_state,
        "state_detail": _redact(item.state_detail or ""),
        "delivery": {
            "bundle_revision": wf["delivery"]["bundle_revision"],
            "receipt_received": wf["delivery"]["status"] == "published",
            "received_at": wf["delivery"]["received_at"],
            "source_download_bundle": item.original_download_bundle or None,
        },
    }
    if job is not None:
        technical["latest_job"] = {
            "stage": job.stage,
            "state": job.state,
            "attempt": job.attempt,
            "last_error": _redact((job.last_error or "")[:200]),
        }
    if run is not None:
        technical["asr"] = {
            "state": run.state,
            "pause_reason": run.pause_reason or None,
            "done_chunks": run.next_chunk_index,
            "chunk_count": run.chunk_count,
            "model_alias": run.model_alias,
            "last_error": _redact((run.last_error or "")[:200]),
        }
    if meta.get("missing_materials"):
        technical["missing_materials"] = meta["missing_materials"]
    return {
        "summary": summary,
        "explanation": ("下面的信息用于排查问题，一般不需要处理。"
                        "内容版本表示服务器保存的第几次修改，用来避免旧结果覆盖新内容。"),
        "technical_records": technical,
        "workflow": wf,
    }


_SECRET_PATTERNS = (
    "sessdata=", "authorization:", "bearer ", "api_key=", "apikey=", "sk-", "token=",
)


def _redact(text: str) -> str:
    """技术记录脱敏：去掉疑似密钥片段，只保留错误类别语义。"""
    lowered = (text or "").lower()
    for pat in _SECRET_PATTERNS:
        idx = lowered.find(pat)
        if idx >= 0:
            return text[:idx].rstrip(" ,;：:") + "…（已省略敏感信息）"
    return text or ""
