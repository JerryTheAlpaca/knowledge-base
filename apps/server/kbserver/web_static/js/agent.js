// agent.js — 与 AI 自由对话的面板（docs/27：前端只跟 A 机同源接口说话，零 CORS）
//
// 顶栏「AI 对话」进的是这一层：真正随便说的 agent，不是预制问卷。
// 会话列表由面板自己带着（新建 / 切换 / 重命名 / 关闭），点按钮直接落回最近一次会话，
// 中间不再多一个列表页。消息流先读 history 把已有的画出来，再订阅 SSE 收增量，
// 两边都按 seq 走：seq 由编排服务单调分配，断线重连既不重复也不漏。
// 过程块只画服务端真发出来的事件（status / tool_call / tool_result）：
// 不显示模型隐藏推理、不编造进度百分比、不猜「还要几秒」（docs/20 §3.5）。

import { $, api, esc, fmtShort, toast, isModalOpen, confirmModal, promptModal } from "./api.js";
import { setListPollPaused } from "./item-list.js";

const FIRST_KEY = "kb.agent.firstnotice.v1";
const LAST_KEY = "kb.agent.lastsession.v1";
const PAGE = 200;                        // /history 一页条数（服务端上限 500）
const MAX_PAGES = 10;                    // 一次最多回溯 2000 条，再多只留最近的
const RECONNECT_MS = [1500, 3000, 8000]; // 断线重连节奏，之后停在 8 秒一次
const STALL_MS = 30000;                  // 连着却等不到新事件时，不把输入框一直锁死
// 这一轮还没完的信号：末尾停在这几种事件上，说明服务器那边还在做事
const KIND_OPEN = new Set(["user_message", "status", "tool_call", "tool_result"]);
// 内存准入排队由容器的 status 事件报出来，界面上就说「服务器忙，已排队」
const BUSY_TEXT = /(排队|等待资源|内存不足|服务器忙)/;
const BUSY_STATE = /(queued|waiting_resources|admission)/;

let go = (url, state) => {
  // 和 shares.js 同一套：state 里带 view，退出时靠它判断这条历史是我们压进去的
  try { history.pushState(state || {}, "", url); } catch (e) { /* 忽略 */ }
};

let sessions = [];
let degraded = false;   // 列表来自 A 机镜像（容器接不上），界面要如实标注
let activeId = null;
let events = [];        // 已渲染的事件，按 seq 升序：history + SSE 合流后去重
let lastSeq = 0;
let loading = false;    // 会话列表 / 历史在飞
let awaiting = false;   // 刚 POST 出去、SSE 还没回首个事件
let stalled = false;    // 等不到新事件：把输入框交还给用户，别锁死
let sending = false;
let notice = "";        // 面板内的失败提示：容器不可达、接口报错都写在这里
let firstNotice = lsGet(FIRST_KEY) !== "1";
let menuOpen = false;
let fold = new Map();   // 过程块（按首条 seq）→ 用户手动开合的状态
let sseAbort = null;
let sseTimer = 0;
let sseLive = false;
let sseFails = 0;
let lastEventAt = 0;
let openedAt = 0;
let watchTimer = 0;
let lastSig = "";
let lastCount = 0;

function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* 忽略 */ } }
function lsDel(k) { try { localStorage.removeItem(k); } catch (e) { /* 忽略 */ } }

// ---------- 事件负载读法：至少有一个 text / message 可读，工具带 name ----------

function payloadText(p) {
  if (!p || typeof p !== "object") return "";
  const v = p.text != null ? p.text : (p.message != null ? p.message
    : (p.content != null ? p.content : ""));
  if (typeof v === "string") return v;
  try { return JSON.stringify(v); } catch (e) { return String(v); }
}

function payloadName(p) { return p && p.name ? String(p.name) : ""; }

function clip(s, n) { return s.length > n ? s.slice(0, n) + "…" : s; }

// ---------- 开合状态 ----------

function agentIsOpen() { return !!$("agentView") && !$("agentView").hidden; }

function syncChrome() {
  document.body.classList.toggle("agent-open", agentIsOpen());
  // 列表被盖住了就停掉它的轮询，面板自己走 SSE
  setListPollPaused(agentIsOpen());
}

