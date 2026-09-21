// shares.js — 选材料 → 澄清需求 → 生成 → 预览/修改 → 分享（docs/20 §3）
//
// 界面只呈现服务器给的状态文案与需求摘要；不显示进度百分比、不展示模型提示词。
// 预览走「主站鉴权取数据 → 可信容器设置 srcdoc」，iframe 只开 allow-scripts。

import { $, api, esc, showErr, toast } from "./api.js";

const SELECTED_KEY = "kb.share.selected.v1";
let selected = new Set(loadSelected());
let selectMode = false;
let activeShareId = null;
let activeRunId = null;
let pollTimer = 0;
let workCache = null;      // GET /v1/shares/{id} 的结果
let convCache = null;      // 对话与待答问题
let lastStatusKey = "";

function loadSelected() {
  try { return JSON.parse(localStorage.getItem(SELECTED_KEY) || "[]"); } catch (e) { return []; }
}

function saveSelected() {
  try { localStorage.setItem(SELECTED_KEY, JSON.stringify([...selected])); } catch (e) { /* 忽略 */ }
}

export function selectedIds() { return [...selected]; }

// ---------- 列表选择模式 ----------

function setSelectMode(on) {
  selectMode = on;
  const toggle = $("selectToggle");
  if (toggle) {
    toggle.setAttribute("aria-pressed", on ? "true" : "false");
    toggle.classList.toggle("on", on);
  }
  document.body.classList.toggle("share-selecting", on);
  renderSelectionBar();
}

export function isSelectMode() { return selectMode; }

function toggleItem(itemId) {
  if (selected.has(itemId)) selected.delete(itemId); else selected.add(itemId);
  saveSelected();
  syncRowMarks();
  renderSelectionBar();
}

function syncRowMarks() {
  document.querySelectorAll("ul.items li.item").forEach((li) => {
    li.classList.toggle("picked", selected.has(li.dataset.id));
  });
}

function renderSelectionBar() {
  const bar = $("shareSelBar");
  if (!bar) return;
  const count = selected.size;
  bar.hidden = !(selectMode && count > 0);
  const num = $("shareSelCount");
  if (num) num.textContent = String(count);
}

function installSelection() {
  document.querySelectorAll("ul.items").forEach((ul) => {
    // 捕获阶段先于 item-list 的行点击（打开详情），选择模式下把点击改成勾选
    ul.addEventListener("click", (e) => {
      if (!selectMode) return;
      const li = e.target.closest("li.item");
      if (!li) return;
      e.preventDefault();
      // 同一元素上的其他监听（打开详情）也要拦住，否则点第二行时已经跳到详情
      e.stopImmediatePropagation();
      toggleItem(li.dataset.id);
    }, true);
  });
  const toggle = $("selectToggle");
  if (toggle) toggle.addEventListener("click", () => setSelectMode(!selectMode));
  const clear = $("shareSelClear");
  if (clear) clear.addEventListener("click", () => {
    selected.clear(); saveSelected(); syncRowMarks(); renderSelectionBar();
  });
  const go = $("shareSelCreate");
  if (go) go.addEventListener("click", () => openWorkbench());
  const exit = $("shareSelExit");
  if (exit) exit.addEventListener("click", () => setSelectMode(false));
}

// ---------- 视图切换 ----------

function showSharesView() {
  $("homeView").hidden = true;
  $("settingsView").hidden = true;
  $("sharesView").hidden = false;
  document.body.classList.add("shares-open");
}

function hideSharesView() {
  $("sharesView").hidden = true;
  document.body.classList.remove("shares-open");
  stopPolling();
}

export function isSharesOpen() { return $("sharesView") && !$("sharesView").hidden; }

export async function openSharesView(shareId) {
  showSharesView();
  await renderWorksList();
  if (shareId) await openWork(shareId);
}

// 从列表/详情进入：带上选中材料，打开创作面板
export async function openWorkbench(itemIds) {
  const ids = itemIds && itemIds.length ? itemIds : selectedIds();
  showSharesView();
  await renderWorksList();
  renderComposer(ids);
}

// ---------- 创作面板：材料 + 一句要求 ----------

