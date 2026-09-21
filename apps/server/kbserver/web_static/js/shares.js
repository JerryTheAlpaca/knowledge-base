// shares.js — 分享创作：多选材料 → 一轮轮说清楚 → 生成可分享的 HTML（docs/20 §3）
//
// 页面直接复用首页那一层：金蔷薇压在强遮罩后面当背景，中间只剩和 AI 的对话，
// 底部沿用首页那副胶囊输入框。输入框这一句算什么，由当前状态决定：
// 开工要求 / 回答提问 / 调整方向 / 继续改成品。顶栏三横线那侧是以前所有的对话。
// 预览走「主站鉴权取数据 → 可信容器设置 srcdoc」，iframe 只开 allow-scripts。

import { $, api, esc, fmtShort, showErr, toast, isModalOpen, dismissToast, sanitizeFilename } from "./api.js";
import { setListPollPaused, refreshItems } from "./item-list.js";
import { currentDetailId, closeDetail, openDetail } from "./item-detail.js";

const SELECTED_KEY = "kb.share.selected.v1";
let selected = new Set(loadSelected());
let selectMode = false;
let mode = null;           // null | draft | work
let draftIds = [];         // 还没开工时选中的材料
let matsState = new Map(); // id -> { title, meta, warn }：草稿里读到的材料
let matsVersion = 0;       // 材料读完了/被服务端退回时递增，对话流据此重画
let activeShareId = null;
let activeRunId = null;
let pollTimer = 0;
let workCache = null;      // GET /v1/shares/{id} 的结果
let convCache = null;      // 对话与待答问题
let lastThreadSig = "";
let lastMsgCount = 0;
let lastRevisionCount = 0;
let pvOpen = false;        // 成品预览是否展开
let pvDoc = null, pvDocKey = "";
let sending = false;
let go = (url, state) => {
  // state 里带 view：退出时靠它判断这条历史是我们压进去的，可以直接 back 回去
  try { history.pushState(state || {}, "", url); } catch (e) { /* 忽略 */ }
};

function loadSelected() {
  try { return JSON.parse(localStorage.getItem(SELECTED_KEY) || "[]"); } catch (e) { return []; }
}

function saveSelected() {
  try { localStorage.setItem(SELECTED_KEY, JSON.stringify([...selected])); } catch (e) { /* 忽略 */ }
}

function selectedIds() { return [...selected]; }

// ---------- 列表选择模式 ----------

function setSelectMode(on) {
  selectMode = on;
  const toggle = $("selectToggle");
  if (toggle) {
    toggle.setAttribute("aria-pressed", on ? "true" : "false");
    toggle.classList.toggle("on", on);
  }
  document.body.classList.toggle("share-selecting", on);
  if (!on) showSelMenu(false);
  renderSelectionBar();
}

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
  bar.hidden = !(selectMode && selected.size > 0);
  const num = $("shareSelCount");
  if (num) num.textContent = String(selected.size);
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
  const done = $("shareSelDone");
  if (done) done.addEventListener("click", () => {
    const menu = $("shareSelMenu");
    showSelMenu(!!menu && menu.hidden);
  });
  const pick = (id, fn) => {
    const el = $(id);
    if (el) el.addEventListener("click", () => { showSelMenu(false); fn(); });
  };
  pick("shareDlSource", () => downloadChosen("source"));
  pick("shareDlDigest", () => downloadChosen("digest"));
  pick("shareMakeHtml", () => openWorkbench());
  // 点菜单外任何一处就收起；勾选项本身由上面的捕获监听负责
  document.addEventListener("click", (e) => {
    const menu = $("shareSelMenu");
    if (!menu || menu.hidden) return;
    if (!e.target.closest("#shareSelMenu") && !e.target.closest("#shareSelDone")) showSelMenu(false);
  });
}

