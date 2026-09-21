// item-detail.js — 条目详情（docs/17 §7）
//
// 只保留三个用户概念：整理结果 / 原始内容 / 更多操作（§7.1）。
// 状态图与阶段面板只渲染服务端 WorkflowView；同一时间最多一个主按钮。
// 处理记录采用「白话摘要 → 解释 → 折叠技术信息」三级结构（§7.5）。

import { $, api, esc, toast, showErr, confirmModal, fmtTime, uploadFiles,
         sanitizeFilename, openModalHTML, closeModal } from "./api.js";
import { stepperHTML, stagePanelHTML, actionLabel, availableActionLabels, STAGE_LABELS } from "./workflow.js";
import { refreshItems as refreshList, closeDrawer, openDrawer, setListPollPaused } from "./item-list.js";

let detailId = null;
let detailData = null;
let detailTab = "auto";
let detailLoading = false;
let detailSeq = 0;
let asrStatus = null;
let sourceEditing = false;
let supFilesState = [];
let onboardingWasVisible = false;
const SEG_PAGE = 60;
let segShown = 0;

const COVERAGE_WARN = {
  partial_text: "只取得部分正文",
  user_excerpt: "只有用户摘录，未取得原文全文",
  screenshots_only: "只有截图，未取得原文",
  transcript_only: "只有字幕/文字稿，不含视频画面",
  metadata_only: "没有取得正文，只有链接信息",
};

// 缺失材料的用户说法（语言契约：枚举值不进入界面，docs/17 §2.4）
const MISSING_LABELS = {
  main_content: "正文",
  transcript: "字幕或文字稿",
  ocr_text: "图片中的文字",
  subtitle_file: "字幕文件",
};
const missingLabel = (m) => MISSING_LABELS[m] || m;

function mayHaveAudio(it) {
  if (!it) return false;
  if (it.source_type === "bilibili" || it.platform === "bilibili") return true;
  if (it.source_type === "audio_upload" || it.platform === "audio_upload" || it.media_kind === "audio") return true;
  return !!(it.platform === "web" && it.original_url);
}

// ---------- 打开/关闭 ----------
// 定时刷新每 4s 跑一次：内容与上次相同时绝不重写 DOM，避免动画重启导致闪烁
function setHTML(el, html) {
  if (el._lastHTML === html) return;
  el._lastHTML = html;
  el.innerHTML = html;
}

export async function openDetail(itemId, opts = {}) {
  detailId = itemId;
  detailSeq++;
  asrStatus = null;
  detailTab = "auto";
  segShown = 0;
  sourceEditing = false;
  supFilesState = [];
  // 详情是二级视图：抽屉/金蔷薇/采集框整体让位（docs/17 §3）
  onboardingWasVisible = !$("onboardingHost").hidden;
  $("onboardingHost").hidden = true;
  $("homeMain").hidden = true;
  closeDrawer();
  $("detailView").hidden = false;
  setHTML($("detailBody"), "");
  setHTML($("stepperHost"), "");
  setHTML($("stagePanelHost"), "");
  setListPollPaused(true);   // 列表被详情盖住，停掉它的轮询（审查 C-06）
  $("detailTitle").textContent = "加载中…";
  $("detailMeta").textContent = "";
  $("moreMenu").hidden = true;
  window.scrollTo({ top: 0, behavior: "smooth" });
  if (opts.push !== false) {
    try { history.pushState({ item: itemId }, "", "/inbox?item=" + encodeURIComponent(itemId)); }
    catch (e) { /* 忽略 */ }
  }
  await refreshDetail();
}

export function closeDetail() {
  detailId = null; detailData = null; asrStatus = null;
  detailExiting = false;
  setListPollPaused(false);
  $("detailView").hidden = true;
  $("homeMain").hidden = false;
  if (onboardingWasVisible) $("onboardingHost").hidden = false;
  window.scrollTo({ top: 0 });
}