function renderComposer(itemIds) {
  const host = $("shareComposer");
  const list = $("shareWorkArea");
  if (!itemIds.length) {
    host.innerHTML = '<p class="share-empty">先在收件箱里点「选择」勾几篇材料，或从条目详情里选「生成分享页」。</p>';
    host.hidden = false;
    list.hidden = true;
    return;
  }
  host.hidden = false;
  list.hidden = true;
  host.innerHTML =
    '<h3 class="share-h">用这 ' + itemIds.length + ' 篇材料做作品</h3>' +
    '<ul class="share-mats" id="shareMats"><li class="muted">正在读取材料…</li></ul>' +
    '<label class="share-label" for="shareAsk">想把这些材料做成什么？可以说内容重点、读者、呈现方式或交互要求。</label>' +
    '<textarea id="shareAsk" class="share-ask" rows="3" placeholder="例：整理出它们的差异和相互启发，适合初学者阅读；有必要再配图。"></textarea>' +
    '<div class="share-actions">' +
    '  <button class="btn primary" id="shareStart">开始创作</button>' +
    '  <button class="btn ghost" id="shareAskCancel">先不做</button>' +
    '  <span class="muted small" id="shareStartHint">不填也可以，AI 会先读材料再问你几个问题。</span>' +
    '</div>';
  $("shareAskCancel").addEventListener("click", () => { $("shareComposer").hidden = true; renderWorksList(); });
  $("shareStart").addEventListener("click", () => startCreation(itemIds));
  loadMaterials(itemIds);
}

async function loadMaterials(itemIds) {
  const host = $("shareMats");
  if (!host) return;
  const rows = [];
  const unusable = [];
  for (const id of itemIds.slice(0, 30)) {
    let d = null;
    try { d = await api("/v1/items/" + encodeURIComponent(id)); } catch (e) { d = null; }
    if (!d) { unusable.push({ id, title: "有一篇材料已经打不开了" }); continue; }
    const it = d || {};
    // 界面只做粗筛；到底能不能读由服务端判定，422 会带回具体条目
    const readable = ["ready", "extracted"].includes(it.pipeline_state);
    if (!readable) { unusable.push({ id, title: it.title || "未命名材料" }); continue; }
    rows.push('<li><span class="mtitle">' + esc(it.title || "未命名材料") + "</span>" +
      '<span class="mmeta">' + esc(it.source_label || "") + "</span></li>");
  }
  for (const bad of unusable) {
    rows.push('<li class="warn"><span class="mtitle">' + esc(bad.title) + "</span>" +
      '<span class="mmeta">还没有可读正文</span>' +
      '<button class="btn ghost small" data-drop="' + esc(bad.id) + '">移除</button></li>');
  }
  host.innerHTML = rows.join("");
  host.querySelectorAll("[data-drop]").forEach((btn) => btn.addEventListener("click", () => {
    selected.delete(btn.dataset.drop); saveSelected(); syncRowMarks(); renderSelectionBar();
    renderComposer(selectedIds());
  }));
  const start = $("shareStart");
  if (start) {
    start.disabled = rows.length === 0 || unusable.length > 0;
    const hint = start.nextElementSibling;
    if (unusable.length && hint) hint.textContent = "先移除没有可读正文的材料，或去补充材料。";
  }
}

async function startCreation(itemIds) {
  const instructions = ($("shareAsk") ? $("shareAsk").value : "").trim();
  const btn = $("shareStart");
  if (btn) btn.disabled = true;
  try {
    const created = await api("/v1/shares", {
      method: "POST",
      body: { item_ids: itemIds, instructions },
      idempotencyKey: newKey("share"),
    });
    selected.clear(); saveSelected(); syncRowMarks(); renderSelectionBar();
    setSelectMode(false);
    await openWork(created.share_id, created.run_id);
    toast("已开始，AI 正在读你选的材料", { type: "ok" });
  } catch (e) {
    if (btn) btn.disabled = false;
    const bad = e && e.details && e.details.unreadable_items;
    if (Array.isArray(bad) && bad.length) {
      // 服务端判定哪几篇不可读：就地给出「补充材料／移除」，不悄悄排除
      for (const item of bad) {
        const li = document.createElement("li");
        li.className = "warn";
        li.innerHTML = '<span class="mtitle">' + esc(item.title || "未命名材料") +
          "</span><span class=\"mmeta\">没有可读正文</span> " +
          '<button class="btn ghost small" data-drop="' + esc(item.item_id) + '">移除</button> ' +
          '<button class="btn ghost small" data-fix="' + esc(item.item_id) + '">补充材料</button>';
        $("shareMats").appendChild(li);
      }
      $("shareMats").querySelectorAll("[data-drop]").forEach((b) => b.addEventListener("click", () => {
        selected.delete(b.dataset.drop); saveSelected(); syncRowMarks(); renderSelectionBar();
        renderComposer(selectedIds());
      }));
      $("shareMats").querySelectorAll("[data-fix]").forEach((b) => b.addEventListener("click", () => {
        hideSharesView();
        openDetailFromShare(b.dataset.fix);
      }));
      const hint = $("shareStartHint");
      if (hint) hint.textContent = "上面这几篇还没有可读正文：移除它们，或先去补充材料。";
      toast("有材料还没有可读正文", { type: "warn" });
      return;
    }
    showErr(e);
  }
}