function showSelMenu(on) {
  const menu = $("shareSelMenu");
  if (!menu) return;
  menu.hidden = !on;
  if (!on) return;
  // 贴着多选条上沿：条子在小屏会换行长高，写死 bottom 会把它盖住
  const bar = $("shareSelBar").getBoundingClientRect();
  menu.style.bottom = Math.round(window.innerHeight - bar.top + 10) + "px";
}

// ---------- 批量下载：原文取 readable/normalized.md，整理稿取 preview.md ----------

const DL_PATHS = { source: ["readable.md", "normalized.md"], digest: ["preview.md"] };
const DL_LABEL = { source: "原文", digest: "整理稿" };
let downloading = false;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function downloadChosen(kind) {
  if (downloading) return;
  const ids = selectedIds();
  if (!ids.length) return;
  downloading = true;
  const busy = toast("正在准备 " + ids.length + " 篇" + DL_LABEL[kind] + "…", { sticky: true });
  let ok = 0;
  let missed = 0;
  for (const id of ids) {
    const one = await downloadOne(id, kind);
    if (!one.ok) { missed++; continue; }
    ok++;
    await sleep(260);   // 连着点下载会被浏览器当成批量抓取，留一点间隔
  }
  dismissToast(busy);
  const tail = missed ? "，" + missed + " 篇还没有可下载的" + DL_LABEL[kind] : "";
  if (ok) toast("已下载 " + ok + " 篇" + DL_LABEL[kind] + tail, { type: missed ? "warn" : "ok" });
  else toast(missed + " 篇都还没有可下载的" + DL_LABEL[kind], { type: "warn" });
  downloading = false;
  if (kind === "source" && ok) refreshItems();   // 下载原文会把条目推到「已下载」
}

async function downloadOne(id, kind) {
  let d = null;
  try { d = await api("/v1/items/" + encodeURIComponent(id) + "/reading"); } catch (e) { d = null; }
  const title = (d && d.item && d.item.title) || "未命名材料";
  const sm = d && d.source_material;
  if (!sm) return { ok: false };
  const file = DL_PATHS[kind]
    .map((p) => (sm.files || []).find((f) => f.relative_path === p))
    .find(Boolean);
  if (!file) return { ok: false };
  let text = "";
  try {
    const r = await fetch(sm.download_base + "/" + encodeURIComponent(file.file_id),
      { credentials: "same-origin" });
    if (r.ok) text = await r.text();
  } catch (e) { text = ""; }
  if (!text.trim()) return { ok: false };
  if (kind === "source") text = cleanReadableMd(text);
  saveAsMd(text, sanitizeFilename(title) + (kind === "digest" ? "-整理稿" : "") + ".md");
  if (kind === "source") {
    // 下载走本地 Blob，服务器只能靠这次登记把条目判为已下载（docs/17 §5.2）
    try { await api("/v1/items/" + encodeURIComponent(id) + "/source-download", { method: "POST" }); }
    catch (e) { /* 登记失败不影响已经到手的文件 */ }
  }
  return { ok: true };
}

// 证据角标（^s0001）是给网页定位用的，落到文件里就是噪声
function cleanReadableMd(raw) {
  return raw.split("\n").map((l) => l.replace(/\s+\^[sp]\d{4,}\s*$/, ""))
    .join("\n").replace(/\n{3,}/g, "\n\n").trim() + "\n";
}

function saveAsMd(text, name) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/markdown;charset=utf-8" }));
  const a = document.createElement("a");
  a.href = url; a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ---------- 视图切换：对话舞台 / 以前的对话 ----------

function anyOverlayOpen() {
  return $("shareStage") && (!$("shareStage").hidden || !$("sharesView").hidden);
}

function syncChrome() {
  const open = anyOverlayOpen();
  document.body.classList.toggle("share-open", open);
  // 列表被盖住了就停掉它的轮询，舞台上另有自己的节奏
  setListPollPaused(open);
}