export function isAgentOpen() { return agentIsOpen(); }

export function hideAgentView() {
  if (!$("agentView")) return;
  $("agentView").hidden = true;
  closeMenu();
  stopEvents();
  stopWatch();
  // 离开的这一路把「正在等」的猜测清掉：回来时按历史末尾重新判断
  awaiting = false;
  stalled = false;
  notice = "";
  syncChrome();
}

function showAgent() {
  $("agentView").hidden = false;
  syncChrome();
}

// 顶栏按钮走的这条路：只换 URL，渲染统一交给 app.js 的 route()
export function openAgentPanel() {
  go("/inbox?view=agent", { view: "agent" });
}

function exitAgent() {
  // 进来时压了一条历史：back 回到进入前的位置（同分享舞台、同设置页）
  if (history.state && history.state.view === "agent") { history.back(); return; }
  go("/inbox");
}

// ---------- 会话列表 ----------

function findSession(id) { return sessions.find((s) => s.session_id === id) || null; }

// 列表按「最近更新」排；容器给的顺序和镜像那边不完全一致，这里统一一次
function sortedSessions() {
  return sessions.slice().sort((a, b) =>
    String(b.updated_at || "").localeCompare(String(a.updated_at || "")));
}

function pickLastSession(explicit) {
  if (explicit) return explicit;
  if (activeId && findSession(activeId)) return activeId;   // 面板刚收起来又点开：回到那一段
  const remembered = lsGet(LAST_KEY);
  if (remembered && findSession(remembered)) return remembered;
  const list = sortedSessions();
  return list.length ? list[0].session_id : null;
}

async function loadSessions() {
  try {
    const data = await api("/v1/agent/sessions");
    sessions = Array.isArray(data.sessions) ? data.sessions : [];
    degraded = !!data.degraded;
  } catch (e) {
    // 连镜像都没有：面板照样开，只是列表空着并说明原因，不影响收件箱其余部分
    sessions = [];
    degraded = false;
    setNotice(e);
  }
  render();
}

function setNotice(e) {
  notice = (e && e.userMessage) || "刚才没有完成，请再试一次。";
  render();
}

async function createSession(title) {
  try {
    const out = await api("/v1/agent/sessions", { method: "POST", body: { title: title || "" } });
    if (!out || !out.session_id) return null;
    // 先把手里这份摆进列表（新建的就是最近一次），再让服务端把列表校准一遍
    sessions = [Object.assign({ updated_at: new Date().toISOString() }, out)]
      .concat(sessions.filter((s) => s.session_id !== out.session_id));
    render();
    return out.session_id;
  } catch (e) {
    setNotice(e);
    return null;
  }
}

async function createAndOpen() {
  const id = await createSession("");
  if (!id) return;
  await openSession(id, "push");
  loadSessions();   // 标题/时间由服务端定，回来对一次
}

async function renameSession(id) {
  const cur = findSession(id);
  const t = await promptModal({
    title: "给这段对话起个名字",
    value: (cur && cur.title) || "",
    placeholder: "例：把这三篇的差异整理成笔记",
    confirmLabel: "保存",
  });
  if (t == null || t === (cur && cur.title)) return;
  try {
    await api("/v1/agent/sessions/" + encodeURIComponent(id), { method: "PUT", body: { title: t } });
    if (cur) cur.title = t;
    render();
    toast("已改名", { type: "ok" });
  } catch (e) { setNotice(e); }
}

async function closeSession(id) {
  const ok = await confirmModal({
    title: "关闭这个会话？",
    body: "对话记录还留在服务器上，只是不再出现在列表里。",
    confirmLabel: "关闭会话",
    danger: true,
  });
  if (!ok) return;
  try {
    await api("/v1/agent/sessions/" + encodeURIComponent(id), { method: "DELETE" });
  } catch (e) { setNotice(e); return; }
  sessions = sessions.filter((s) => s.session_id !== id);
  if (activeId === id) {
    activeId = null;
    lsDel(LAST_KEY);
    events = []; lastSeq = 0; awaiting = false; stalled = false;
    stopEvents();
    const next = pickLastSession(null);
    if (next) { await openSession(next, "replace"); return; }
  }
  render();
  toast("已关闭", { type: "ok" });
}