// 详情退出统一回到进入前的条目列表（走查反馈：返回/删除都应回列表页）
let detailExiting = false;
function exitToList() {
  if (detailExiting) return;
  detailExiting = true;
  const st = history.state;
  if (st && st.item && detailId && st.item === detailId) {
    // 当前历史条目就是详情推入的：退回上一条（/inbox），popstate 里 closeDetail
    history.back();
    setTimeout(() => { detailExiting = false; }, 600);
  } else {
    // 深链/刷新进入：原地替换 URL 并直接关闭
    try { history.replaceState({}, "", "/inbox"); } catch (e) {}
    closeDetail();
  }
  // 等「点外部收起抽屉」的 document click 处理完再开抽屉，
  // 否则本次返回点击冒泡到 document 会把刚打开的抽屉当成点外部秒关
  setTimeout(() => openDrawer(), 0);
}

export function exitDetail() {
  exitToList();
}

function isDetailBusy() {
  const wf = detailData && detailData.item && detailData.item.workflow;
  return !!wf && wf.overall_state === "working";
}
export function currentDetailId() { return detailId; }

// ---------- 数据 ----------
async function refreshDetail() {
  if (!detailId) return;
  detailLoading = true;
  const seq = detailSeq;
  try {
    detailData = await api("/v1/items/" + encodeURIComponent(detailId) + "/reading");
    if (seq !== detailSeq) return;
    renderDetail();
    loadAsrStatus();
  } catch (e) {
    if (e.status === 404 || e.status === 410) { toast("这条内容不存在或已删除", { type: "warn" }); exitDetail(); }
    else showErr(e);
  }
  detailLoading = false;
}

async function loadAsrStatus() {
  const id = detailId, seq = detailSeq;
  if (!id || !detailData || !mayHaveAudio(detailData.item)) return;
  let asr = null;
  try { asr = await api("/v1/items/" + encodeURIComponent(id) + "/asr"); }
  catch (e) { return; }
  if (seq !== detailSeq || detailId !== id) return;
  asrStatus = asr || null;
  renderHeader(detailData.item);
  if (((asrStatus && asrStatus.audio_candidates) || []).length) renderDetail();
}

// 轻量刷新：只重画头部（状态图/阶段面板），不动页签内容，保护正在输入的文字
async function refreshDetailStatus() {
  if (!detailId || !detailData || $("detailView").hidden) return;
  const seq = detailSeq;
  let reading;
  try { reading = await api("/v1/items/" + encodeURIComponent(detailId) + "/reading"); }
  catch (e) { return; }
  if (seq !== detailSeq || detailId == null || !reading) return;
  const sup = $("supText");
  const editing = sourceEditing || (sup && sup.value.trim());
  detailData = reading;
  renderHeader(reading.item);
  if (!editing) renderDetail();
}

setInterval(() => {
  if (!detailId || !detailData || $("detailView").hidden || document.visibilityState !== "visible") return;
  if (isDetailBusy()) refreshDetailStatus();
}, 4000);

// ---------- 渲染 ----------
function renderDetail() {
  const d = detailData;
  if (!d || !detailId) return;
  const it = d.item;
  if (detailTab === "auto") detailTab = (d.cloud_digest && d.cloud_digest.state === "ready") ? "digest" : "source";
  $("detailTitle").textContent = it.title || (it.original_url || "条目详情");
  $("detailMeta").textContent = [it.source_label, fmtTime(it.created_at)].filter(Boolean).join(" · ");
  renderHeader(it);
  setHTML($("detailBody"),
    '<nav class="readtabs">' +
      '<button class="' + (detailTab === "digest" ? "active" : "") + '" data-tab="digest">整理结果</button>' +
      '<button class="' + (detailTab === "source" ? "active" : "") + '" data-tab="source">原始内容</button>' +
    "</nav>" +
    '<div class="readpane">' + renderPane(d) + "</div>");
  renderMoreMenu(it);
}

function renderHeader(it) {
  const wf = it.workflow;
  setHTML($("stepperHost"), stepperHTML(wf));
  let extra = "";
  const cands = (asrStatus && asrStatus.audio_candidates) || [];
  if (wf && wf.reason_code === "SELECTION_REQUIRED" && cands.length > 1) {
    extra = '<div class="auxcard"><div class="small mb-8">这个页面有多条音频，选择要转写的一条：</div>' +
      cands.map((c) => '<div class="row row-between">' +
        "<span>" + esc(c.title || c.host || "音频") + (c.duration_hint
          ? "（约 " + Math.round(c.duration_hint) + " 秒）" : "") + "</span>" +
        '<button class="small" data-asr-candidate="' + esc(c.candidate_id) + '">转写这条</button>' +
        "</div>").join("") + "</div>";
  }
  setHTML($("stagePanelHost"), stagePanelHTML(wf, { platLabel: it.source_label }) + extra);
  const hasPanel = !!wf;
  $("stagePanelHost").hidden = !hasPanel;
}