function showStage() {
  // 从条目详情进来时详情页是盖在首页上的：先收回，金蔷薇才露得出来
  if (currentDetailId()) closeDetail();
  $("sharesView").hidden = true;
  $("shareStage").hidden = false;
  syncChrome();
}

function showConvList() {
  $("shareStage").hidden = true;
  $("sharesView").hidden = false;
  stopPolling();
  syncChrome();
}

export function isSharesOpen() { return anyOverlayOpen(); }

export function hideSharesView() {
  $("shareStage").hidden = true;
  $("sharesView").hidden = true;
  mode = null;
  activeShareId = null;
  stopPolling();
  syncChrome();
}

// 路由进来：?share= 是一篇具体对话，?new= 是带着选中材料刚要开工，都没有就是对话记录
export async function openSharesView(shareId, isNew) {
  if (shareId) {
    if (shareId === activeShareId && !$("shareStage").hidden) return;
    showStage();
    await openWork(shareId);
    return;
  }
  if (isNew) {
    showStage();
    startDraft(draftIds.length ? draftIds : selectedIds());
    return;
  }
  showConvList();
  await renderWorksList();
}

// 顶栏三横线：以前的对话
function openConvList() {
  go("/inbox?view=shares", { view: "shares" });
}

// 从多选/详情进入：带着选中材料到舞台上
export function openWorkbench(itemIds) {
  const ids = itemIds && itemIds.length ? itemIds : selectedIds();
  if (!ids.length) { toast("先在「已收集」里勾几篇材料", { type: "warn" }); return; }
  draftIds = ids.slice(0, 30);
  selected.clear(); saveSelected(); syncRowMarks(); renderSelectionBar();
  setSelectMode(false);
  go("/inbox?view=shares&new=1", { view: "shares" });
}

function startDraft(ids) {
  mode = "draft";
  draftIds = ids && ids.length ? ids : draftIds;
  workCache = null; convCache = null; activeShareId = null; activeRunId = null;
  matsState = new Map();
  pvOpen = false; pvDoc = null; pvDocKey = "";
  lastRevisionCount = 0;
  stopPolling();
  $("stageInput").value = "";
  $("stageTitle").textContent = "分享创作";
  $("stageDelete").hidden = true;
  renderThread();
  renderDock();
  loadMaterials();
  $("stageInput").focus();
}

// ---------- 对话流 ----------

function runState() { return workCache && workCache.run ? workCache.run.state : null; }
function revisions() { return (workCache && workCache.revisions) || []; }

function threadSig() {
  const run = workCache && workCache.run;
  return JSON.stringify([
    mode, mode === "draft" ? [draftIds, matsVersion] : null,
    run ? [run.state, run.stage, run.reason_code, run.brief_version] : null,
    convCache ? convCache.messages.map((m) => m.seq) : null,
    revisions().length, workCache && workCache.round ? workCache.round.round_id : null,
    workCache ? workCache.share.status : null, pvOpen,
  ]);
}

function renderThread() {
  const host = $("stageThread");
  if (!host) return;
  const sig = threadSig();
  if (sig === lastThreadSig) return;
  const wasNearBottom = host.scrollHeight - host.scrollTop - host.clientHeight < 90;
  lastThreadSig = sig;
  host.innerHTML = mode === "draft" ? draftHTML() : workHTML();
  const count = convCache ? convCache.messages.length : 0;
  if (wasNearBottom || count > lastMsgCount) host.scrollTop = host.scrollHeight;
  lastMsgCount = count;
  wireThread();
  if (pvOpen) applyPreview();
}