// ---------- 进某一个会话：先历史，再 SSE ----------

export async function openAgentView(sessionId) {
  if (!$("agentView")) return;
  if (agentIsOpen() && sessionId && sessionId === activeId) return;   // 已经在看着这一段
  showAgent();
  loading = true;
  render();
  await loadSessions();
  const target = pickLastSession(sessionId);
  if (!target) {
    // 一段会话都还没有：不就地新建（点开一次面板不该在服务器上起一个进程），
    // 停在空面板上，等用户说第一句话时才真的建
    activeId = null;
    events = []; lastSeq = 0;
    loading = false;
    render();
    $("agentInput").focus();
    return;
  }
  await openSession(target, sessionId ? "keep" : "replace");
}

async function openSession(id, urlMode) {
  activeId = id;
  lsSet(LAST_KEY, id);
  // 先把上一段那条流掐掉：晚一步，旧会话的事件就会串进新面板的 events 里
  stopEvents();
  events = []; lastSeq = 0; awaiting = false; stalled = false; notice = "";
  fold = new Map();
  loading = true;
  if (urlMode === "push") go("/inbox?view=agent&session=" + encodeURIComponent(id), { view: "agent" });
  // "replace"：路由进来时 URL 还没有 session 参数，补上但不多压一条历史；
  // "keep"：调用方（route）已经把 URL 摆好了，这里不再动历史
  else if (urlMode === "replace") replaceUrl(id);
  showAgent();
  render();
  await loadHistory(id);
  if (activeId !== id) return;      // 历史在飞的这段时间里已经换了会话
  loading = false;
  connectEvents(id);
  startWatch();
  render();
  $("agentInput").focus({ preventScroll: true });
}

function replaceUrl(id) {
  const params = new URLSearchParams(location.search);
  params.set("view", "agent");
  params.set("session", id);
  const url = "/inbox?" + params.toString();
  if (location.pathname + location.search === url) return;
  try { history.replaceState({ view: "agent" }, "", url); } catch (e) { /* 忽略 */ }
}

async function loadHistory(id) {
  const acc = [];
  let after = 0;
  let pages = 0;
  while (pages < MAX_PAGES) {
    let data;
    try {
      data = await api(`/v1/agent/sessions/${encodeURIComponent(id)}/history?after_seq=${after}&limit=${PAGE}`);
    } catch (e) {
      if (activeId === id) setNotice(e);
      break;
    }
    const list = Array.isArray(data.events) ? data.events : [];
    acc.push(...list);
    pages += 1;
    const maxSeq = list.reduce((m, ev) => (typeof ev.seq === "number" && ev.seq > m ? ev.seq : m), after);
    if (list.length < PAGE || maxSeq === after) break;   // 拿完了，或者服务端没往前走
    after = maxSeq;
  }
  if (activeId !== id) return;
  // 按 seq 合流：SSE 可能比这一页历史更早送到新事件，直接覆盖会把已经画出来的
  // 那几条抹掉、还会让 lastSeq 倒退，重连时就补投成第二遍
  const bySeq = new Map();
  for (const ev of events) bySeq.set(ev.seq, ev);
  for (const ev of acc) bySeq.set(ev.seq, ev);
  events = Array.from(bySeq.values()).sort((a, b) => a.seq - b.seq);
  lastSeq = events.length ? events[events.length - 1].seq : lastSeq;
  lastEventAt = Date.now();
  render();
}

// ---------- SSE：按 seq 续传，断了自己接上 ----------

function stopEvents() {
  if (sseTimer) { clearTimeout(sseTimer); sseTimer = 0; }
  if (sseAbort) { try { sseAbort.abort(); } catch (e) { /* 忽略 */ } sseAbort = null; }
  sseLive = false;
}

function scheduleReconnect(id, delay) {
  if (sseTimer) clearTimeout(sseTimer);
  sseTimer = window.setTimeout(() => { sseTimer = 0; connectEvents(id); }, delay);
}

function reconnectNow() {
  if (!activeId) return;
  stopEvents();
  sseFails = 0;
  connectEvents(activeId);
}