function renderPane(d) {
  if (detailTab === "digest") return renderDigestPane(d);
  return renderSourcePane(d);
}

// ---- 整理结果（§7.3）：默认只展示有内容的区块，不显示占位段落 ----
function renderDigestPane(d) {
  const g = d.cloud_digest;
  const rows = [];
  if (!g || g.state === "missing" || g.state === "pending") {
    rows.push('<div class="muted">整理还没有开始；原始内容仍可阅读。</div>');
    return rows.join("");
  }
  if (g.state === "expired") {
    rows.push('<div class="muted">这份整理已过保留期；本地已下载的材料仍可查看。</div>');
  }
  if (g.stale_note) rows.push('<div class="muted">' + esc(g.stale_note) + "</div>");
  if (g.state !== "ready" && g.state !== "expired") {
    rows.push('<div class="muted">' + esc(g.state_detail || "整理还没有完成。") + "</div>");
    return rows.join("");
  }
  if (g.summary) rows.push('<h4>一句话总结</h4><div class="readbody">' + esc(g.summary) + "</div>");
  if (g.key_points.length) {
    rows.push('<h4>核心观点</h4><div class="readbody"><ul>' + g.key_points.map((kp, i) =>
      "<li>" + esc(kp.text) + (kp.conditions ? "（适用条件：" + esc(kp.conditions) + "）" : "") +
      evidenceButtons("kp:" + i, kp.evidence_ids, g) + "</li>").join("") + "</ul></div>");
  }
  if (g.excerpts.length) {
    rows.push('<h4>值得保留的原文</h4><div class="readbody">' + g.excerpts.map((ex, i) =>
      "<blockquote>" + esc(ex.text) + evidenceButtons("ex:" + i, ex.evidence_ids, g) + "</blockquote>").join("") + "</div>");
  }
  if (g.methods.length || g.limitations.length) {
    rows.push('<h4>方法与局限</h4><div class="readbody">');
    if (g.methods.length) {
      rows.push("<ul>" + g.methods.map((m, i) => "<li>" + esc(m.text) + evidenceButtons("m:" + i, m.evidence_ids, g) +
        (m.steps && m.steps.length ? "<ul>" + m.steps.map((s) => "<li>" + esc(s) + "</li>").join("") + "</ul>" : "") +
        (m.conditions ? '<div class="muted">适用条件：' + esc(m.conditions) + "</div>" : "") + "</li>").join("") + "</ul>");
    }
    if (g.limitations.length) {
      rows.push("<ul>" + g.limitations.map((l) => "<li>局限：" + esc(l) + "</li>").join("") + "</ul>");
    }
    rows.push("</div>");
  }
  if (g.insights.length) {
    rows.push('<h4>AI 候选启发</h4><div class="readbody"><ul>' + g.insights.map((i) =>
      "<li>" + esc(i.text) + "（AI 推测，未经原文证明）</li>").join("") + "</ul></div>");
  }
  if (!rows.length) rows.push('<div class="muted">这份整理没有可显示的内容。</div>');
  return rows.join("");
}

const evidenceCursor = {};
function evidenceButtons(key, ids, g) {
  if (!ids || !ids.length) return "";
  const known = ids.filter((sid) => g.segments && Object.prototype.hasOwnProperty.call(g.segments, sid));
  if (!known.length) return "";
  return ' <button class="loclink" data-key="' + esc(key) + '" data-ids="' + esc(known.join(",")) +
    '" title="定位到原文出处">原文</button>';
}