function draftHTML() {
  if (!draftIds.length) {
    return '<div class="t-card"><p class="muted" style="margin:0">还没有选材料。回「已收集」点「分享」，勾几篇再过来。</p></div>';
  }
  const rows = draftIds.map((id) => {
    const m = matsState.get(id);
    if (!m) return '<li class="muted">正在读取材料…</li>';
    if (m.warn) {
      return '<li class="warn"><span class="mtitle">' + esc(m.title) +
        '</span><span class="mmeta">还没有可读正文</span>' +
        '<button class="btn ghost small" data-drop="' + esc(id) + '">移除</button>' +
        (m.fixable === false ? "" :
          '<button class="btn ghost small" data-fix="' + esc(id) + '">补充材料</button>') + "</li>";
    }
    return '<li><span class="mtitle">' + esc(m.title) + '</span>' +
      '<span class="mmeta">' + esc(m.meta) + "</span></li>";
  }).join("");
  return '<div class="t-card"><h4>这次用的材料</h4><ul class="t-mats">' + rows + "</ul></div>" +
    '<div class="t-msg ai"><p class="munder">把这几篇整合成一个网页。先说一句你想要的效果：给谁看、' +
    "想让人记住什么、要不要图。不想说细节就写「你决定」，我读完材料再问你几个问题。</p></div>";
}

function workHTML() {
  const messages = (convCache && convCache.messages) || [];
  const pendingRound = workCache && workCache.round ? workCache.round.round_id : null;
  const bubbles = messages.map((m) => {
    if (m.role === "user") {
      return '<div class="t-msg me">' + esc(m.text || "").replace(/\n/g, "<br>") + "</div>";
    }
    const understanding = m.understanding
      ? '<p class="munder">' + esc(m.understanding) + "</p>" : "";
    const qs = (m.questions || []).map((q) => questionHTML(q, m.round_id,
      answering() && m.round_id === pendingRound)).join("");
    return '<div class="t-msg ai">' + understanding + qs + "</div>";
  }).join("");
  return bubbles + briefHTML() + previewHTML();
}

function questionHTML(q, roundId, live) {
  const options = (q.options || []).map((o) => live
    ? '<label class="t-opt"><input type="radio" name="' + esc(roundId + ":" + q.id) + '" value="' +
      esc(o.id) + '"><span>' + esc(o.label) + "</span></label>"
    : '<span class="t-opt">' + esc(o.label) + "</span>").join("");
  const required = q.required_for_generation ? '<span class="tag-block">需要先确认</span>' : "";
  return '<div class="t-q" data-q="' + esc(q.id) + '" data-round="' + esc(roundId) + '">' +
    "<p>" + esc(q.text) + required + "</p>" +
    (q.reason ? '<p class="qreason">' + esc(q.reason) + "</p>" : "") +
    (options ? '<div class="t-opts">' + options + "</div>" : "") + "</div>";
}

function briefHTML() {
  const brief = workCache && workCache.brief;
  if (!brief || !brief.fields.length) return "";
  const rows = brief.fields.map((f) =>
    "<div><dt>" + esc(f.label) + "</dt><dd>" + f.value.map(esc).join("；") +
    (f.by === "ai" ? ' <span class="tag-ai">AI 暂定</span>' : "") + "</dd></div>").join("");
  return '<div class="t-card"><h4>目前的需求 <span class="small num">版本 ' + esc(brief.version) +
    "</span></h4><dl class=\"brief-dl\">" + rows + "</dl></div>";
}

function previewHTML() {
  const list = revisions();
  if (!list.length) return "";
  const latest = list[list.length - 1];
  const actions = (workCache && workCache.actions) || {};
  const published = workCache.share.status === "published";
  const body = '<div class="pv-body"' + (pvOpen ? "" : " hidden") + ">" +
    '<iframe id="shareFrame" class="share-frame" sandbox="allow-scripts" title="作品预览"></iframe>' +
    (published
      ? '<p class="share-link">已分享：<a id="shareLink" href="#" target="_blank" rel="noopener noreferrer">' +
        '打开链接</a> <button class="btn ghost small" id="shareRevoke">撤销</button></p>'
      : "") + "</div>";
  return '<div class="t-card"><h4>做好了，先看一眼 <span class="small num">v' + esc(latest.revision) + "</span></h4>" +
    '<div class="pv-row">' +
    '  <button class="btn small" id="pvToggle">' + (pvOpen ? "收起预览" : "展开预览") + "</button>" +
    (actions.can_publish ? '  <button class="btn primary small" id="sharePublish">分享</button>' : "") +
    '  <a class="btn small" href="/v1/shares/' + esc(activeShareId) + "/revisions/" + latest.revision +
    '/download" download>下载 HTML</a></div>' +
    (pvOpen ? body : "") + "</div>";
}