function connectEvents(id) {
  stopEvents();
  const mine = new AbortController();
  sseAbort = mine;
  sseLive = false;
  openedAt = Date.now();
  (async () => {
    try {
      const res = await fetch(`/v1/agent/sessions/${encodeURIComponent(id)}/events?after=${lastSeq}`,
        { credentials: "same-origin", headers: { Accept: "text/event-stream" }, signal: mine.signal });
      if (!res.ok || !res.body) throw new Error("stream");
      sseLive = true;
      if (sseFails) { sseFails = 0; notice = ""; }
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const r = await reader.read();
        if (r.done) break;
        buf += dec.decode(r.value, { stream: true });
        let at;
        while ((at = buf.indexOf("\n\n")) >= 0) {
          const frame = buf.slice(0, at);
          buf = buf.slice(at + 2);
          readFrame(frame, id);
        }
      }
    } catch (e) {
      if (mine.signal.aborted) return;   // 换会话 / 关面板：正常收尾，不报错
    }
    if (mine !== sseAbort) return;       // 已经另起了一条流
    sseLive = false;
    // 转发那条流本身有超时（A 机 600 秒），跑完一段自己断了就马上接上；
    // 连续失败才说「暂时不可用」，别一次抖动就吓到人
    sseFails += 1;
    if (Date.now() - openedAt > 5000) sseFails = 1;
    if (sseFails >= 2) {
      notice = "对话服务暂时不可用，收件箱其他功能不受影响；正在自动重试。";
      render();
    }
    const wait = RECONNECT_MS[Math.min(sseFails - 1, RECONNECT_MS.length - 1)];
    scheduleReconnect(id, sseFails >= 2 ? wait : 1200);
  })();
}

function readFrame(raw, id) {
  let data = "";
  for (const line of raw.split(/\r?\n/)) {
    if (!line || line.charAt(0) === ":") continue;   // 心跳注释行
    if (line.slice(0, 5) === "data:") data += (data ? "\n" : "") + line.slice(5).replace(/^ /, "");
  }
  if (!data.trim()) return;
  let ev;
  try { ev = JSON.parse(data); } catch (e) { return; }   // 不是我们这一路的帧，丢掉
  ingest(ev, id);
}

function ingest(ev, id) {
  if (!ev || typeof ev.seq !== "number") return;
  if (id && id !== activeId) return;          // 换会话那一瞬还在缓冲里的旧流事件
  if (ev.seq <= lastSeq) return;          // 重复投递：seq 就是幂等键，不重画第二遍
  lastSeq = ev.seq;
  events.push(ev);
  awaiting = false;
  stalled = false;
  lastEventAt = Date.now();
  // 断线重连后服务端会成串补投，一帧里画一次就够，不用每条都重排一遍对话流
  scheduleRender();
}

let renderQueued = false;
function scheduleRender() {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => { renderQueued = false; render(); });
}

// 一次改动三处观感一起对：列表、底部、对话流读的都是同一份状态
function render() {
  renderThread();
  renderDock();
  renderMenu();
}

// 连着却半天没有新事件：把输入框还给人家，别让一句话把面板锁死
function startWatch() {
  stopWatch();
  watchTimer = window.setInterval(() => {
    if (!agentIsOpen()) { stopWatch(); return; }
    if (!turnRunning() && !awaiting) return;
    if (Date.now() - lastEventAt < STALL_MS) return;
    if (!stalled) { stalled = true; awaiting = false; render(); }
  }, 5000);
}

function stopWatch() {
  if (watchTimer) { clearInterval(watchTimer); watchTimer = 0; }
}

// ---------- 状态判断 ----------

function turnRunning() {
  if (stalled) return false;
  if (awaiting) return true;
  const last = events[events.length - 1];
  return !!(last && KIND_OPEN.has(last.kind));
}

function turnBusy() {
  for (let i = events.length - 1; i >= 0; i--) {
    const ev = events[i];
    if (ev.kind === "turn_end") break;
    if (ev.kind !== "status") continue;
    const p = ev.payload || {};
    if (BUSY_TEXT.test(payloadText(p))) return true;
    if (BUSY_STATE.test([p.state, p.stage, p.name].filter(Boolean).join(" "))) return true;
  }
  return false;
}