async function openDetailFromShare(itemId) {
  const { openDetail } = await import("./item-detail.js");
  openDetail(itemId);
}

function newKey(prefix) {
  return prefix + "-" + Math.random().toString(36).slice(2) + Date.now().toString(36);
}

// ---------- 作品列表 ----------

async function renderWorksList() {
  const host = $("shareWorks");
  if (!host) return;
  host.innerHTML = '<li class="muted">正在加载…</li>';
  let data;
  try { data = await api("/v1/shares?limit=30"); } catch (e) { showErr(e); host.innerHTML = ""; return; }
  if (!data.items.length) {
    host.innerHTML = '<li class="muted">还没有作品。去收件箱选几篇材料，点「生成分享页」。</li>';
    return;
  }
  host.innerHTML = data.items.map((w) =>
    '<li class="work" data-share="' + esc(w.share_id) + '" role="button" tabindex="0">' +
    '<span class="wtitle">' + esc(w.title) + "</span>" +
    '<span class="wmeta">' + esc(w.status_text) +
    (w.latest_revision ? " · v" + w.latest_revision : "") +
    (w.share_status === "published" ? " · 已分享" : "") + "</span></li>").join("");
  host.querySelectorAll(".work").forEach((li) => {
    const open = () => openWork(li.dataset.share);
    li.addEventListener("click", open);
    li.addEventListener("keydown", (e) => { if (e.key === "Enter") open(); });
  });
}

// ---------- 单个作品：对话 + 摘要 + 状态 + 预览 ----------

async function openWork(shareId, runId) {
  activeShareId = shareId;
  const params = new URLSearchParams(location.search);
  params.set("view", "shares");
  params.set("share", shareId);
  if (runId) params.set("run", runId);
  try { history.replaceState({}, "", "/inbox?" + params.toString()); } catch (e) { /* 忽略 */ }
  await refreshWork();
  startPolling();
}

async function refreshWork() {
  if (!activeShareId) return;
  try {
    workCache = await api("/v1/shares/" + encodeURIComponent(activeShareId));
  } catch (e) { showErr(e); stopPolling(); return; }
  activeRunId = workCache.run ? workCache.run.run_id : null;
  if (activeRunId) {
    try {
      convCache = await api(`/v1/shares/${encodeURIComponent(activeShareId)}/runs/${encodeURIComponent(activeRunId)}/conversation?limit=50`);
    } catch (e) { convCache = null; }
  } else {
    convCache = null;
  }
  renderWork();
}

function statusBlock() {
  const run = workCache && workCache.run;
  if (!run) return "";
  const key = run.state + "|" + run.stage + "|" + (run.reason_code || "");
  const detail = run.reason_text ? '<p class="share-reason">' + esc(run.reason_text) + "</p>" : "";
  const stop = run.state === "running" || run.state === "queued" || run.state === "retry_wait"
    ? '<button class="btn ghost small" id="shareStop">停止生成</button>' : "";
  const retry = ["failed", "unknown_outcome", "cancelled"].includes(run.state)
    ? '<button class="btn small" id="shareRetry">重新尝试</button>' : "";
  if (key === lastStatusKey) {
    // 状态没变就不重画，避免轮询把正在输入的光标顶掉
    return '<div class="share-status"><span class="pill">' + esc(run.status_text) + "</span>" + stop + retry + detail + "</div>";
  }
  lastStatusKey = key;
  return '<div class="share-status"><span class="pill">' + esc(run.status_text) + "</span>" + stop + retry + detail + "</div>";
}

function briefBlock() {
  const brief = workCache && workCache.brief;
  if (!brief || !brief.fields.length) return "";
  const rows = brief.fields.map((f) =>
    "<div><dt>" + esc(f.label) + "</dt><dd>" + f.value.map(esc).join("；") +
    (f.by === "ai" ? ' <span class="tag-ai">AI 暂定</span>' : "") + "</dd></div>").join("");
  return '<details class="share-brief" open><summary>目前的需求（版本 ' + esc(brief.version) + "）</summary>" +
    "<dl>" + rows + "</dl></details>";
}