function wireThread() {
  const on = (id, fn) => { const el = $(id); if (el) el.onclick = fn; };
  on("pvToggle", () => { pvOpen = !pvOpen; renderThread(); renderDock(); });
  on("sharePublish", publishWork);
  on("shareRevoke", revokeWork);
  document.querySelectorAll(".t-mats [data-drop]").forEach((b) => b.addEventListener("click", () => {
    draftIds = draftIds.filter((x) => x !== b.dataset.drop);
    matsState.delete(b.dataset.drop);
    matsVersion += 1;
    renderThread();
    renderDock();
  }));
  document.querySelectorAll(".t-mats [data-fix]").forEach((b) => b.addEventListener("click", () => {
    // 退回首页去把这篇材料补充完整
    hideSharesView();
    openDetail(b.dataset.fix);
  }));
}

// ---------- 底部：状态、随状态出现的按钮、输入框 ----------

function answering() { return runState() === "waiting_user"; }
function confirming() { return runState() === "awaiting_confirmation"; }
function running() { return ["queued", "running", "retry_wait", "awaiting_runner"].includes(runState()); }
function halted() { return ["failed", "unknown_outcome", "cancelled"].includes(runState()); }

function renderDock() {
  const status = $("stageStatus");
  const run = workCache && workCache.run;
  if (run) {
    status.hidden = false;
    status.innerHTML = '<span class="pill">' + esc(run.status_text) + "</span>" +
      (run.reason_text ? '<span class="reason">' + esc(run.reason_text) + "</span>" : "");
  } else {
    status.hidden = true;
    status.innerHTML = "";
  }
  const actions = $("stageActions");
  const chips = [];
  if (running()) chips.push('<button class="btn ghost small" id="shareStop">停止</button>');
  if (halted()) chips.push('<button class="btn small" id="shareRetry">重新尝试</button>');
  if (confirming()) {
    chips.push('<button class="btn primary small" id="shareConfirm">确认并生成</button>');
    chips.push('<button class="btn small" id="shareDelegate">按你的建议生成</button>');
  }
  actions.hidden = chips.length === 0;
  actions.innerHTML = chips.join("");
  const on = (id, fn) => { const el = $(id); if (el) el.onclick = fn; };
  on("shareStop", stopRun);
  on("shareRetry", retryRun);
  on("shareConfirm", () => startGeneration("confirm"));
  on("shareDelegate", () => startGeneration("delegate_preferences"));
  $("stageDelete").hidden = mode !== "work";
  $("stageTitle").textContent = mode === "work" && workCache ? workCache.title : "分享创作";

  const input = $("stageInput");
  const send = $("stageSend");
  const hint = $("stageHint");
  input.disabled = !!running();
  send.disabled = !!running();
  if (mode === "draft") {
    const blocked = blockedMats();
    input.disabled = blocked;
    send.disabled = blocked;
    hint.textContent = !matsState.size && draftIds.length
      ? "正在读你选的材料…"
      : (blocked ? "上面这几篇还没有可读正文：先移除它们，或去把材料补充完整。"
        : (draftIds.length ? "共 " + draftIds.length + " 篇材料 · 不填也可以，AI 会先读材料再问你" : ""));
    input.placeholder = "例：整理出它们的差异和相互启发，适合初学者阅读；有必要再配图。";
  } else if (answering()) {
    hint.textContent = "";
    input.placeholder = "可以逐题回答，也可以一次说整组；也可以直接说「你决定，直接做」。";
  } else if (confirming()) {
    hint.textContent = "方向不对就直接说，AI 会改需求再来确认一次。";
    input.placeholder = "例：读者是同行，不用解释基础名词。";
  } else if (running()) {
    hint.textContent = "正在做，好了会在这里继续。";
    input.placeholder = "AI 正在处理…";
  } else if (revisions().length) {
    hint.textContent = "";
    input.placeholder = "想改哪里？例：缩短一点、突出差异、把这一部分画成结构图。";
  } else if (halted()) {
    // 这一轮停住了又还没有成品：话没处接，只能先把这一轮重新跑起来
    input.disabled = true;
    send.disabled = true;
    hint.textContent = "这一轮停住了，点「重新尝试」接着做。";
    input.placeholder = "先重新尝试这一轮";
  } else {
    hint.textContent = "";
    input.placeholder = "说说你想做成什么样……";
  }
}

