"""WorkflowView：把内部状态转换为面向 Web 的四阶段用户状态（docs/17 §10）。

四阶段：extract（提取）→ process（加工）→ organize（整理）→ publish（发布）。
- 列表页与详情页共用同一转换；前端只渲染本视图，不再手写状态映射。
- reason_code 是稳定机器码，前端不得直接显示；message 是按 reason_code
  生成的用户文案，不透传 state_detail、异常字符串或第三方响应。
- 发布步骤只认当前最新 Bundle 的有效 Receipt；云端生成 Bundle 不等于已发布。
- 所有查询按条目集合批量执行，禁止逐条目查询（docs/17 §10.4）。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AsrRun, BundleRevision, Device, Item, Job, Receipt, SourceRevision

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
          progress: int | None = None) -> dict:
    return {
        "id": step_id,
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
) -> dict:
    """推导单个条目的 WorkflowView（输入均已按用户隔离批量取得）。"""
    ps = item.pipeline_state
    missing = meta.get("missing_materials") or []
    platform = meta.get("platform") or ""
    asr_active = bool(run and run.state in _ASR_ACTIVE_STATES)

    # ---- 提取 ----
    if asr_active:
        # 有转写 run：来源材料已定位落库，提取视为完成
        extract = _step("extract", "completed", "SOURCE_READY", "已取得原始内容")
    elif ps == "queued":
        extract = _step("extract", "pending", "RECEIVED", "已接收，准备提取")
    elif ps == "extracting":
        extract = _step("extract", "running", "EXTRACTING", "正在读取网页内容")
    elif ps == "needs_input":
        if platform == "bilibili":
            extract = _step("extract", "attention", "WAITING_PLATFORM_AUTH",
                            "需要连接 B 站才能读取这条内容")
        else:
            extract = _step("extract", "attention", "NEEDS_CONTENT", "需要补充正文或字幕")
    elif ps == "failed" and not (run and run.state == "failed"):
        extract = _step("extract", "failed", "EXTRACT_FAILED", "这次提取没有成功，可以重试")
    else:
        extract = _step("extract", "completed", "SOURCE_READY", "已取得原始内容")

    # ---- 加工 ----
    if run is not None:
        if run.state == "succeeded":
            process = _step("process", "completed", "PROCESS_DONE", "转写完成", 100)
        elif run.state == "failed":
            process = _step("process", "failed", "TRANSCRIBE_FAILED", "转写没有完成，可以重新转写")
        elif run.state == "cancelled":
            process = _step("process", "waiting", "PROCESS_CANCELLED",
                            "转写已取消，可以重新转写或补充内容")
        elif run.state == "paused" and run.pause_reason == "selection_required":
            process = _step("process", "attention", "SELECTION_REQUIRED", "需要选择要转写的音频")
        elif run.state == "paused":
            # 资源等待：百分比冻结，只说等待对象
            process = _step("process", "waiting", "TRANSCRIBE_PAUSED",
                            "等待服务器空闲后继续", _asr_progress(run))
        elif run.state == "preparing":
            process = _step("process", "running", "PREPARING_AUDIO", "正在准备音频")
        elif run.state == "transcribing":
            progress = _asr_progress(run)
            message = "正在转写音频" if progress is None else f"正在转写 {progress}%"
            process = _step("process", "running", "TRANSCRIBING", message, progress)
        else:  # queued：排队等待执行
            process = _step("process", "running", "TRANSCRIBE_QUEUED", "转写排队中")
    elif meta.get("media_kind") == "audio" or "transcript" in missing:
        # 音频条目但还没有转写 run：等待用户触发或补充
        process = _step("process", "waiting", "TRANSCRIBE_PENDING", "等待音频转写")
    else:
        process = _step("process", "skipped", "PROCESS_SKIPPED", "无需额外加工")

    # ---- 整理 ----
    bundle_ready = bool(bundle and bundle.processing_state == "ready")
    bundle_stale = bool(bundle_ready and bundle.source_revision != item.source_revision)
    if bundle_ready and not bundle_stale:
        organize = _step("organize", "completed", "ORGANIZE_DONE", "已生成整理结果")
    elif bundle and bundle.processing_state == "failed" and bundle.source_revision == item.source_revision:
        organize = _step("organize", "failed", "ORGANIZE_FAILED", "这次整理没有成功，可以重新整理")
    elif ps == "waiting_key":
        organize = _step("organize", "attention", "WAITING_MODEL", "需要选择整理模型")
    elif ps == "unknown_outcome":
        organize = _step("organize", "waiting", "ORGANIZE_UNKNOWN",
                         "暂时无法确认整理是否完成，系统正在核对结果")
    elif ps == "enriching":
        organize = _step("organize", "running", "ORGANIZING", "正在整理内容")
    elif ps == "extracted" and not auto_enrich:
        organize = _step("organize", "attention", "AUTO_ORGANIZE_OFF",
                         "原文已就绪，自动整理已关闭")
    elif ps == "extracted":
        organize = _step("organize", "pending", "WAITING_FOR_ORGANIZE", "等待整理开始")
    elif bundle_stale:
        organize = _step("organize", "attention", "STALE_ORGANIZE", "原始内容已经更新，需要重新整理")
    elif ps == "failed" and run is not None and run.state == "failed":
        organize = _step("organize", "pending", "WAITING_FOR_PROCESS", "等待转写完成后整理")
    elif ps == "failed":
        organize = _step("organize", "pending", "WAITING_FOR_EXTRACT", "等待提取完成后整理")
    elif ps in ("needs_input",):
        organize = _step("organize", "pending", "WAITING_FOR_CONTENT", "等待补充内容后整理")
    elif asr_active:
        organize = _step("organize", "pending", "WAITING_FOR_PROCESS", "等待加工完成")
    elif ps in ("queued", "extracting"):
        organize = _step("organize", "pending", "WAITING_FOR_EXTRACT", "等待提取完成")
    else:
        organize = _step("organize", "pending", "WAITING_FOR_ORGANIZE", "等待整理开始")

    # ---- 发布（只认当前最新 Bundle 的有效回执）----
    receipt_valid = bool(
        receipt is not None and bundle is not None
        and receipt.manifest_sha256 == bundle.manifest_sha256
        and receipt.bundle_revision == bundle.revision
    )
    if bundle is None:
        publish = _step("publish", "pending", "WAITING_FOR_ORGANIZE", "等待整理完成")
        delivery = {"status": "not_ready", "bundle_revision": None,
                    "received_at": None, "device_id": None, "device_name": None}
    elif receipt_valid:
        publish = _step("publish", "completed", "PUBLISHED", "已发布到 Obsidian", 100)
        delivery = {"status": "published", "bundle_revision": bundle.revision,
                    "received_at": receipt.received_at.isoformat(),
                    "device_id": receipt.device_id, "device_name": None}
    elif has_device:
        publish = _step("publish", "waiting", "WAITING_OBSIDIAN", "等待 Obsidian 下载")
        delivery = {"status": "waiting_obsidian", "bundle_revision": bundle.revision,
                    "received_at": None, "device_id": None, "device_name": None}
    else:
        publish = _step("publish", "attention", "CONNECT_OBSIDIAN", "连接 Obsidian 后自动发布")
        delivery = {"status": "connect_obsidian", "bundle_revision": bundle.revision,
                    "received_at": None, "device_id": None, "device_name": None}

    steps = [extract, process, organize, publish]

    # ---- 聚合状态（docs/17 §5.2）----
    order = {"pending": 0, "skipped": 0, "completed": 0, "waiting": 1,
             "running": 1, "attention": 2, "failed": 3}
    overall = "published"
    if any(s["status"] == "failed" for s in steps):
        overall = "failed"
    elif any(s["status"] == "attention" for s in steps):
        overall = "attention"
    elif any(s["status"] in ("running", "waiting") for s in steps):
        overall = "working"
    elif publish["status"] == "completed":
        overall = "published"
    else:
        overall = "working"

    current = next((s for s in steps if s["status"] in ("running", "waiting", "attention", "failed")),
                   publish)

    primary = _primary_action(current, extract, organize, delivery)
    available = _available_actions(item, meta, steps, run, active_job, delivery, auto_enrich)

    return {
        "version": WORKFLOW_VERSION,
        "overall_state": overall,
        "current_stage": current["id"],
        "reason_code": current["reason_code"],
        "message": current["message"],
        "progress_percent": current["progress_percent"] if overall == "working" else None,
        "requires_user_action": overall == "attention",
        "primary_action": primary,
        "available_actions": available,
        "steps": steps,
        "delivery": delivery,
    }


def _primary_action(current: dict, extract: dict, organize: dict, delivery: dict) -> str | None:
    """同一时间最多一个主按钮（docs/17 §2.2）：从当前阶段推导唯一动作。"""
    status = current["status"]
    if status == "attention":
        if current["reason_code"] in ("NEEDS_CONTENT", "WAITING_PLATFORM_AUTH"):
            return "supplement"
        if current["reason_code"] == "WAITING_MODEL":
            return "choose_model"
        if current["reason_code"] == "AUTO_ORGANIZE_OFF":
            return "start_organize"
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
                       delivery: dict, auto_enrich: bool) -> list[str]:
    """服务端按真实状态给出可用操作；前端不猜（docs/17 §10.2）。"""
    steps_by_id = {s["id"]: s for s in steps}
    extract = steps_by_id["extract"]
    process = steps_by_id["process"]
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
    if organize["reason_code"] == "WAITING_MODEL":
        acts.append("choose_model")
    if organize["reason_code"] in ("AUTO_ORGANIZE_OFF", "STALE_ORGANIZE", "ORGANIZE_FAILED"):
        acts.append("start_organize")
    if delivery["status"] == "connect_obsidian":
        acts.append("connect_obsidian")
    return acts


def collect_workflow_inputs(db: Session, user_id: str, items: list[Item]) -> dict:
    """批量取得推导所需数据（每类一次查询，禁止 N+1，docs/17 §10.4）。"""
    ids = [it.id for it in items]
    out: dict = {
        "metas": {}, "runs": {}, "bundles": {}, "receipts": {},
        "jobs": {}, "has_device": False, "device_names": {},
        "auto_enrich": True,
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

    from ..workers.publish import auto_enrich_enabled
    out["auto_enrich"] = auto_enrich_enabled(db, user_id)
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
        )
        if wf["delivery"]["device_id"]:
            wf["delivery"]["device_name"] = inputs["device_names"].get(wf["delivery"]["device_id"])
        result[it.id] = wf
    return result


# ---- 首页三视图的候选状态集合（SQL 近似 + Python 精筛）----
# attention：需要用户处理（含 extracted=自动整理已关闭；ready 候选交给精筛剔除已发布）
VIEW_CANDIDATE_STATES = {
    "attention": ("needs_input", "waiting_key", "failed", "extracted", "ready"),
    "working": ("queued", "extracting", "enriching", "ready"),
    "published": ("ready",),
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
        {"stage": s["id"], "stage_label": stage_labels[s["id"]],
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