// ---- 原始内容（§7.4）：只显示首屏必需项；空值元数据行不显示 ----
function renderSourcePane(d) {
  const it = d.item;
  const sm = d.source_material;
  const rows = [];
  const metaBits = [];
  if (it.author) metaBits.push("作者：" + esc(it.author));
  if (it.published_at) metaBits.push("发布于 " + esc(it.published_at));
  if (metaBits.length) rows.push('<div class="srcmeta">' + metaBits.join("<br>") + "</div>");
  if (it.original_url) {
    rows.push('<div class="srcmeta"><a href="' + esc(it.original_url) + '" target="_blank" rel="noopener">' +
      esc(it.original_url) + "</a></div>");
  }
  const incomplete = it.coverage && COVERAGE_WARN[it.coverage];
  if (incomplete) {
    rows.push('<div class="muted mb-10">' + esc(COVERAGE_WARN[it.coverage]) +
      (it.missing_materials && it.missing_materials.length
        ? "；缺失：" + esc(it.missing_materials.map(missingLabel).join("、")) : "") + "</div>");
  }
  if (it.user_note) {
    rows.push("<blockquote>" + esc(it.user_note) + "</blockquote>");
  }
  if (!sm) {
    rows.push('<div class="muted">这条内容还没有可阅读的原始材料。</div>');
    return rows.join("");
  }
  const hasEditable = !!(sm.readable_md || sm.normalized_md);
  if (sourceEditing && hasEditable) {
    rows.push('<div class="small muted mb-8">编辑原始内容：一行一段，以 # 开头为标题。保存后会生成新版本（旧版本保留）。</div>');
    rows.push('<textarea id="editSourceText" rows="16" placeholder="一行一段；以 # 开头为标题">' + esc(sourceEditText(sm)) + "</textarea>");
    rows.push('<div class="row mt-12">' +
      '<button class="primary" id="saveSourceBtn">保存修改</button>' +
      '<button id="cancelEditBtn">取消</button></div>');
    return rows.join("");
  }
  const mdFile = (sm.files || []).find((f) => f.relative_path === "readable.md")
    || (sm.files || []).find((f) => f.relative_path === "normalized.md");
  if (mdFile || hasEditable) {
    rows.push('<div class="srcactions">' +
      (mdFile ? '<button class="small" data-act="download-source">下载原文文件</button>' : "") +
      (hasEditable ? '<button class="small" data-act="edit-source">编辑原始内容</button>' : "") +
      "</div>");
  }
  const paras = sm.readable_md ? sm.readable_md.split("\n").filter((l) => l.trim()) : null;
  const lines = paras || (sm.normalized_md ? sm.normalized_md.split("\n").filter((l) => l.trim()) : []);
  if (lines.length) {
    const shown = lines.slice(0, segShown + SEG_PAGE);
    rows.push('<div class="readbody" id="sourceBody">' +
      shown.map(paras ? renderParagraphLine : renderSourceLine).join("") + "</div>");
    if (shown.length < lines.length) {
      rows.push('<div class="row mt-12">' +
        '<button data-act="more-segments">继续展开（还有 ' + (lines.length - shown.length) + (paras ? " 段）" : " 片段）") + "</button>" +
        '<button data-act="source-top">回到顶部</button></div>');
    }
  } else if (sm.truncated) {
    rows.push('<div class="muted">正文超过内联阅读上限，可在「更多操作」里下载原文文件。</div>');
  } else {
    rows.push('<div class="muted">没有取得可读的正文。</div>');
  }
  return rows.join("");
}