// ---------- 草稿：材料可用性由服务端判定 ----------

async function loadMaterials() {
  if (mode !== "draft") return;
  const next = new Map();
  for (const id of draftIds) {
    let it = null;
    try { it = await api("/v1/items/" + encodeURIComponent(id)); } catch (e) { it = null; }
    if (!it) { next.set(id, { title: "有一篇材料已经打不开了", meta: "", warn: true, fixable: false }); continue; }
    // 界面只做粗筛；到底能不能读由服务端判定，422 会带回具体条目
    const readable = ["ready", "extracted"].includes(it.pipeline_state);
    next.set(id, { title: it.title || "未命名材料", meta: it.source_label || "", warn: !readable });
  }
  if (mode !== "draft") return;
  matsState = next;
  matsVersion += 1;
  renderThread();
  renderDock();
}

function blockedMats() {
  for (const m of matsState.values()) if (m.warn) return true;
  return matsState.size === 0 && draftIds.length > 0;   // 还在读，先不让发出去
}

async function startCreation(itemIds, instructions) {
  try {
    const created = await api("/v1/shares", {
      method: "POST",
      body: { item_ids: itemIds, instructions },
      idempotencyKey: newKey("share"),
    });
    draftIds = [];
    await openWork(created.share_id, created.run_id, true);
    toast("已开始，AI 正在读你选的材料", { type: "ok" });
  } catch (e) {
    const bad = e && e.details && e.details.unreadable_items;
    if (Array.isArray(bad) && bad.length) {
      // 服务端判定哪几篇不可读：就地给出「移除」，不悄悄排除
      for (const item of bad) {
        matsState.set(item.item_id, {
          title: item.title || "未命名材料", meta: "", warn: true });
      }
      matsVersion += 1;
      renderThread();
      renderDock();
      toast("有材料还没有可读正文", { type: "warn" });
      return;
    }
    showErr(e);
  }
}

function newKey(prefix) {
  return prefix + "-" + Math.random().toString(36).slice(2) + Date.now().toString(36);
}

// ---------- 以前的对话 ----------

async function renderWorksList() {
  const host = $("shareWorks");
  if (!host) return;
  host.innerHTML = '<li class="muted">正在加载…</li>';
  let data;
  try { data = await api("/v1/shares?limit=50"); } catch (e) { showErr(e); host.innerHTML = ""; return; }
  if (!data.items.length) {
    host.innerHTML = '<li class="muted">还没有对话。在「已收集」里点「分享」，勾几篇材料就能开始。</li>';
    return;
  }
  host.innerHTML = data.items.map((w) =>
    '<li class="work" data-share="' + esc(w.share_id) + '" role="button" tabindex="0">' +
    '<span class="wtitle">' + esc(w.title) + "</span>" +
    '<span class="wmeta">' + esc(w.status_text) +
    (w.latest_revision ? " · v" + w.latest_revision : "") +
    (w.share_status === "published" ? " · 已分享" : "") +
    " · " + esc(fmtShort(w.updated_at)) + "</span></li>").join("");
  host.querySelectorAll(".work").forEach((li) => {
    const open = () => go("/inbox?view=shares&share=" + encodeURIComponent(li.dataset.share),
      { view: "shares" });
    li.addEventListener("click", open);
    li.addEventListener("keydown", (e) => { if (e.key === "Enter") open(); });
  });
}