function chatBlock() {
  const run = workCache && workCache.run;
  if (!run) return "";
  const messages = (convCache && convCache.messages) || [];
  const bubbles = messages.map((m) => {
    if (m.role === "user") {
      return '<div class="msg me">' + esc(m.text || "").split("\n").map(esc).join("<br>") + "</div>";
    }
    const understanding = m.understanding ? '<p class="munder">' + esc(m.understanding) + "</p>" : "";
    const qs = (m.questions || []).map((q) => questionHTML(q, m.round_id)).join("");
    return '<div class="msg ai">' + understanding + qs + "</div>";
  }).join("");
  const pending = workCache.round ? workCache.round : null;
  const answering = pending && run.state === "waiting_user";
  const confirming = pending && run.state === "awaiting_confirmation";
  let composer = "";
  if (answering || confirming) {
    composer =
      '<div class="share-answer" id="shareAnswer">' +
      (confirming
        ? '<p class="muted">' + esc(pending.understanding || "方向已经清楚了，确认后就开工。") + "</p>" +
          '<div class="share-actions">' +
          '  <button class="btn primary" id="shareConfirm">确认并生成</button>' +
          '  <button class="btn ghost" id="shareAdjust">再调整一下</button>' +
          '  <button class="btn ghost" id="shareDelegate">按你的建议生成</button>' +
          "</div>"
        : '<textarea id="shareFreeText" rows="2" class="share-ask" placeholder="可以逐题回答，也可以一次说整组；也可以直接说「你决定，直接做」。"></textarea>' +
          '<div class="share-actions"><button class="btn primary" id="shareSendAnswer">发送回答</button></div>') +
      "</div>";
  }
  return '<div class="share-chat">' + bubbles + (composer || "") + "</div>";
}

function questionHTML(q, roundId) {
  const options = (q.options || []).map((o) =>
    '<label class="opt"><input type="radio" name="' + esc(roundId + ":" + q.id) + '" value="' +
    esc(o.id) + '"><span>' + esc(o.label) + "</span></label>").join("");
  const required = q.required_for_generation ? '<span class="tag-block">需要先确认</span>' : "";
  return '<div class="q" data-q="' + esc(q.id) + '" data-round="' + esc(roundId) + '">' +
    "<p>" + esc(q.text) + required + "</p>" +
    (q.reason ? '<p class="qreason">' + esc(q.reason) + "</p>" : "") +
    (options ? '<div class="opts">' + options + "</div>" : "") + "</div>";
}

function previewBlock() {
  const revisions = (workCache && workCache.revisions) || [];
  if (!revisions.length) {
    return '<div class="share-preview"><p class="muted">还没有可预览的版本。回答几个问题或确认后就开始制作。</p></div>';
  }
  const latest = revisions[revisions.length - 1];
  const actions = workCache.actions || {};
  return '<div class="share-preview">' +
    '<div class="pv-bar">' +
    '  <span class="muted small">预览 v' + esc(latest.revision) + " · 默认只有你自己能看到</span>" +
    '  <span class="spacer"></span>' +
    '  <button class="btn small" id="shareReloadPv">重新载入</button>' +
    '  <a class="btn small" href="/v1/shares/' + esc(activeShareId) + "/revisions/" + latest.revision + '/download" download>下载 HTML</a>' +
    (actions.can_publish ? '<button class="btn primary small" id="sharePublish">分享</button>' : "") +
    (actions.can_modify ? '<button class="btn ghost small" id="shareModify">继续提要求</button>' : "") +
    "</div>" +
    '  <iframe id="shareFrame" class="share-frame" sandbox="allow-scripts" title="作品预览"></iframe>' +
    (workCache.share.status === "published"
      ? '<p class="share-link" id="shareLinkRow">已分享：<a id="shareLink" href="#" target="_blank" rel="noopener noreferrer">打开链接</a> ' +
        '<button class="btn ghost small" id="shareRevoke">撤销</button></p>'
      : "") +
    "</div>";
}

function modifyBlock() {
  return '<div class="share-modify" id="shareModifyBox" hidden>' +
    '<label class="share-label" for="shareModifyText">想改哪里？例：缩短一点、突出差异、把这一部分画成结构图。</label>' +
    '<textarea id="shareModifyText" rows="2" class="share-ask"></textarea>' +
    '<div class="share-actions"><button class="btn primary" id="shareModifyGo">开始修改</button>' +
    '<button class="btn ghost" id="shareModifyCancel">取消</button></div></div>';
}

