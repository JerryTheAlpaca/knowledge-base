// shares.js — 分享创作：多选材料 → 一轮轮说清楚 → 生成可分享的 HTML（docs/20 §3）
//
// 页面直接复用首页那一层：金蔷薇压在强遮罩后面当背景，中间只剩和 AI 的对话，
// 底部沿用首页那副胶囊输入框。输入框这一句算什么，由当前状态决定：
// 开工要求 / 回答提问 / 调整方向 / 继续改成品。顶栏三横线那侧是以前所有的对话。
// 预览走「主站鉴权取数据 → 可信容器设置 srcdoc」，iframe 只开 allow-scripts。

import { $, api, esc, fmtShort, showErr, toast, isModalOpen, dismissToast, sanitizeFilename } from "./api.js";
import { setListPollPaused, refreshItems, onRowsRendered } from "./item-list.js";
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
let shareLink = null;      // 分享链接：null 还没去取 / false 取不到 / 字符串就是可用链接
let convCache = null;      // 对话与待答问题
let lastThreadSig = "";
let lastMsgCount = 0;
let lastRevisionCount = 0;
let pvOpen = false;        // 成品预览是否展开
let pvDoc = null, pvDocKey = "";
let sending = false;
let qStep = 0;             // 当前待答轮里显示到第几题
let qAns = new Map();      // question_id -> { opt, other }：切题/重画都从这份状态还原
let qRoundId = null;       // qStep/qAns 属于哪一轮，换轮就清空
let draftKey = null;       // 这一件新作品的幂等键：进草稿生成一次，超时重试复用同一把
let draftKeySig = "";      // 键对应的请求内容；换了材料或改了说法才算另一件事，才换新键
let planOpen = false;      // 顶部「这一版的方向」折叠态
let planAuto = "";         // 为哪一版需求自动展开过，避免每次轮询都抢回展开
let openRounds = new Set();// 已答问题轮次里被手动摊开的 round_id
let procKey = "";          // 过程块的折叠态属于哪一次运行
let procOpen = null;       // null = 还没手动碰过，跟着「是不是在跑」自动收放
let procLive = null;       // 上一次画出来时是不是在跑，用来在翻转的那一刻把开合交还给自动
let procTimer = 0;         // 过程块自己的一秒计时，不牵动整条对话流重画
let procRun = "";          // 下面三个量在给哪一次运行计时
let procFrom = 0;          // 这一段「真在跑」从什么时候开始看到
let procMs = 0;            // 已经累计到的处理时长
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
  // 点亮标记每次都按 selected 重画：进多选时列表早就画好了，不补一次就只剩计数对得上。
  // 退出多选不清类——发光写在 body.share-selecting 作用域里，作用域一撤就整层灭掉
  syncRowMarks();
  renderSelectionBar();
}

function toggleItem(itemId) {
  if (selected.has(itemId)) selected.delete(itemId); else selected.add(itemId);
  saveSelected();
  syncRowMarks();
  renderSelectionBar();
}

// 把 selected 摊到当前画出来的行上。列表每次重画都会经 onRowsRendered 回到这里一次，
// 勾选/清空/进出多选态这几个改 selected 的地方也各自调，卡片就不会和计数分家
function syncRowMarks() {
  document.querySelectorAll("ul.items li.item").forEach((li) => {
    li.classList.toggle("picked", selected.has(li.dataset.id));
  });
}

function renderSelectionBar() {
  const bar = $("shareSelBar");
  if (!bar) return;
  // 一进选择态就亮条子，0 篇也在：否则点「分享」页面毫无变化
  bar.hidden = !selectMode;
  bar.classList.toggle("has-sel", selected.size > 0);
  const num = $("shareSelCount");
  if (num) num.textContent = String(selected.size);
  const empty = selected.size === 0;
  const clear = $("shareSelClear");
  if (clear) clear.disabled = empty;
  const done = $("shareSelDone");
  if (done) done.disabled = empty;
}