// ---------- 单个作品：多轮问答 + 成品 ----------

async function openWork(shareId, runId, replaceUrl) {
  activeShareId = shareId;
  mode = "work";
  showStage();
  const params = new URLSearchParams(location.search);
  params.set("view", "shares");
  params.set("share", shareId);
  params.delete("new");
  if (runId) params.set("run", runId);
  const url = "/inbox?" + params.toString();
  try {
    if (replaceUrl) history.replaceState({ view: "shares" }, "", url);
    else if (location.pathname + location.search !== url) history.pushState({ view: "shares" }, "", url);
  } catch (e) { /* 忽略 */ }
  pvOpen = false; pvDoc = null; pvDocKey = "";
  lastRevisionCount = 0;
  await refreshWork();
  startPolling();
  $("stageInput").focus();
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
  // 版本多了一个就是刚做好：预览自己展开，不用再去点
  if (revisions().length > lastRevisionCount) pvOpen = true;
  lastRevisionCount = revisions().length;
  renderThread();
  renderDock();
}

// ---------- 预览 / 下载 / 发布 ----------

async function applyPreview() {
  const frame = $("shareFrame");
  const list = revisions();
  if (!frame || !activeShareId || !list.length) return;
  const latest = list[list.length - 1];
  const key = activeShareId + ":" + latest.revision;
  if (pvDoc && pvDocKey === key) { frame.srcdoc = pvDoc; return; }
  try {
    const data = await api(`/v1/shares/${encodeURIComponent(activeShareId)}/revisions/${latest.revision}/preview-content`);
    pvDoc = data.document; pvDocKey = key;
    const again = $("shareFrame");   // 取的这段时间里可能已经换了 DOM
    if (again && pvDocKey === key) again.srcdoc = pvDoc;
  } catch (e) { showErr(e); }
}

async function publishWork() {
  const list = revisions();
  if (!list.length) return;
  try {
    const out = await api(`/v1/shares/${encodeURIComponent(activeShareId)}/publish`, {
      method: "POST",
      body: { revision_id: list[list.length - 1].revision_id,
        expected_work_version: workCache.version },
      idempotencyKey: newKey("pub"),
    });
    await refreshWork();
    const link = $("shareLink");
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
  if (!window.confirm("删除这次对话？已生成的分享链接会立即失效。")) return;
  try {
    await api("/v1/shares/" + encodeURIComponent(activeShareId), { method: "DELETE" });
    activeShareId = null;
    go("/inbox");
    toast("已删除", { type: "ok" });
  } catch (e) { showErr(e); }
}

function copy(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).catch(() => { /* 忽略 */ });
  }
}

// ---------- 发送：这一句按当前状态决定算什么 ----------

function collectAnswers() {
  const out = [];
  document.querySelectorAll("#stageThread .t-q").forEach((q) => {
    if (!q.querySelector("input")) return;   // 只有当前待答的那一组可填
    const picked = q.querySelector("input:checked");
    if (picked) out.push({ question_id: q.dataset.q, option_ids: [picked.value], text: "" });
  });
  return out;
}

function takeInput() {
  const el = $("stageInput");
  const text = (el.value || "").trim();
  el.value = "";
  autosize();
  return text;
}

function autosize() {
  const t = $("stageInput");
  t.style.height = "";
  t.style.height = Math.min(t.scrollHeight, 220) + "px";
}