// ---------- 对话流 ----------

function firstHTML() {
  return '<div class="t-card agent-first"><h4>第一次用，先说一句</h4>' +
    '<p class="agent-first-p">这里的 AI 会自己去翻你收进来的材料：为了回答一个问题，' +
    "它一次可能读几十条原文，比你一条条点着看得多得多。</p>" +
    '<p class="agent-first-p">如果你在设置里填的模型 Key 指向第三方中转站，这些原文会经由中转站' +
    "送到那边的模型。不想让内容出去，就先把 Key 换成官方地址再用。</p>" +
    '<div class="pv-row"><button class="btn primary small" id="agentFirstOk">我知道了</button></div></div>';
}

function procRowHTML(ev) {
  const p = ev.payload || {};
  const text = clip(payloadText(p).trim(), 240);
  const name = payloadName(p);
  if (ev.kind === "tool_call" || ev.kind === "tool_result") {
    const label = ev.kind === "tool_call" ? "调用工具" : "工具返回";
    return '<span class="agent-tool">' + label + "</span>" +
      (name ? '<span class="agent-toolname">' + esc(name) + "</span>" : "") +
      (text ? '<span class="agent-tooltext">' + esc(text) + "</span>" : "");
  }
  // status：原样给出真实阶段名，不改写、不补一个没发生的阶段
  return esc(text || "状态更新");
}

function procHTML(items, live) {
  const key = String(items[0].seq);
  const open = fold.has(key) ? fold.get(key) : live;
  // 跑动时那一行只报真实的阶段名（最后一条 status）：把工具返回的长正文当标题
  // 会和下面那行重复，而且它不是「正在做哪一步」
  let stage = "";
  for (let i = items.length - 1; i >= 0; i--) {
    if (items[i].kind === "status") { stage = payloadText(items[i].payload || {}).trim(); break; }
  }
  const head = live ? (clip(stage, 40) || "正在处理") : "处理过程";
  const rows = items.map((ev, i) => {
    const now = live && i === items.length - 1;
    return '<li class="' + (now ? "now" : "ok") + '"><span class="proc-mark" aria-hidden="true"></span>' +
      '<span class="agent-proc-text">' + procRowHTML(ev) + "</span></li>";
  }).join("");
  // data-rendered 记下这一笔画出来是开还是合：details 插进 DOM 时浏览器会补发一次
  // toggle，把它当成用户操作记进 fold，跑完那一轮就折不回去了
  return '<details class="t-card t-proc"' + (open ? " open" : "") +
    ' data-proc="' + key + '" data-rendered="' + (open ? "1" : "0") + '">' +
    '<summary><span class="proc-sum"' + (live ? ' data-live="1"' : "") + ">" + esc(head) + "</span>" +
    '<span class="proc-cost">' + items.length + " 条</span>" +
    '<span class="spacer"></span><span class="proc-chev" aria-hidden="true"></span></summary>' +
    '<ul class="proc-steps"' + (live ? ' data-live="1"' : "") + ">" + rows + "</ul></details>";
}

function threadHTML() {
  const out = [];
  if (firstNotice) out.push(firstHTML());
  if (loading && !events.length) {
    out.push('<div class="t-card"><p class="muted" style="margin:0">正在加载…</p></div>');
    return out.join("");
  }
  if (!events.length) {
    out.push('<div class="t-card"><p class="muted" style="margin:0">' +
      (activeId ? "这一段还是空的。" : "还没有会话。说第一句话就会开一段，AI 会自己去读你收进来的材料。") +
      "</p></div>");
  }
  let buf = [];
  const flush = (live) => { if (buf.length) { out.push(procHTML(buf, live)); buf = []; } };
  for (const ev of events) {
    if (ev.kind === "user_message" || ev.kind === "assistant_message") {
      flush(false);
      const t = payloadText(ev.payload || {}).trim();
      if (!t) continue;
      out.push('<div class="t-msg ' + (ev.kind === "user_message" ? "me" : "ai") + '">' +
        esc(t).replace(/\n/g, "<br>") + "</div>");
      continue;
    }
    if (ev.kind === "error") {
      flush(false);
      const t = payloadText(ev.payload || {}).trim() || "这一轮停住了。";
      out.push('<div class="t-msg ai agent-err">' + esc(t).replace(/\n/g, "<br>") + "</div>");
      continue;
    }
    if (ev.kind === "turn_end") { flush(false); continue; }
    buf.push(ev);
  }
  flush(turnRunning());
  return out.join("");
}