function renderWork() {
  const host = $("shareWorkArea");
  if (!host || !workCache) return;
  $("shareComposer").hidden = true;
  const actions = workCache.actions || {};
  host.hidden = false;
  host.innerHTML =
    '<header class="share-head"><h3 class="share-h">' + esc(workCache.title) + "</h3>" +
    '<button class="btn ghost small" id="shareBack">← 返回作品列表</button>' +
    '<button class="btn ghost small danger" id="shareDelete">删除作品</button></header>' +
    statusBlock() + briefBlock() + chatBlock() + modifyBlock() + previewBlock();
  wireWorkArea(actions);
  loadPreview();
}

function wireWorkArea(actions) {
  const byId = (id) => $(id) || document.getElementById(id);
  const on = (id, fn) => { const el = byId(id); if (el) el.onclick = fn; };
  on("shareBack", () => { activeShareId = null; stopPolling(); hideSharesView(); goHome(); });
  on("shareDelete", deleteWork);
  on("shareStop", stopRun);
  on("shareRetry", retryRun);
  on("shareSendAnswer", sendAnswer);
  on("shareConfirm", () => startGeneration("confirm"));
  on("shareDelegate", () => startGeneration("delegate_preferences"));
  on("shareAdjust", () => { const box = byId("shareFreeText"); if (box) box.focus(); else openModify(); });
  on("shareModify", openModify);
  on("shareModifyCancel", () => { const b = byId("shareModifyBox"); if (b) b.hidden = true; });
  on("shareModifyGo", submitModify);
  on("sharePublish", publishWork);
  on("shareRevoke", revokeWork);
  on("shareReloadPv", () => loadPreview(true));
}

function goHome() {
  try { history.replaceState({}, "", "/inbox"); } catch (e) { /* 忽略 */ }
  hideSharesView();
  $("homeView").hidden = false;
  if (window.__kbRefreshItems) window.__kbRefreshItems();
}

// ---------- 预览 / 下载 / 发布 ----------

// 预览：只在版本真的变了时重设 srcdoc，轮询不得让画面闪一下（走查零容忍抖动）
let loadedPreviewKey = "";

async function loadPreview(force) {
  const frame = document.getElementById("shareFrame");
  if (!frame || !activeShareId) return;
  const revisions = (workCache && workCache.revisions) || [];
  if (!revisions.length) return;
  const latest = revisions[revisions.length - 1];
  const key = activeShareId + ":" + latest.revision + ":" + (force ? Date.now() : "");
  if (!force && key === loadedPreviewKey) return;
  try {
    const data = await api(`/v1/shares/${encodeURIComponent(activeShareId)}/revisions/${latest.revision}/preview-content`);
    loadedPreviewKey = activeShareId + ":" + latest.revision + ":" + data.html_sha256;
    // 可信容器：用属性赋值建立 srcdoc，不拼接未转义文本
    frame.srcdoc = data.document;
  } catch (e) {
    frame.removeAttribute("srcdoc");
    showErr(e);
  }
}

async function publishWork() {
  const revisions = (workCache && workCache.revisions) || [];
  if (!revisions.length) return;
  try {
    const out = await api(`/v1/shares/${encodeURIComponent(activeShareId)}/publish`, {
      method: "POST",
      body: { revision_id: revisions[revisions.length - 1].revision_id,
        expected_work_version: workCache.version },
      idempotencyKey: newKey("pub"),
    });
    await refreshWork();
    const link = document.getElementById("shareLink");
    if (link) { link.href = out.url; link.textContent = out.url; }
    copy(out.url);
    toast("分享链接已复制；只有拿到链接的人能看", { type: "ok" });
  } catch (e) { showErr(e); }
}

async function revokeWork() {
  try {
    await api(`/v1/shares/${encodeURIComponent(activeShareId)}/revoke`, { method: "POST", body: {} });
    toast("已撤销，旧链接不再可访问", { type: "ok" });
    await refreshWork();
  } catch (e) { showErr(e); }
}

async function deleteWork() {
  if (!activeShareId) return;
  if (!window.confirm("删除这件作品？已生成的分享链接会立即失效。")) return;
  try {
    await api("/v1/shares/" + encodeURIComponent(activeShareId), { method: "DELETE" });
    activeShareId = null;
    goHome();
    toast("作品已删除", { type: "ok" });
  } catch (e) { showErr(e); }
}