function installSelection() {
  // 列表重画只有一个出口，就在这里挂一次：轮询、切回标签页、搜索都不用在各自地方补标记
  onRowsRendered(syncRowMarks);
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
    // 键盘走同一条路：勾选态里 Tab 到卡片按回车/空格是勾选，不是跳详情
    ul.addEventListener("keydown", (e) => {
      if (!selectMode || (e.key !== "Enter" && e.key !== " ")) return;
      const li = e.target.closest("li.item");
      if (!li || e.target !== li) return;
      e.preventDefault();
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

// 舞台（那一件作品的对话）是不是开着：作品轮询只在开着时才继续排下一轮
function stageIsOpen() {
  return !!$("shareStage") && !$("shareStage").hidden;
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
  // setSelectMode 里会把标记和条子一起重画，这里只改集合
  selected.clear(); saveSelected();
  setSelectMode(false);
  go("/inbox?view=shares&new=1", { view: "shares" });
}

function startDraft(ids) {
  mode = "draft";
  draftIds = ids && ids.length ? ids : draftIds;
  workCache = null; convCache = null; activeShareId = null; activeRunId = null;
  shareLink = null;
  matsState = new Map();
  // 一件新作品一把幂等键：这一份草稿里超时重试都用它，换到下一份草稿才换键
  // （键在第一次真的发出去时现造，见 startCreation）
  draftKey = null; draftKeySig = "";
  pvOpen = false; pvDoc = null; pvDocKey = "";
  resetRoundViews();
  lastRevisionCount = 0;
  stopPolling();
  resetStageInput();
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
    workCache ? [workCache.share.status, shareLink] : null, pvOpen,
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
  syncProcClock();
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
  const answered = answersByRound();
  const bubbles = messages.map((m) => {
    if (m.role === "user") {
      // 勾了哪些选项已经折在上面那组问题里，你这一侧只留自己打的那句话
      const t = (m.answers || []).length ? (m.free_text || "") : (m.text || "");
      if (!t.trim()) return "";
      return '<div class="t-msg me">' + esc(t).replace(/\n/g, "<br>") + "</div>";
    }
    const understanding = m.understanding
      ? '<p class="munder">' + esc(m.understanding) + "</p>" : "";
    // 待答的这一组挪到输入框上方一题一题答，这里只把答完的折起来留档
    const qs = m.questions || [];
    const round = (!qs.length || (answering() && m.round_id === pendingRound))
      ? "" : roundHTML(qs, m.round_id, answered.get(m.round_id));
    if (!understanding && !round) return "";
    return '<div class="t-msg ai">' + understanding + round + "</div>";
  }).join("");
  return bubbles + procHTML() + previewHTML();
}

// 按 round_id 把结构化的回答配回提问那一组，用来折叠和标出当时选了哪个
function answersByRound() {
  const out = new Map();
  for (const m of ((convCache && convCache.messages) || [])) {
    if (m.role !== "user" || !m.round_id) continue;
    const cur = out.get(m.round_id) || new Map();
    for (const a of m.answers || []) if (a && a.question_id) cur.set(a.question_id, a);
    out.set(m.round_id, cur);
  }
  return out;
}

function chosenText(q, a) {
  if (!a) return "";
  const labels = {};
  for (const o of q.options || []) labels[o.id] = o.label;
  const parts = (a.option_ids || []).map((id) => labels[id] || id);
  const free = (a.text || "").trim();
  if (free) parts.push(free);
  return parts.join("、");
}

function roundHTML(qs, roundId, ans) {
  const chips = qs.map((q) => chosenText(q, ans && ans.get(q.id)) || "未回答")
    .map((t) => '<span class="t-chip">' + esc(t) + "</span>").join("");
  return '<details class="t-round"' + (openRounds.has(roundId) ? " open" : "") +
    ' data-round="' + esc(roundId) + '">' +
    '<summary><span class="t-round-q">问了 ' + qs.length + " 个问题</span>" +
    '<span class="t-round-a">' + chips + '</span>' +
    '<span class="proc-chev" aria-hidden="true"></span></summary>' +
    qs.map((q) => questionHTML(q, roundId, false, ans && ans.get(q.id))).join("") +
    "</details>";
}

// 待答轮：一次只显示一个问题，右上角「往左／往右」切上一题、下一题
function stepperHTML() {
  const round = mode === "work" && answering() ? (workCache && workCache.round) : null;
  const qs = (round && round.questions) || [];
  if (!qs.length) return "";
  if (qRoundId !== round.round_id) { qRoundId = round.round_id; qStep = 0; qAns = new Map(); }
  const i = Math.max(0, Math.min(qStep, qs.length - 1));
  const done = qs.filter(isAnswered).length;
  const all = done === qs.length;
  const chev = (back) => '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    (back ? '<polyline points="15 18 9 12 15 6"/>' : '<polyline points="9 18 15 12 9 6"/>') + "</svg>";
  const head = '<div class="q-step-head"><span class="q-step-pos">第 ' + (i + 1) + " / " + qs.length +
    " 个</span>" + dotsHTML(qs, i) + '<span class="spacer"></span>' +
    '<button class="icon ghost q-nav q-prev"' + (i === 0 ? " disabled" : "") +
      ' aria-label="上一个问题" title="上一个问题">' + chev(true) + "</button>" +
    '<button class="icon ghost q-nav q-next"' + (i === qs.length - 1 ? " disabled" : "") +
      ' aria-label="下一个问题" title="下一个问题">' + chev(false) + "</button></div>";
  // 答完最后一题不用再找发送键：主按钮就地给出，没答完也留着当「先提交这些」
  const foot = '<div class="q-foot"><span class="q-done' + (all ? " all" : "") + '">' +
    (all ? "都答完了" : "已答 " + done + " / " + qs.length) + "</span>" +
    '<button class="btn ' + (all ? "primary " : "ghost ") + 'small" id="askSubmit">' +
    (all ? "提交这 " + qs.length + " 个回答" : "提交回答") + "</button></div>";
  return '<div class="t-card q-step">' + head + questionHTML(qs[i], round.round_id, true) + foot + "</div>";
}

// 一排小点当进度：答过的填色，当前这题描边，比「2/3」更一眼看出还剩几题
function dotsHTML(qs, cur) {
  if (qs.length < 2) return "";
  return '<span class="q-dots" aria-hidden="true">' + qs.map((q, k) =>
    '<span class="q-dot' + (isAnswered(q) ? " done" : "") + (k === cur ? " now" : "") + '"></span>').join("") +
    "</span>";
}

function isAnswered(q) {
  const a = qAns.get(q.id);
  if (!a) return false;
  if (a.opt === "__other__") return !!(a.other || "").trim();
  return !!a.opt;
}

// 选了预设选项就算答完这一题，自动往后找第一道还没答的；改已答过的不抢焦点
function advanceFrom(qs, i) {
  for (let k = i + 1; k < qs.length; k++) {
    if (isAnswered(qs[k])) continue;
    qStep = k;
    renderAsk();
    focusOption();
    return;
  }
  renderAsk();   // 后面都答完了：留在本题，把「都答完了」画出来
}

function questionHTML(q, roundId, live, ans) {
  const a = live ? (qAns.get(q.id) || {}) : {};
  const name = esc(roundId + ":" + q.id);
  // 没有任何预设选项的开放题：直接把「其他」摊开让人写
  const chosen = live ? (a.opt || ((q.options || []).length ? null : "__other__")) : null;
  const picked = ans ? (ans.option_ids || []) : [];
  const opts = (q.options || []).map((o) => live
    ? '<label class="t-opt"><input type="radio" name="' + name + '" value="' + esc(o.id) + '"' +
      (chosen === o.id ? " checked" : "") + '><span>' + esc(o.label) + "</span></label>"
    : '<span class="t-opt' + (picked.includes(o.id) ? " t-opt--chosen" : "") + '">' +
      '<span class="t-dot" aria-hidden="true"></span><span>' + esc(o.label) + "</span></span>");
  if (live) {
    opts.push('<label class="t-opt t-opt--other"><input type="radio" name="' + name +
      '" value="__other__"' + (chosen === "__other__" ? " checked" : "") +
      '><span>其他</span></label>');
  }
  const other = live ? '<div class="q-other"' + (chosen === "__other__" ? "" : " hidden") + ">" +
    '<textarea class="q-other-input" rows="2" data-q="' + esc(q.id) +
    '" placeholder="写下你自己的想法……" aria-label="其他：写下你的需求">' + esc(a.other || "") +
    "</textarea></div>" : "";
  // 答完的轮次里，「其他」这一项和当时写的话一起补在选项后面
  const free = ans ? (ans.text || "").trim() : "";
  if (free) {
    opts.push('<span class="t-opt t-opt--other t-opt--chosen">' +
      '<span class="t-dot" aria-hidden="true"></span><span>其他</span></span>');
  }
  const freeBlock = live || !free ? "" : '<div class="t-free">' + esc(free) + "</div>";
  const required = q.required_for_generation ? '<span class="tag-block">需要先确认</span>' : "";
  return '<div class="t-q"' + (live ? ' data-q="' + esc(q.id) + '"' : "") + ">" +
    "<p>" + esc(q.text) + required + "</p>" +
    (q.reason ? '<p class="qreason">' + esc(q.reason) + "</p>" : "") +
    (opts.length ? '<div class="t-opts">' + opts.join("") + "</div>" : "") + other + freeBlock + "</div>";
}

// ---------- 过程信息：把服务端真实在跑的那一步摊开，跑完自己折回一行 ----------

const PROC_STEPS = [
  { label: "读取材料", at: ["preparing"] },
  { label: "理解需求", at: ["clarifying"] },
  { label: "整合内容", at: ["synthesizing"] },
  { label: "制作页面", at: ["generating"] },
  { label: "检查页面", at: ["packaging", "awaiting_runner", "checking", "repairing", "waiting_resources"] },
];

function procIdx() {
  const run = workCache && workCache.run;
  if (!run) return -1;
  if (["succeeded", "ready"].includes(run.state)) return PROC_STEPS.length;
  const i = PROC_STEPS.findIndex((s) => s.at.includes(run.stage));
  return i < 0 ? 0 : i;
}

function procElapsed() {
  return procMs + (procFrom ? Date.now() - procFrom : 0);
}

function fmtMs(ms) {
  const s = Math.round(ms / 1000);
  return s < 60 ? s + " 秒" : Math.floor(s / 60) + " 分 " + (s % 60) + " 秒";
}

// 只累计这一轮真在服务器跑的时间：从本页第一次看到它在动算起，停下来就收表。
// run.created_at 里含着用户思考的几分钟，拿它当「处理了多久」会把等待算成干活。
function tickProc() {
  const run = workCache && workCache.run;
  if (!run) return;
  if (procRun !== run.run_id) { procRun = run.run_id; procFrom = 0; procMs = 0; }
  if (running()) { if (!procFrom) procFrom = Date.now(); }
  else if (procFrom) { procMs += Date.now() - procFrom; procFrom = 0; }
}

function procLabel() {
  const ms = procElapsed();
  if (!ms) return "";
  return (procFrom ? "已用 " : "用时 ") + fmtMs(ms);
}

function procHTML() {
  const run = mode === "work" && workCache ? workCache.run : null;
  if (!run) return "";
  if (procKey !== run.run_id) { procKey = run.run_id; procOpen = null; }
  const idx = procIdx();
  const live = running();
  // 开跑自己摊开、停下自己收起；用户在这中间的手动开合只保留到下一次状态翻转
  if (procLive !== live) { procLive = live; procOpen = null; }
  const open = procOpen === null ? live : procOpen;
  const rounds = ((convCache && convCache.messages) || []).filter((m) => m.role === "user"
    && (m.answers || []).length).length;
  const note = (k) => k === 1 && rounds ? rounds + " 轮问答"
    : (k === 4 && run.repair_count ? "调整 " + run.repair_count + " 次" : "");
  const steps = PROC_STEPS.map((s, k) => {
    const state = idx > k ? "ok" : (idx === k ? (halted() ? "bad" : "now") : "");
    const txt = s.label + (note(k) ? ' <span class="small num">' + note(k) + "</span>" : "");
    return '<li class="' + state + '"><span class="proc-mark" aria-hidden="true"></span>' +
      "<span>" + txt + "</span></li>";
  }).join("");
  const stepName = idx >= 0 && idx < PROC_STEPS.length ? PROC_STEPS[idx].label : "";
  const head = ["succeeded", "ready"].includes(run.state) ? "已完成"
    : (run.state === "queued" && stepName ? "排队中 · " + stepName : run.status_text);
  return '<details class="t-card t-proc"' + (open ? " open" : "") + " data-proc>" +
    '<summary><span class="proc-sum"' + (live ? ' data-live="1"' : "") + ">" + esc(head) + "</span>" +
    '<span class="proc-cost" id="procElapsed">' + esc(procLabel()) + "</span>" +
    (run.attempt > 1 ? '<span class="tag-ai">第 ' + esc(run.attempt) + " 次尝试</span>" : "") +
    '<span class="spacer"></span><span class="proc-chev" aria-hidden="true"></span></summary>' +
    '<ul class="proc-steps"' + (live ? ' data-live="1"' : "") + ">" + steps + "</ul>" +
    "</details>";
}

// 秒数自己走，不为它重画整条对话流
function syncProcClock() {
  tickProc();
  const el = $("procElapsed");
  if (el) el.textContent = procLabel();   // 刚停下的这一次重画，文案要从「已用」换成「用时」
  if (!running()) { stopProcClock(); return; }
  if (procTimer) return;
  procTimer = window.setInterval(() => {
    tickProc();
    const el = $("procElapsed");
    if (!el) { stopProcClock(); return; }
    el.textContent = procLabel();
    if (!running()) stopProcClock();
  }, 1000);
}

function stopProcClock() {
  if (procTimer) { clearInterval(procTimer); procTimer = 0; }
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
      ? '<p class="share-link">已分享：' + linkHTML() +
        ' <button class="btn ghost small" id="shareRevoke">撤销</button></p>'
      : "") + "</div>";
  return '<div class="t-card"><h4>做好了，先看一眼 <span class="small num">v' + esc(latest.revision) + "</span></h4>" +
    '<div class="pv-row">' +
    '  <button class="btn small" id="pvToggle">' + (pvOpen ? "收起预览" : "展开预览") + "</button>" +
    (actions.can_publish ? '  <button class="btn primary small" id="sharePublish">分享</button>' : "") +
    '  <a class="btn small" href="/v1/shares/' + esc(activeShareId) + "/revisions/" + latest.revision +
    '/download" download>下载 HTML</a></div>' +
    (pvOpen ? body : "") + "</div>";
}

// 分享链接不在作品详情里（详情只有 has_link 这个布尔），要单独问一次 /link。
// 没拿到真链接之前不渲染指向 # 的空锚点：刷新后点了没反应，用户只会以为链接坏了。
// 锚点文字固定「打开链接」，不在发布当场换成整串 URL——那一行会横着长出去（审查 U-02）
function linkHTML() {
  if (typeof shareLink === "string" && shareLink) {
    return '<a href="' + esc(shareLink) + '" target="_blank" rel="noopener noreferrer">打开链接</a>';
  }
  return '<span class="muted">' + (shareLink === null ? "正在取链接…" : "链接不可用") + "</span>";
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
  // 折叠态记在变量里：轮询重画对话流时才不会把用户刚摊开的那组问题折回去
  const proc = document.querySelector("[data-proc]");
  if (proc) proc.addEventListener("toggle", () => { procOpen = proc.open; });
  document.querySelectorAll(".t-round").forEach((d) => {
    d.addEventListener("toggle", () => {
      if (d.open) openRounds.add(d.dataset.round); else openRounds.delete(d.dataset.round);
    });
  });
}

// ---------- 顶部：这一版的方向，折成一行也能随时点开对照 ----------

function renderPlan() {
  const host = $("stagePlan");
  if (!host) return;
  const brief = mode === "work" && workCache ? workCache.brief : null;
  if (!brief || !brief.fields.length) { host.hidden = true; host.innerHTML = ""; return; }
  // 走到「请确认方向」这一步，方案就是被确认的东西：自动摊开一次，之后收放归用户
  const key = activeRunId + ":" + brief.version;
  if (confirming() && planAuto !== key) { planAuto = key; planOpen = true; }
  const shown = brief.fields.filter((f) => f.value && f.value.length);
  const digest = shown.slice(0, 2).map((f) => f.label + "：" + f.value.join("；")).join("　·　");
  host.hidden = false;
  host.innerHTML = '<details class="plan"' + (planOpen ? " open" : "") + ">" +
    "<summary><span class=\"plan-title\">这一版的方向" +
      '<span class="num"> v' + esc(brief.version) + "</span></span>" +
    '<span class="plan-digest">' + esc(digest) + "</span>" +
    '<span class="proc-chev" aria-hidden="true"></span></summary>' +
    '<dl class="brief-dl">' + brief.fields.map((f) =>
      "<div><dt>" + esc(f.label) + "</dt><dd>" + f.value.map(esc).join("；") +
      (f.by === "ai" ? ' <span class="tag-ai">AI 暂定</span>' : "") + "</dd></div>").join("") +
    "</dl></details>";
  const d = host.querySelector("details");
  d.addEventListener("toggle", () => { planOpen = d.open; });
}

// ---------- 贴着输入框的问答卡 ----------

function renderAsk() {
  const host = $("stageAsk");
  if (!host) return;
  const html = stepperHTML();
  host.innerHTML = html;
  host.hidden = !html;
  wireAsk();
}

function wireAsk() {
  const step = $("stageAsk");
  if (step && !step.hidden) wireStepper(step);
}

function wireStepper(step) {
  const qs = (workCache && workCache.round && workCache.round.questions) || [];
  const qEl = step.querySelector(".t-q");
  const qid = qEl && qEl.dataset ? qEl.dataset.q : null;
  step.querySelectorAll(".t-q input[type=radio]").forEach((r) => r.addEventListener("change", () => {
    if (!qid) return;
    const prev = qAns.get(qid) || {};
    const firstTime = !isAnswered(qs.find((q) => q.id === qid));
    qAns.set(qid, { opt: r.value, other: prev.other || "" });
    if (r.value === "__other__") {
      // 选「其他」是要写字，别急着跳走；先重画再找输入框，不然焦点跟着旧 DOM 一起没了
      renderAsk();
      const box = step.querySelector(".q-other:not([hidden]) .q-other-input");
      if (box) box.focus({ preventScroll: true });
      return;
    }
    if (!firstTime) { renderAsk(); return; }
    advanceFrom(qs, qs.findIndex((q) => q.id === qid));
  }));
  const ta = step.querySelector(".q-other-input");
  if (ta) {
    ta.addEventListener("input", () => {
      if (!qid) return;
      const prev = qAns.get(qid) || { opt: "__other__" };
      qAns.set(qid, { opt: prev.opt, other: ta.value });
      // 字数一变就重画进度点；焦点在输入区里，重画会打断打字，所以只补底部那一行
      syncQFoot(qs);
    });
    // 写完回车就往后走，和点选项同一套节奏
    ta.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" || e.shiftKey || e.isComposing) return;
      e.preventDefault();
      if (!qid) return;
      const prev = qAns.get(qid) || { opt: "__other__" };
      qAns.set(qid, { opt: prev.opt, other: ta.value });
      advanceFrom(qs, qs.findIndex((q) => q.id === qid));
    });
  }
  const prevBtn = step.querySelector(".q-prev");
  if (prevBtn) prevBtn.onclick = () => { if (qStep > 0) { qStep -= 1; renderAsk(); focusStep("prev"); } };
  const nextBtn = step.querySelector(".q-next");
  if (nextBtn) nextBtn.onclick = () => {
    if (qStep < qs.length - 1) { qStep += 1; renderAsk(); focusStep("next"); }
  };
  const submit = $("askSubmit");
  if (submit) submit.onclick = () => onSend();
}