async function onSend() {
  if (sending) return;
  const el = $("stageInput");
  if (mode === "draft" && !draftIds.length) {
    toast("先在「已收集」里勾几篇材料", { type: "warn" });
    el.focus();
    return;
  }
  const text = takeInput();
  sending = true;
  try {
    if (mode === "draft") {
      await startCreation(draftIds, text);   // 空着也可以：AI 会先读材料再问
      return;
    }
    if (!activeShareId) return;
    if (!text) { el.focus(); return; }       // 对话里空着不算一句话
    if (answering() || confirming()) await sendAnswer(text);
    else if (revisions().length) await submitModify(text);
    else toast("AI 还在读材料，等它问完再说", { type: "warn" });
  } finally {
    sending = false;
  }
}

async function sendAnswer(free) {
  const round = workCache && workCache.round;
  if (!round) return;
  try {
    await api(`/v1/shares/${encodeURIComponent(activeShareId)}/runs/${encodeURIComponent(activeRunId)}/messages`, {
      method: "POST",
      body: {
        expected_conversation_version: convCache ? convCache.conversation_version : 0,
        round_id: round.round_id, answers: collectAnswers(), message: free,
      },
      idempotencyKey: newKey("ans"),
    });
    await refreshWork();
    startPolling();
  } catch (e) { showErr(e); }
}

async function startGeneration(modeName) {
  try {
    await api(`/v1/shares/${encodeURIComponent(activeShareId)}/runs/${encodeURIComponent(activeRunId)}/start`, {
      method: "POST",
      body: {
        expected_conversation_version: convCache ? convCache.conversation_version : null,
        expected_brief_version: workCache.brief ? workCache.brief.version : null,
        mode: modeName,
      },
      idempotencyKey: newKey("start"),
    });
    await refreshWork();
    startPolling();
  } catch (e) { showErr(e); }
}

async function submitModify(instructions) {
  const list = revisions();
  if (!list.length) return;
  try {
    const out = await api(`/v1/shares/${encodeURIComponent(activeShareId)}/runs`, {
      method: "POST",
      body: { base_revision_id: list[list.length - 1].revision_id,
        instructions, expected_work_version: workCache.version },
      idempotencyKey: newKey("mod"),
    });
    toast("已排入修改，好了会提醒你", { type: "ok" });
    await openWork(activeShareId, out.run_id, true);
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
    await openWork(activeShareId, out.run_id, true);
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

function exitOverlay() {
  // 进入舞台/记录时压了一条历史：back 回到进入前的位置（同设置页）
  if (history.state && history.state.view === "shares") { history.back(); return; }
  go("/inbox");
}

export function initShares(navigator) {
  if (navigator) go = navigator;
  installSelection();
  const nav = $("sharesBtn");
  if (nav) nav.addEventListener("click", openConvList);
  const closeStage = $("stageClose");
  if (closeStage) closeStage.addEventListener("click", exitOverlay);
  const closeConv = $("sharesClose");
  if (closeConv) closeConv.addEventListener("click", exitOverlay);
  const del = $("stageDelete");
  if (del) del.addEventListener("click", deleteWork);
  const input = $("stageInput");
  if (input) {
    input.addEventListener("input", autosize);
    // 回车发送、Shift+回车换行：和首页采集框一致的低位摩擦
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        if (!input.disabled) onSend();
      }
    });
  }
  const send = $("stageSend");
  if (send) send.addEventListener("click", () => onSend());
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || isModalOpen()) return;
    const menu = $("shareSelMenu");
    if (menu && !menu.hidden) { showSelMenu(false); return; }   // 先收菜单
    if (!$("shareStage").hidden || !$("sharesView").hidden) { exitOverlay(); return; }
    if (selectMode) setSelectMode(false);                      // 再收多选
  });
  document.addEventListener("click", (e) => {
    const li = e.target.closest && e.target.closest("li.item");
    if (selectMode && li) li.classList.toggle("picked", selected.has(li.dataset.id));
  });
}