function threadSig() {
  return JSON.stringify([activeId, events.length, lastSeq, loading, firstNotice,
    turnRunning(), turnBusy(), stalled, !!notice, degraded, sessions.length]);
}

function renderThread() {
  const host = $("agentThread");
  if (!host) return;
  const sig = threadSig();
  if (sig === lastSig) return;
  const wasNearBottom = host.scrollHeight - host.scrollTop - host.clientHeight < 90;
  lastSig = sig;
  host.innerHTML = threadHTML();
  const count = events.length;
  if (wasNearBottom || count > lastCount) host.scrollTop = host.scrollHeight;
  lastCount = count;
  host.querySelectorAll("[data-proc]").forEach((d) => {
    d.addEventListener("toggle", () => {
      // 只记真的人手开合：details 刚插进 DOM 时浏览器补发的那一次 toggle 不算
      if (d.dataset.rendered === (d.open ? "1" : "0")) return;
      fold.set(d.dataset.proc, d.open);
    });
  });
  const ok = $("agentFirstOk");
  if (ok) ok.addEventListener("click", () => { firstNotice = false; lsSet(FIRST_KEY, "1"); render(); });
}

// ---------- 会话切换条 ----------

function renderMenu() {
  const host = $("agentSessList");
  if (!host) return;
  if (!sessions.length) {
    host.innerHTML = '<li class="agent-sess-empty muted">还没有会话。</li>';
    return;
  }
  host.innerHTML = sortedSessions().map((s) => {
    const id = String(s.session_id);
    return '<li class="agent-sess"' + (id === activeId ? ' data-now="1"' : "") + ">" +
      '<button class="agent-sess-open" data-open="' + esc(id) + '">' +
      '<span class="agent-sess-title">' + esc(s.title || "未命名会话") + "</span>" +
      '<span class="agent-sess-meta">' + esc(fmtShort(s.updated_at)) + "</span></button>" +
      '<span class="agent-sess-acts">' +
      '<button class="icon ghost agent-sess-act" data-rename="' + esc(id) +
      '" aria-label="重命名这段会话" title="重命名">' +
      '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 20h4L20 8l-4-4L4 16z"/><path d="M14 6l4 4"/></svg></button>' +
      '<button class="icon ghost agent-sess-act" data-drop="' + esc(id) +
      '" aria-label="关闭这段会话" title="关闭">' +
      '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg></button>' +
      "</span></li>";
  }).join("");
}

function onMenuClick(e) {
  const open = e.target.closest("[data-open]");
  if (open) { closeMenu(); openSession(open.dataset.open, "push"); loadSessions(); return; }
  const ren = e.target.closest("[data-rename]");
  if (ren) { renameSession(ren.dataset.rename); return; }
  const del = e.target.closest("[data-drop]");
  if (del) { closeMenu(); closeSession(del.dataset.drop); }
}

function toggleMenu() { if (menuOpen) closeMenu(); else openMenu(); }

function openMenu() {
  menuOpen = true;
  $("agentSessions").hidden = false;
  $("agentSwitch").setAttribute("aria-expanded", "true");
  renderMenu();
}

function closeMenu() {
  if (!menuOpen) return;
  menuOpen = false;
  const m = $("agentSessions");
  if (m) m.hidden = true;
  const b = $("agentSwitch");
  if (b) b.setAttribute("aria-expanded", "false");
}

// ---------- 底部：状态、提示、输入框 ----------