function copy(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).catch(() => { /* 忽略 */ });
  }
}

// ---------- 回答 / 开始 / 修改 / 停止 ----------

function collectAnswers() {
  const host = document.getElementById("shareAnswer");
  if (!host) return [];
  const out = [];
  host.querySelectorAll(".q").forEach((q) => {
    const picked = q.querySelector("input:checked");
    const free = (q.querySelector("input[type=text]") || {}).value || "";
    if (!picked && !free.trim()) return;   // 未回答项保留原状态，不采用默认值
    out.push({ question_id: q.dataset.q, option_ids: picked ? [picked.value] : [],
      text: free.trim() });
  });
  return out;
}

async function sendAnswer() {
  const round = workCache && workCache.round;
  if (!round) return;
  const free = (document.getElementById("shareFreeText") || {}).value || "";
  try {
    await api(`/v1/shares/${encodeURIComponent(activeShareId)}/runs/${encodeURIComponent(activeRunId)}/messages`, {
      method: "POST",
      body: {
        expected_conversation_version: convCache ? convCache.conversation_version : 0,
        round_id: round.round_id, answers: collectAnswers(), message: free.trim(),
      },
      idempotencyKey: newKey("ans"),
    });
    await refreshWork();
    startPolling();
  } catch (e) { showErr(e); }
}

async function startGeneration(mode) {
  try {
    await api(`/v1/shares/${encodeURIComponent(activeShareId)}/runs/${encodeURIComponent(activeRunId)}/start`, {
      method: "POST",
      body: {
        expected_conversation_version: convCache ? convCache.conversation_version : null,
        expected_brief_version: workCache.brief ? workCache.brief.version : null,
        mode,
      },
      idempotencyKey: newKey("start"),
    });
    await refreshWork();
    startPolling();
  } catch (e) { showErr(e); }
}

function openModify() {
  const box = document.getElementById("shareModifyBox");
  if (!box) return;
  box.hidden = false;
  const ta = document.getElementById("shareModifyText");
  if (ta) ta.focus();
}

async function submitModify() {
  const text = (document.getElementById("shareModifyText") || {}).value || "";
  const revisions = (workCache && workCache.revisions) || [];
  if (!revisions.length) return;
  try {
    const out = await api(`/v1/shares/${encodeURIComponent(activeShareId)}/runs`, {
      method: "POST",
      body: { base_revision_id: revisions[revisions.length - 1].revision_id,
        instructions: text.trim(), expected_work_version: workCache.version },
      idempotencyKey: newKey("mod"),
    });
    toast("已排入修改，好了会提醒你", { type: "ok" });
    await openWork(activeShareId, out.run_id);
  } catch (e) { showErr(e); }
}

async function stopRun() {
  try {
    await api(`/v1/shares/${encodeURIComponent(activeShareId)}/runs/${encodeURIComponent(activeRunId)}/cancel`,
      { method: "POST", body: {} });
    toast("已停止后续步骤；已经发出的模型调用不能撤回", { type: "warn" });
    await refreshWork();
  } catch (e) { showErr(e); }
}

async function retryRun() {
  try {
    const out = await api(`/v1/shares/${encodeURIComponent(activeShareId)}/runs/${encodeURIComponent(activeRunId)}/retry`,
      { method: "POST", body: {}, idempotencyKey: newKey("retry") });
    await openWork(activeShareId, out.run_id);
  } catch (e) { showErr(e); }
}

// ---------- 轮询：只在任务还在跑时继续 ----------

const ACTIVE = new Set(["queued", "running", "retry_wait", "awaiting_runner"]);

function startPolling() {
  stopPolling();
  const tick = async () => {
    pollTimer = 0;
    if (!activeShareId) return;
    await refreshWork();
    const state = workCache && workCache.run ? workCache.run.state : "idle";
    if (ACTIVE.has(state)) pollTimer = window.setTimeout(tick, 3000);
  };
  pollTimer = window.setTimeout(tick, 2500);
}

function stopPolling() {
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = 0; }
}

// ---------- 入口 ----------

export function initShares() {
  installSelection();
  const nav = $("sharesBtn");
  if (nav) nav.addEventListener("click", () => openSharesView());
  document.addEventListener("click", (e) => {
    const li = e.target.closest && e.target.closest("li.item");
    if (selectMode && li) li.classList.toggle("picked", selected.has(li.dataset.id));
  });
}

export { setSelectMode, hideSharesView, stopPolling };