function renderSourceLine(line) {
  const m = /^(.*?)\s+\^(s\d{4})\s*$/.exec(line);
  if (!m) return '<div class="seg">' + esc(line) + "</div>";
  return '<div class="seg" id="' + esc(m[2]) + '">' + esc(m[1]) + "</div>";
}
function renderParagraphLine(line) {
  const m = /^(##\s+)?(.*?)\s+\^(p\d{4})\s*$/.exec(line);
  if (!m) return '<p class="para">' + esc(line) + "</p>";
  if (m[1]) return '<h5 class="para parahead" id="' + esc(m[3]) + '">' + esc(m[2]) + "</h5>";
  return '<p class="para" id="' + esc(m[3]) + '">' + esc(m[2]) + "</p>";
}

function sourceEditText(sm) {
  const body = sm.readable_md || sm.normalized_md || "";
  return body.split("\n").map((l) => l.replace(/\s+\^[sp]\d{4}\s*$/, "")).join("\n");
}

// 单篇详情里的「用这篇生成分享页」：带着当前条目直接进分享舞台（docs/20 §3.1）
async function openShareWithThisItem() {
  if (!detailId) return;
  const { openWorkbench } = await import("./shares.js");
  openWorkbench([detailId]);
}

// ---------- 更多操作菜单（§7.5）：按能力动态展示，危险操作在底部 ----------
function renderMoreMenu(it) {
  const wf = it.workflow || {};
  const acts = wf.available_actions || [];
  const labels = availableActionLabels(it.source_label);
  const items = [];
  const seen = new Set();
  for (const code of acts) {
    if (seen.has(code)) continue;
    seen.add(code);
    if (labels[code]) items.push({ code, label: labels[code] });
  }
  if (it.audio_original_retained && it.audio_original_download) items.push({ code: "download-audio", label: "下载上传的录音原件" });
  items.push({ code: "share-page", label: "用这篇生成分享页" });
  items.push({ code: "view-records", label: "查看处理记录" });
  const menu = $("moreMenu");
  menu.innerHTML = items.map((x) =>
    '<button class="menu-item" data-more="' + esc(x.code) + '">' + esc(x.label) + "</button>").join("") +
    '<div class="soft-hr"></div>' +
    '<button class="menu-item danger" data-more="delete">删除服务器材料</button>';
}

// ---------- 处理记录（三级结构：白话摘要 → 解释 → 折叠技术信息） ----------
async function openRecords() {
  if (!detailId) return;
  let d;
  try { d = await api("/v1/items/" + encodeURIComponent(detailId) + "/diagnostics"); }
  catch (e) { showErr(e); return; }
  const summary = d.summary.map((s) =>
    '<div class="record-row"><span class="record-stage">' + esc(s.stage_label) + "</span>" +
    "<span class=\"record-msg\">" + esc(s.status_label) + " · " + esc(s.message) + "</span></div>").join("");
  const t = d.technical_records || {};
  const tech = [];
  const line = (k, v) => { if (v !== undefined && v !== null && v !== "") tech.push("<b>" + esc(k) + "</b><span>" + esc(String(v)) + "</span>"); };
  line("内容版本", t.content_version_label);
  line("整理结果", t.digest_version_label);
  line("处理状态", t.pipeline_state);
  if (t.state_detail) line("说明", t.state_detail);
  if (t.latest_job) {
    line("最近任务", (STAGE_LABELS[t.latest_job.stage] || t.latest_job.stage) + " · " + t.latest_job.state);
    if (t.latest_job.last_error) line("任务错误", t.latest_job.last_error);
  }
  if (t.asr) {
    line("语音转写", t.asr.state + (t.asr.pause_reason ? "（" + t.asr.pause_reason + "）" : ""));
    if (t.asr.chunk_count) line("转写进度", t.asr.done_chunks + "/" + t.asr.chunk_count);
    if (t.asr.last_error) line("转写错误", t.asr.last_error);
  }
  if (t.delivery) {
    line("发布回执", t.delivery.receipt_received ? "已收到" : "尚未收到");
    if (t.delivery.source_download_bundle) line("网页下载原文", "整理结果版本 " + t.delivery.source_download_bundle);
  }
  openModalHTML(
    '<div class="modal-title">处理记录</div>' +
    '<div class="modal-body"><div class="record-summary">' + summary + "</div>" +
    '<details class="disclosure slim mt-14"><summary>技术信息</summary><div class="disclosure-body">' +
    '<p class="small">' + esc(d.explanation || "") + "</p>" +
    '<div class="techgrid">' + tech.join("") + "</div>" +
    '<div class="row mt-10"><button class="small" id="copyTech">复制技术信息</button></div>' +
    "</div></details></div>" +
    '<div class="modal-foot"><button id="recClose">关闭</button></div>');
  $("recClose").onclick = () => closeModal(null);
  $("copyTech").onclick = async () => {
    try {
      await navigator.clipboard.writeText(JSON.stringify(t, null, 2));
      toast("技术信息已复制", { type: "ok" });
    } catch (e) { toast("复制没有完成", { type: "error" }); }
  };
}

// ---------- 操作 ----------
export async function doReprocess() {
  if (!detailId) return;
  try {
    await api("/v1/items/" + encodeURIComponent(detailId) + "/reprocess", {
      method: "POST", body: { reason: "从 Web 收件箱请求重新处理" } });
    toast("已开始重新处理", { type: "ok" });
    refreshDetail();
  } catch (e) { showErr(e); }
}

async function doOptimizeText() {
  if (!detailId) return;
  try {
    await api("/v1/items/" + encodeURIComponent(detailId) + "/optimize-text", { method: "POST" });
    toast("已开始优化文本", { type: "ok" });
    refreshDetail();
  } catch (e) { showErr(e); }
}

async function doRefetch() {
  if (!detailId) return;
  try {
    await api("/v1/items/" + encodeURIComponent(detailId) + "/refetch", { method: "POST" });
    toast("已开始重新提取来源", { type: "ok" });
    refreshDetail();
  } catch (e) { showErr(e); }
}

async function doAsrTrigger(candidateId) {
  if (!detailId) return;
  try {
    asrStatus = await api("/v1/items/" + encodeURIComponent(detailId) + "/asr",
      { method: "POST", body: candidateId ? { audio_candidate_id: candidateId } : {} });
    toast("已加入转写队列（服务器空闲时执行）", { type: "ok" });
    await refreshDetailStatus();
    renderDetail();
  } catch (e) { showErr(e); }
}

async function doAsrCancel() {
  if (!detailId) return;
  try {
    await api("/v1/items/" + encodeURIComponent(detailId) + "/asr/cancel", { method: "POST", body: {} });
    toast("已请求取消转写", { type: "ok" });
    await refreshDetailStatus();
  } catch (e) { showErr(e); }
}

async function doDelete() {
  if (!detailId) return;
  const ok = await confirmModal({
    title: "删除服务器材料",
    body: "将删除这条内容在服务器上的全部材料与整理结果，<b>删除后不可恢复</b>。已写入你 Obsidian 知识库的笔记不受影响。",
    confirmLabel: "删除", danger: true,
  });
  if (!ok) return;
  try {
    await api("/v1/items/" + encodeURIComponent(detailId), { method: "DELETE" });
    toast("已删除服务器材料", { type: "ok" });
    exitToList();
    refreshList();
  } catch (e) { showErr(e); }
}

async function downloadSourceMd() {
  if (!detailData) return;
  const it = detailData.item, sm = detailData.source_material;
  if (!sm) return;
  const mdFile = (sm.files || []).find((f) => f.relative_path === "readable.md")
    || (sm.files || []).find((f) => f.relative_path === "normalized.md");
  let raw = "";
  if (mdFile) {
    try {
      const r = await fetch(sm.download_base + "/" + mdFile.file_id);
      if (r.ok) raw = await r.text();
    } catch (e) { /* 拉取失败落到内联副本（可能截断） */ }
  }
  if (!raw) raw = sm.readable_md || sm.normalized_md || "";
  if (!raw) { toast("没有可下载的原文文件", { type: "error" }); return; }
  const cleaned = raw.split("\n")
    .map((l) => l.replace(/\s+\^[sp]\d{4,}\s*$/, ""))
    .join("\n").replace(/\n{3,}/g, "\n\n").trim() + "\n";
  const name = sanitizeFilename((it.title || "").trim() || "原文") + ".md";
  const blob = new Blob([cleaned], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast("已下载原文文件", { type: "ok" });
  // 下载走的是本地 Blob，服务器只能靠这次登记把条目判为已下载（docs/17 §5.2）
  try {
    await api("/v1/items/" + encodeURIComponent(it.item_id) + "/source-download", { method: "POST" });
    await refreshDetailStatus();
  } catch (e) { showErr(e); }
  refreshList();
}

// ---------- 补充内容（唯一主按钮 supplement 的弹窗形态） ----------
function openSupplementModal() {
  supFilesState = [];
  openModalHTML(
    '<div class="modal-title">补充内容</div>' +
    '<div class="modal-body">' +
    '<label for="supText">补充文字</label><textarea id="supText" rows="4" placeholder="粘贴缺失的正文、字幕内容…"></textarea>' +
    '<div class="row mt-10"><button type="button" class="ghost small" id="supPick">添加文件</button>' +
    '<input id="supFiles" type="file" multiple hidden><span class="small" id="supProgress"></span></div>' +
    '<div class="chiprow" id="supFileChips" hidden></div>' +
    "</div>" +
    '<div class="modal-foot"><button id="mCancel">取消</button>' +
    '<button id="supOk" class="primary">提交补充</button></div>');
  $("supPick").onclick = () => $("supFiles").click();
  $("supFiles").onchange = (e) => {
    for (const f of e.target.files) supFilesState.push(f);
    e.target.value = "";
    renderSupChips();
  };
  $("mCancel").onclick = () => closeModal(null);
  $("supOk").onclick = doSupplement;
}
function renderSupChips() {
  const host = $("supFileChips");
  if (!host) return;
  host.hidden = supFilesState.length === 0;
  host.innerHTML = supFilesState.map((f, i) =>
    '<span class="chip"><span class="lbl">' + esc(f.name) + '</span>' +
    '<button class="xbtn" data-sup-i="' + i + '" aria-label="移除该附件">✕</button></span>').join("");
}

async function doSupplement() {
  if (!detailId) return;
  const text = $("supText") ? $("supText").value.trim() : "";
  const files = supFilesState.slice();
  if (!text && !files.length) { toast("先填写文字或选择文件", { type: "error" }); return; }
  const btn = $("supOk");
  if (btn) btn.disabled = true;
  try {
    const uploadIds = files.length ? await uploadFiles(files, (s) => ($("supProgress").textContent = s)) : [];
    const it = await api("/v1/items/" + encodeURIComponent(detailId));
    await api("/v1/items/" + encodeURIComponent(detailId) + "/supplements", {
      method: "POST",
      body: { expected_source_revision: it.source_revision, text: text || null, upload_ids: uploadIds },
    });
    closeModal(null);
    toast("已补充，系统会继续处理", { type: "ok" });
    refreshDetail();
    refreshList();
  } catch (e) {
    if (btn) btn.disabled = false;
    showErr(e);
  }
}

// ---------- 原文编辑 ----------
function startEditSource() {
  sourceEditing = true;
  segShown = 0;
  detailTab = "source";
  renderDetail();
  const t = $("editSourceText");
  if (t) { t.focus(); t.setSelectionRange(t.value.length, t.value.length); }
}
function cancelEditSource() {
  sourceEditing = false;
  renderDetail();
}
async function saveSourceEdit() {
  if (!detailId || !detailData) return;
  const box = $("editSourceText");
  const text = box ? box.value : "";
  if (!text.split("\n").map((l) => l.trim()).filter(Boolean).length) {
    toast("编辑后的正文为空", { type: "error" }); return;
  }
  const btn = $("saveSourceBtn");
  if (btn) btn.disabled = true;
  try {
    await api("/v1/items/" + encodeURIComponent(detailId) + "/source-text", {
      method: "POST",
      body: { expected_source_revision: detailData.item.source_revision, text },
    });
    sourceEditing = false;
    toast("已保存修改", { type: "ok" });
    refreshDetail();
    refreshList();
  } catch (e) {
    if (btn) btn.disabled = false;
    showErr(e);
  }
}

// ---------- 证据定位 ----------
function jumpEvidence(btn) {
  const ids = (btn.getAttribute("data-ids") || "").split(",").filter(Boolean);
  const key = btn.getAttribute("data-key") || "";
  if (!ids.length) return;
  const step = (evidenceCursor[key] || 0) % ids.length;
  evidenceCursor[key] = step + 1;
  jumpToSegment(ids[step]);
  if (ids.length > 1) btn.title = "定位到原文出处（第 " + (step + 1) + "/" + ids.length + " 处，再点看下一处）";
}
function jumpToSegment(sid) {
  const g = detailData && detailData.cloud_digest;
  const sm = detailData && detailData.source_material;
  if (!g || !sm) return;
  const pid = sm.readable_md && sm.segment_paragraph ? sm.segment_paragraph[sid] : null;
  const body = pid ? sm.readable_md : sm.normalized_md;
  const target = pid || sid;
  if (!body) return;
  const lines = body.split("\n").filter((l) => l.trim());
  const idx = lines.findIndex((l) => l.indexOf("^" + target) !== -1);
  if (idx === -1) {
    // 静默不响应会被当成「按钮坏了」；来源版本换过之后旧段落定位不到是真实情况
    toast("没有定位到这段原文：可能已随新的来源版本调整，可在「原始内容」里搜索关键词", { type: "info" });
    return;
  }
  // 目标段在当前窗口之上时同样要翻到它所在的那一页：否则下面的
  // getElementById 取不到节点，点「原文」毫无反应（审查 C-11、U-08）
  if (idx < segShown || idx >= segShown + SEG_PAGE) segShown = Math.floor(idx / SEG_PAGE) * SEG_PAGE;
  detailTab = "source";
  renderDetail();
  const el = document.getElementById(target);
  if (el) { el.scrollIntoView({ block: "center" }); el.classList.add("highlight"); }
}

// ---------- 主按钮分发（唯一主按钮，docs/17 §6.3） ----------
async function stageAction(code) {
  switch (code) {
    case "supplement": openSupplementModal(); break;
    case "choose_model": goSettingsCard("secModel"); break;
    case "connect_obsidian": goSettingsCard("secObsidian"); break;
    case "connect_platform": goPlatformSettings(); break;
    case "update_session": goPlatformSettings(); break;
    case "retry": doReprocess(); break;
    case "start_organize": doReprocess(); break;
    case "start_optimize_text": doOptimizeText(); break;
    default: break;
  }
}
function goSettingsCard(cardId) {
  import("./app.js").then((m) => m.openSettings(cardId));
}
// 登录态动作落到设置页「内容平台」卡：该平台已有独立配置块时直接定位到它
function goPlatformSettings() {
  const plat = (detailData && detailData.item && detailData.item.platform) || "";
  goSettingsCard($("plat-" + plat) ? "plat-" + plat : "secBili");
}

// ---------- 事件 ----------
export function initDetail() {
  $("backBtn").addEventListener("click", exitDetail);
  $("moreBtn").addEventListener("click", (e) => {
    e.stopPropagation();
    if (detailData) renderMoreMenu(detailData.item);
    $("moreMenu").hidden = !$("moreMenu").hidden;
  });
  $("moreMenu").addEventListener("click", (e) => {
    const b = e.target.closest("[data-more]");
    if (!b) return;
    $("moreMenu").hidden = true;
    const code = b.dataset.more;
    if (code === "view-records") openRecords();
    else if (code === "share-page") openShareWithThisItem();
    else if (code === "delete") doDelete();
    else if (code === "download-audio") window.location.href = detailData.item.audio_original_download;
    else if (code === "refetch") doRefetch();
    else if (code === "cancel_process") doAsrCancel();
    else if (code === "retry_process") doAsrTrigger();
    else if (code === "supplement") openSupplementModal();
    else if (code === "start_organize") doReprocess();
    else if (code === "start_optimize_text") doOptimizeText();
    else if (code === "choose_model") goSettingsCard("secModel");
    else if (code === "connect_obsidian") goSettingsCard("secObsidian");
    else if (code === "connect_platform" || code === "update_session") goPlatformSettings();
  });
  $("stagePanelHost").addEventListener("click", (e) => {
    const b = e.target.closest("[data-stage-action]");
    if (b) stageAction(b.dataset.stageAction);
    const cand = e.target.closest("[data-asr-candidate]");
    if (cand) doAsrTrigger(cand.dataset.asrCandidate);
  });
  $("detailBody").addEventListener("click", (e) => {
    const tab = e.target.closest("[data-tab]");
    if (tab) { detailTab = tab.dataset.tab; if (detailTab === "source") segShown = 0; renderDetail(); return; }
    const loc = e.target.closest(".loclink");
    if (loc) { jumpEvidence(loc); return; }
    if (e.target.id === "saveSourceBtn") { saveSourceEdit(); return; }
    if (e.target.id === "cancelEditBtn") { cancelEditSource(); return; }
    const act = e.target.closest("[data-act]");
    if (act) {
      const code = act.dataset.act;
      if (code === "download-source") downloadSourceMd();
      else if (code === "edit-source") startEditSource();
      else if (code === "more-segments") { segShown += SEG_PAGE; renderDetail(); }
      else if (code === "source-top") window.scrollTo({ top: 0, behavior: "smooth" });
    }
  });
  document.addEventListener("click", (e) => {
    const x = e.target.closest("[data-sup-i]");
    if (x) { supFilesState.splice(Number(x.dataset.supI), 1); renderSupChips(); }
  });
}

window.addEventListener("popstate", () => {
  const id = new URLSearchParams(location.search).get("item");
  if (id && id !== detailId) { openDetail(id, { push: false }); }
  else if (!id && detailId) { closeDetail(); }
});