function renderDock() {
  const cur = activeId ? findSession(activeId) : null;
  const label = $("agentSwitchLabel");
  if (label) label.textContent = cur ? (cur.title || "未命名会话")
    : (sessions.length ? "选择会话" : "新会话");
  const run = turnRunning();
  const input = $("agentInput");
  const send = $("agentSend");
  input.disabled = run || sending || loading;
  send.disabled = run || sending || loading;
  input.placeholder = run ? "AI 正在处理这一轮…" : "想问点什么……";
  $("agentHint").textContent = run
    ? (turnBusy() ? "服务器忙，已排队，排到了会自动继续。" : "正在处理这一轮，好了会在这里继续。") : "";
  const notes = [];
  if (notice) {
    notes.push('<span class="reason">' + esc(notice) + "</span>" +
      '<button class="btn ghost small" id="agentRetry">重试</button>');
  }
  if (stalled && !notice) {
    notes.push('<span class="agent-note">这一轮的状态没有再传回来，可能还在服务器上跑。想接着说就直接写。</span>');
  }
  if (degraded && !notice) {
    notes.push('<span class="agent-note">对话服务现在接不上，上面列的是服务器上存过的会话。</span>');
  }
  const host = $("agentStatus");
  host.hidden = !notes.length;
  host.innerHTML = notes.join("");
  const retry = $("agentRetry");
  if (retry) retry.onclick = () => { notice = ""; loadSessions(); if (activeId) reconnectNow(); };
}

function autosize() {
  const t = $("agentInput");
  t.style.height = "";
  t.style.height = Math.min(t.scrollHeight, 220) + "px";
  // 没字就是胶囊，一打字长成圆角矩形：与首页采集框、分享舞台同一套两态
  const box = $("agentBox");
  if (box) box.classList.toggle("open", !!t.value.trim());
}

// ---------- 发送 ----------

async function onSend() {
  if (sending) return;
  const el = $("agentInput");
  const text = (el.value || "").trim();
  if (!text) { el.focus(); return; }
  sending = true;
  render();
  try {
    let id = activeId;
    if (!id) {
      id = await createSession("");   // 会话是发第一句话时才建的
      if (!id) { sending = false; render(); return; }
    }
    el.value = "";
    autosize();
    await api(`/v1/agent/sessions/${encodeURIComponent(id)}/messages`, { method: "POST", body: { text } });
    // 这一句由服务端作为 user_message 事件送回来，这里不自己画一遍（seq 会撞）
    awaiting = true;
    stalled = false;
    notice = "";
    lastEventAt = Date.now();
    if (!sseAbort) connectEvents(id);
  } catch (e) {
    // 发不出去的这句话还在框里：上面只在成功之后才清空
    el.value = text;
    autosize();
    setNotice(e);
  } finally {
    sending = false;
  }
  render();
}

// ---------- 入口 ----------

export function initAgent(navigator) {
  if (navigator) go = navigator;
  if (!$("agentView")) return;
  $("agentClose").addEventListener("click", exitAgent);
  $("agentNew").addEventListener("click", createAndOpen);
  $("agentNewTop").addEventListener("click", () => { closeMenu(); createAndOpen(); });
  $("agentSwitch").addEventListener("click", (e) => { e.stopPropagation(); toggleMenu(); });
  // 点面板别处就收起会话列表（同多选「完成」菜单的做法）
  document.addEventListener("click", (e) => {
    if (menuOpen && !e.target.closest(".agent-switchwrap")) closeMenu();
  });
  $("agentSessList").addEventListener("click", onMenuClick);
  const input = $("agentInput");
  input.addEventListener("input", autosize);
  // 胶囊⇄矩形这一步会让文字重新折行，中途量的 scrollHeight 偏大，形变落定再量一次
  input.addEventListener("transitionend", (e) => {
    if (e.propertyName === "padding-right") autosize();
  });
  // 回车发送、Shift+回车换行：和分享舞台那副输入框一致
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      if (!input.disabled) onSend();
    }
  });
  $("agentSend").addEventListener("click", onSend);
  // Escape 一层层往外收：先收盘里的会话列表，再退出面板。挂捕获阶段并给这次按键
  // 打记号，账号菜单（app.js）与抽屉（item-list.js）读同一个记号，不会一次收掉两层
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || isModalOpen() || !agentIsOpen()) return;
    if (menuOpen) { closeMenu(); e.kbEscTaken = true; return; }
    exitAgent();
    e.kbEscTaken = true;
  }, true);
  renderDock();
}