// 打字过程中只更新底部「已答 N / M」和进度点，整张卡重画会打断输入
function syncQFoot(qs) {
  const foot = document.querySelector("#stageAsk .q-foot");
  if (!foot || !qs.length) return;
  const done = qs.filter(isAnswered).length;
  const all = done === qs.length;
  const label = foot.querySelector(".q-done");
  if (label) { label.textContent = all ? "都答完了" : "已答 " + done + " / " + qs.length;
    label.classList.toggle("all", all); }
  const btn = $("askSubmit");
  if (btn) {
    btn.textContent = all ? "提交这 " + qs.length + " 个回答" : "提交回答";
    btn.className = "btn " + (all ? "primary " : "ghost ") + "small";
  }
  foot.parentElement.querySelectorAll(".q-dot").forEach((d, k) => {
    d.classList.toggle("done", isAnswered(qs[k]));
  });
}

// 切一题就是把整块 innerHTML 重写一遍，焦点掉回 body，键盘用户每切一题要从头 Tab。
// 重画完把焦点放回这一题：刚才那个切换钮还在就用它，否则落到当前题的第一个可选项上。
// preventScroll：这一层自己可滚，让浏览器顺手滚动会把画面顶一下（审查 U-05）
function focusStep(which) {
  const step = $("stageAsk");
  if (!step) return;
  const btn = step.querySelector(".q-" + which);
  if (btn && !btn.disabled) { btn.focus({ preventScroll: true }); return; }
  const openOther = step.querySelector(".q-other:not([hidden]) .q-other-input");
  const target = openOther || step.querySelector(".t-q input");
  if (target) target.focus({ preventScroll: true });
}

// 自动跳到下一题时落在题面本身，而不是刚被按下去的那个箭头
function focusOption() {
  const step = $("stageAsk");
  if (!step) return;
  const target = step.querySelector(".q-other:not([hidden]) .q-other-input") ||
    step.querySelector(".t-q input");
  if (target) target.focus({ preventScroll: true });
}

// ---------- 底部：状态、随状态出现的按钮、输入框 ----------

function answering() { return runState() === "waiting_user"; }
function confirming() { return runState() === "awaiting_confirmation"; }
function running() { return ["queued", "running", "retry_wait", "awaiting_runner"].includes(runState()); }
function halted() { return ["failed", "unknown_outcome", "cancelled"].includes(runState()); }

function renderDock() {
  renderPlan();
  renderAsk();
  const status = $("stageStatus");
  const run = workCache && workCache.run;
  // 阶段本身由对话流末尾那块「过程」在说，这里不再重复一遍；钉住的这一条只报需要看一眼的原因
  const why = run ? [run.reason_text, run.error_detail].filter(Boolean).join("：") : "";
  if (why) {
    status.hidden = false;
    status.innerHTML = '<span class="reason">' + esc(why) + "</span>";
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
    // 粗筛只认「来源压根没抓到正文」：加工中、整理失败时原文照旧在，不能算缺正文。
    // 到底能不能读由服务端判定，422 会带回具体条目
    const readable = it.coverage !== "metadata_only";
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
  // 幂等键跟着这件草稿走：超时后原样再按一次回车还是同一把键，服务端把已建成的那份
  // 还回来，不会建第二个作品、扣两份预算。换了材料或改了说法才是另一件事，换新键，
  // 否则服务端按「同键不同内容」挡回，用户反倒卡在这句上发不出去（审查 U-04）
  const sig = JSON.stringify([itemIds, instructions]);
  if (!draftKey || sig !== draftKeySig) { draftKey = crypto.randomUUID(); draftKeySig = sig; }
  try {
    const created = await api("/v1/shares", {
      method: "POST",
      body: { item_ids: itemIds, instructions },
      idempotencyKey: draftKey,
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
  resetRoundViews();
  shareLink = null;
  lastRevisionCount = 0;
  resetStageInput();
  await refreshWork();
  startPolling();
  $("stageInput").focus();
}

async function refreshWork() {
  if (!activeShareId) return;
  try {
    workCache = await api("/v1/shares/" + encodeURIComponent(activeShareId));
  } catch (e) {
    // 离开舞台之后才回来的那一次：这件作品没人在看，别在别的页面上弹一条无关提示
    if (stageIsOpen()) showErr(e);
    stopPolling();
    return;
  }
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
  const askLink = shareLink === null && !!workCache.share;
  renderThread();
  renderDock();
  // 作品详情只给 has_link，真链接要再问一次 /link：先画完再问，不为它多等一个来回；
  // 问过这一次就有了，轮询的刷新不会再问（审查 U-02）
  if (askLink) { await fetchShareLink(); renderThread(); }
}

async function fetchShareLink() {
  if (!workCache.share.has_link) { shareLink = false; return; }
  const id = activeShareId;
  let out = null;
  try { out = await api("/v1/shares/" + encodeURIComponent(id) + "/link"); }
  catch (e) { out = null; }
  if (activeShareId !== id) return;   // 取的这段时间里已经换了一件作品
  shareLink = (out && out.url) ? out.url : false;
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
      idempotencyKey: crypto.randomUUID(),
    });
    shareLink = out.url || false;   // 当场就拿得到，不用再问一次 /link；重画后锚点即真链接
    await refreshWork();
    copy(out.url);
    toast("分享链接已复制；只有拿到链接的人能看", { type: "ok" });
  } catch (e) { showErr(e); }
}

async function revokeWork() {
  try {
    await api(`/v1/shares/${encodeURIComponent(activeShareId)}/revoke`, { method: "POST", body: {} });
    toast("已撤销，旧链接不再可访问", { type: "ok" });
    shareLink = null;
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
  const round = workCache && workCache.round;
  if (!round) return [];
  const out = [];
  for (const q of round.questions || []) {
    const a = qAns.get(q.id);
    if (!a) continue;
    if (a.opt === "__other__") {
      const t = (a.other || "").trim();
      if (t) out.push({ question_id: q.id, option_ids: [], text: t });
    } else if (a.opt) {
      out.push({ question_id: q.id, option_ids: [a.opt], text: "" });
    }
  }
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
  // 没字就是胶囊，一打字长成圆角矩形：跟首页采集框同一套两态
  const box = $("stageBox");
  if (box) box.classList.toggle("open", !!t.value.trim());
}

function resetStageInput() {
  $("stageInput").value = "";
  autosize();
}

// 换一件作品就是另一套折叠态：上一件的展开记录不带过来
function resetRoundViews() {
  planOpen = false; planAuto = "";
  openRounds = new Set();
  procKey = ""; procOpen = null; procLive = null;
  procRun = ""; procFrom = 0; procMs = 0;
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
    if (answering()) {
      // 回答这一组：只勾了选项、没写补充也发得出去；两边都空才提示
      const answers = collectAnswers();
      if (!text && !answers.length) {
        toast("选一个选项，或点「其他」写两句；不想细说就直接写「你决定，按你的建议做」", { type: "warn" });
        el.focus();
        return;
      }
      await sendAnswer(text, answers);
    } else if (confirming() || revisions().length) {
      if (!text) { el.focus(); return; }
      if (confirming()) await sendAnswer(text, []);
      else await submitModify(text);
    } else {
      if (!text) { el.focus(); return; }
      toast("AI 还在读材料，等它问完再说", { type: "warn" });
    }
  } finally {
    sending = false;
  }
}

async function sendAnswer(free, answers) {
  const round = workCache && workCache.round;
  if (!round) return;
  try {
    await api(`/v1/shares/${encodeURIComponent(activeShareId)}/runs/${encodeURIComponent(activeRunId)}/messages`, {
      method: "POST",
      body: {
        expected_conversation_version: convCache ? convCache.conversation_version : 0,
        round_id: round.round_id, answers: answers || collectAnswers(), message: free,
      },
      idempotencyKey: crypto.randomUUID(),
    });
    await refreshWork();
    startPolling();
  } catch (e) { showErr(e); }
}

// 确认生成 / 让它自己定：和发送框同一把 sending 锁，连点第二次不会重复起一轮
async function startGeneration(modeName) {
  if (sending) return;
  sending = true;
  try {
    await api(`/v1/shares/${encodeURIComponent(activeShareId)}/runs/${encodeURIComponent(activeRunId)}/start`, {
      method: "POST",
      body: {
        expected_conversation_version: convCache ? convCache.conversation_version : null,
        expected_brief_version: workCache.brief ? workCache.brief.version : null,
        mode: modeName,
      },
      idempotencyKey: crypto.randomUUID(),
    });
    await refreshWork();
    startPolling();
  } catch (e) { showErr(e); }
  finally { sending = false; }
}

async function submitModify(instructions) {
  const list = revisions();
  if (!list.length) return;
  try {
    const out = await api(`/v1/shares/${encodeURIComponent(activeShareId)}/runs`, {
      method: "POST",
      body: { base_revision_id: list[list.length - 1].revision_id,
        instructions, expected_work_version: workCache.version },
      idempotencyKey: crypto.randomUUID(),
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
      { method: "POST", body: {}, idempotencyKey: crypto.randomUUID() });
    await openWork(activeShareId, out.run_id, true);
  } catch (e) { showErr(e); }
}

// ---------- 轮询：只在任务还在跑时继续 ----------

const ACTIVE = new Set(["queued", "running", "retry_wait", "awaiting_runner"]);

function startPolling() {
  stopPolling();
  const tick = async () => {
    pollTimer = 0;
    if (!activeShareId || !stageIsOpen()) return;
    await refreshWork();
    // 在飞的那一次回来时可能已经回了对话列表：舞台关了就不再续排，也别在列表页报错
    if (!stageIsOpen()) return;
    const state = workCache && workCache.run ? workCache.run.state : "idle";
    if (ACTIVE.has(state)) pollTimer = window.setTimeout(tick, 3000);
  };
  pollTimer = window.setTimeout(tick, 2500);
}

function stopPolling() {
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = 0; }
  stopProcClock();
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
    // 胶囊⇄矩形这一步会让文字重新折行，中途量的 scrollHeight 偏大，形变落定再量一次
    input.addEventListener("transitionend", (e) => {
      if (e.propertyName === "padding-right") autosize();
    });
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
  // Escape 一层层往外收：先收「完成」菜单，再退出舞台/对话记录，再退出多选。
  // 挂捕获阶段并给这次按键打个记号：账号菜单（app.js）和抽屉（item-list.js）都是
  // 同一次按键上的气泡监听，不读这个记号就会一次收掉两层（走查反馈：菜单开着按
  // Esc，抽屉跟着没了；审查 U-05 剩下的是菜单与舞台、详情页「更多操作」）
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || isModalOpen()) return;
    const menu = $("shareSelMenu");
    if (menu && !menu.hidden) { showSelMenu(false); e.kbEscTaken = true; return; }   // 先收菜单
    if (!$("shareStage").hidden || !$("sharesView").hidden) {                         // 再退舞台
      exitOverlay();
      e.kbEscTaken = true;
      return;
    }
    if (selectMode) { setSelectMode(false); e.kbEscTaken = true; }                    // 再收多选
  }, true);
}
