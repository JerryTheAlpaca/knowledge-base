// item-list.js — 首页条目流（docs/17 §4.5、§6.4）
//
// 三组归类：需要你处理 / 待发布 / 已完成，组内按 created_at 倒序（最新在最上）。
// 归哪一组只看服务端 workflow.overall_state，本模块不推断业务状态。
// 刷新按 item_id 做 DOM diff，只更新变化行，保护滚动位置（§6.4）。

import { $, api, esc, fmtShort, showErr, isModalOpen } from "./api.js";
import { listRowAux } from "./workflow.js";
import { openDetail } from "./item-detail.js";

const FETCH_LIMIT = 50;
let refreshing = false;
let searchTimer = null;
let searchOpen = false;

// —— 顶部抽屉（金蔷薇布局）：圆弧拉手拖着整页下拉，点外部或 Esc 收起 ——
// 动画模型：面板钉在终位，clip-path 可视区恒为 [顶栏下沿, 拉手上沿]；拉手用
// transform 下移、充当页面前缘的拉环，顶端同时露出面板自带的收起钮（新圆弧）。
// 拖拽必须 1:1 跟手且拉手与页面边缘同帧同位，CSS transition 做不到，统一 rAF 驱动。
const drawerHeader = document.querySelector("header");
function drawerRestTop() {
  return drawerHeader ? drawerHeader.getBoundingClientRect().bottom : 56;
}
function drawerMaxPull() {
  return Math.max(0, window.innerHeight - drawerRestTop());
}

let pull = 0;        // 当前拉出量 px = 拉手相对静止位的下移量
let pullRaf = 0;
const reduceMotion = window.matchMedia
  && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function applyPull(px) {
  pull = px;
  const top = drawerRestTop();
  const max = Math.max(0, window.innerHeight - top);
  const handle = $("drawerHandle");
  const panel = $("drawerPanel");
  const close = $("drawerClose");
  handle.style.transform = px > 0.5 ? "translateY(" + px + "px)" : "";
  const bottom = Math.max(0, window.innerHeight - top - px);
  panel.style.clipPath = "inset(" + top + "px 0 " + bottom + "px 0)";
  panel.style.visibility = px > 0.5 ? "visible" : "";
  // 收起钮不全程占位：拉到最后 10% 才渐显，收起时随之隐去；未显现时不可点
  const show = max > 0 && px / max > 0.9 ? Math.min(1, (px / max - 0.9) / 0.1) : 0;
  close.style.opacity = show;
  close.style.pointerEvents = show > 0.5 ? "auto" : "none";
}

function animatePull(target) {
  if (pullRaf) { cancelAnimationFrame(pullRaf); pullRaf = 0; }
  if (reduceMotion || Math.abs(target - pull) < 1) { applyPull(target); return; }
  const from = pull;
  const start = performance.now();
  const dur = 420;
  const ease = (t) => 1 - Math.pow(1 - t, 3);  // easeOutCubic
  const step = (now) => {
    const t = Math.min(1, (now - start) / dur);
    applyPull(from + (target - from) * ease(t));
    pullRaf = t < 1 ? requestAnimationFrame(step) : 0;
  };
  pullRaf = requestAnimationFrame(step);
}

export function isDrawerOpen() { return $("listWrap").classList.contains("open"); }

let drawerClosedAt = 0;
export function openDrawer(instant) {
  $("listWrap").classList.add("open");
  $("drawerHandle").setAttribute("aria-expanded", "true");
  if (instant) {  // 从设置返回条目抽屉：直接定格在展开位，不重播下拉动画
    if (pullRaf) { cancelAnimationFrame(pullRaf); pullRaf = 0; }
    applyPull(drawerMaxPull());
  } else {
    animatePull(drawerMaxPull());
  }
}
export function closeDrawer() {
  $("listWrap").classList.remove("open");
  $("drawerHandle").setAttribute("aria-expanded", "false");
  drawerClosedAt = Date.now();
  animatePull(0);
}

// 滚动显现（走查反馈）：卡片进入视口时淡入上移；已显现的行不再重复动画
const revealIO = ("IntersectionObserver" in window)
  ? new IntersectionObserver((entries) => {
      for (const en of entries) {
        if (!en.isIntersecting) continue;
        en.target.classList.remove("pre");
        en.target.classList.add("enter");
        revealIO.unobserve(en.target);
      }
    }, { rootMargin: "60px 0px" })
  : null;

function rowJSON(it) {
  const wf = it.workflow || {};
  return JSON.stringify([
    it.title, it.original_url, it.source_label, it.created_at,
    wf.message || "", wf.overall_state || "", wf.progress_percent ?? null,
    wf.delivery ? wf.delivery.status : "",
  ]);
}

function rowInner(it) {
  const display = it.title || (it.original_url || "").replace(/^https?:\/\/(www\.)?/, "").slice(0, 60) || "文字 / 文件采集";
  const aux = listRowAux(it.workflow, it);
  return '<div class="item-main"><span class="item-title">' + esc(display) + "</span>" +
    '<span class="item-time num" title="' + esc(new Date(it.created_at).toLocaleString("zh-CN", { hour12: false })) + '">' +
    esc(fmtShort(it.created_at)) + "</span></div>" +
    '<div class="item-aux">' + aux + "</div>";
}

function rowEl(it) {
  const li = document.createElement("li");
  li.className = "item pre";
  li.setAttribute("role", "button");
  li.tabIndex = 0;
  li.dataset.id = it.item_id;
  li.dataset.json = rowJSON(it);
  li.innerHTML = rowInner(it);
  if (revealIO) revealIO.observe(li);
  else li.classList.remove("pre");
  return li;
}

function updateRow(li, it) {
  li.dataset.json = rowJSON(it);
  li.innerHTML = rowInner(it);
}

// 时间倒序 diff：新条目插到正确位置，变化行才重写，未变化的 DOM 原样保留
function fillList(ul, items) {
  const keep = new Set(items.map((it) => it.item_id));
  for (const li of Array.from(ul.children)) {
    if (!keep.has(li.dataset.id)) {
      if (revealIO) revealIO.unobserve(li);
      li.remove();
    }
  }
  let anchor = ul.firstChild;
  for (const it of items) {
    let li = ul.querySelector('li[data-id="' + CSS.escape(it.item_id) + '"]');
    if (!li) li = rowEl(it);
    else if (li.dataset.json !== rowJSON(it)) updateRow(li, it);
    ul.insertBefore(li, anchor);  // 已在正确位置时是无操作
    anchor = li.nextSibling;
  }
}

function searchQuery() {
  return searchOpen ? $("searchBox").value.trim() : "";
}

// —— 三组归类：只映射服务端 overall_state，空组不占位 ——
const GROUPS = [
  { key: "attention", group: "groupAttention", list: "listAttention", count: "countAttention" },
  { key: "pending", group: "groupPending", list: "listPending", count: "countPending" },
  { key: "done", group: "groupDone", list: "listDone", count: "countDone" },
];
const DONE_SHOWN = 3;  // 已完成默认只露最新三条，其余折叠

function bucketOf(it) {
  const st = (it.workflow || {}).overall_state;
  if (st === "attention" || st === "failed") return "attention";
  if (st === "published") return "done";
  return "pending";
}

function updateDoneToggle(total) {
  const list = $("listDone");
  const btn = $("doneToggle");
  const extra = total - DONE_SHOWN;
  if (extra <= 0) list.classList.add("collapsed");  // 没什么可展开的就回到折叠位
  btn.hidden = extra <= 0;
  if (!btn.hidden) {
    btn.textContent = list.classList.contains("collapsed") ? "展开其余 " + extra + " 条" : "收起";
  }
}

function renderGroups(items) {
  const buckets = { attention: [], pending: [], done: [] };
  for (const it of items) buckets[bucketOf(it)].push(it);
  for (const g of GROUPS) {
    fillList($(g.list), buckets[g.key]);
    $(g.group).hidden = buckets[g.key].length === 0;
    $(g.count).textContent = buckets[g.key].length ? String(buckets[g.key].length) : "";
  }
  updateDoneToggle(buckets.done.length);
}

function updateEmptyState(items) {
  const searching = !!searchQuery();
  const empty = items.length === 0;
  $("emptyState").hidden = !empty;
  if (empty) {
    const title = $("emptyState").querySelector(".empty-title");
    const p = $("emptyState").querySelector("p");
    if (searching) {
      title.textContent = "没有匹配的内容";
      p.textContent = "换个关键词，或清空搜索看看全部。";
    } else {
      title.textContent = "收件箱还是空的";
      p.textContent = "粘贴一条你刚看到的文章或视频链接。";
    }
  }
}

async function fetchAll() {
  const q = new URLSearchParams({ limit: String(FETCH_LIMIT), offset: "0" });
  const r = await api("/v1/items?" + q.toString());
  return r.items || [];
}

async function fetchSearch(text) {
  const q = new URLSearchParams({ search: text, limit: String(FETCH_LIMIT), offset: "0" });
  const r = await api("/v1/items?" + q.toString());
  $("searchCount").textContent = r.total ? String(r.total) : "";
  return r.items || [];
}

export async function refreshItems() {
  if (refreshing) return;
  refreshing = true;
  try {
    const q = searchQuery();
    let items;
    if (q) {
      // 搜索时三组让位给结果列表；组的 DOM 原样留着，退出搜索直接复用
      for (const g of GROUPS) $(g.group).hidden = true;
      items = await fetchSearch(q);
      fillList($("searchList"), items);
      $("groupSearch").hidden = items.length === 0;
    } else {
      $("groupSearch").hidden = true;
      items = await fetchAll();
      renderGroups(items);
    }
    // 「有任务在跑」由服务端给（workflow.has_active_job），前端不猜状态
    workingActive = items.some((it) => it.workflow && it.workflow.has_active_job);
    updateEmptyState(items);
  } catch (e) {
    if (!(e && e.net && document.visibilityState === "hidden")) showErr(e);
  } finally {
    refreshing = false;
    armListPoll();
  }
}

// 轮询节奏（§6.4，审查 C-06）：只有一个自排期的定时器——有活跃任务 4s，
// 没有就 30s；两个常驻 setInterval 会把空闲时的列表拉取放大到每 4s 一次。
// 页面不可见、或详情/设置这类覆盖列表的视图打开时不拉（拉了也没人看）。
const ACTIVE_REFRESH_MS = 4000;
const IDLE_REFRESH_MS = 30000;
let listTimer = null;
let workingActive = false;
let pollPaused = false;

function armListPoll() {
  clearTimeout(listTimer);
  listTimer = setTimeout(() => {
    listTimer = null;
    if (pollPaused || document.visibilityState !== "visible") { armListPoll(); return; }
    refreshItems();
  }, workingActive ? ACTIVE_REFRESH_MS : IDLE_REFRESH_MS);
}

export function setListPollPaused(paused) {
  if (pollPaused === paused) return;
  pollPaused = paused;
  if (!paused && document.visibilityState === "visible") refreshItems();
}

export function scheduleRefresh() { armListPoll(); }

// —— 搜索框：与放大镜同排，从图标处向左展开（宽度过渡），不推动下方条目 ——
function searchSlotMax() {
  const toolbar = $("listToolbar");
  // 扣掉图标 + 两个 flex gap（spacer|槽、槽|图标各 8px）；超出容器会把图标挤跑
  return Math.max(0, toolbar.clientWidth - $("searchToggle").offsetWidth - 16);
}

function setSearchOpen(open) {
  searchOpen = open;
  const slot = $("searchRow");
  $("listToolbar").classList.toggle("search-open", open);
  slot.style.width = open ? searchSlotMax() + "px" : "0px";
  $("searchToggle").setAttribute("aria-expanded", String(open));
  // preventScroll：槽此刻宽度还在 0，默认 focus 会横向滚动祖先把整页推跳（走查反馈：点搜索左右跳）
  if (open) $("searchBox").focus({ preventScroll: true });
  else $("searchBox").value = "";
  refreshItems();
}

export function initItemList() {
  document.querySelectorAll("ul.items").forEach((ul) => {
    ul.addEventListener("click", (e) => {
      if (e.target.closest("a") || e.target.closest("button")) return;
      const li = e.target.closest("li.item");
      if (li) openDetail(li.dataset.id);
    });
    ul.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      const li = e.target.closest("li.item");
      if (!li || e.target !== li) return;
      e.preventDefault();
      openDetail(li.dataset.id);
    });
  });
  // —— 抽屉：拉手拖着整页跟手下拉，松手按位移/速度决定展开或弹回；纯点击仍可开关 ——
  //    点面板外/Esc/收起钮收起。拖拽结束后的那次 click 是手势余波，吞掉不回切
  const drawerHandle = $("drawerHandle");
  let drag = null;           // { id, startY, basePull, moved, samples:[{y,t}] }
  let swallowClick = false;

  drawerHandle.addEventListener("click", () => {
    if (swallowClick) { swallowClick = false; return; }
    // 收起动画期间落在拉手上的连点不回开：否则快速连点时按钮在开/关之间来回弹（走查反馈：按钮跳）
    if (Date.now() - drawerClosedAt < 400) return;
    if (isDrawerOpen()) closeDrawer(); else openDrawer();
  });
  drawerHandle.addEventListener("pointerdown", (e) => {
    if (isDrawerOpen() || e.button > 0) return;  // 展开态拉手已在屏外
    swallowClick = false;
    if (pullRaf) { cancelAnimationFrame(pullRaf); pullRaf = 0; }
    drag = { id: e.pointerId, startY: e.clientY, basePull: pull, moved: false, samples: [] };
    document.body.style.userSelect = "none";
  });
  document.addEventListener("pointermove", (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    const now = performance.now();
    drag.samples.push({ y: e.clientY, t: now });
    while (drag.samples.length > 2 && now - drag.samples[0].t > 100) drag.samples.shift();
    const dy = e.clientY - drag.startY;
    if (!drag.moved && Math.abs(dy) > 3) drag.moved = true;
    if (drag.moved) applyPull(Math.min(Math.max(0, drag.basePull + dy), drawerMaxPull()));
  });
  const endDrag = (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    const d = drag;
    drag = null;
    document.body.style.userSelect = "";
    if (!d.moved) return;  // 纯点击：交给 click 处理
    d.samples.push({ y: e.clientY, t: performance.now() });
    const s = d.samples;
    const vel = s.length > 1 && s[s.length - 1].t > s[0].t
      ? (s[s.length - 1].y - s[0].y) / (s[s.length - 1].t - s[0].t) : 0;
    if (pull > drawerMaxPull() * 0.22 || vel > 0.5) {
      swallowClick = true;  // 拖拽展开后紧跟的 click（若有）不得把抽屉关回去
      openDrawer();
    } else {
      closeDrawer();        // 弹回；drawerClosedAt 会吞掉余波 click
    }
  };
  document.addEventListener("pointerup", endDrag);
  document.addEventListener("pointercancel", endDrag);
  $("drawerClose").addEventListener("click", () => closeDrawer());
  document.addEventListener("click", (e) => {
    if (swallowClick) { swallowClick = false; return; }  // 拖拽余波落在面板外时由这里吞
    // 「完成」那个菜单挂在 main 底下（要盖在面板之上），点它不该把抽屉一起收掉
    if (isDrawerOpen() && !isModalOpen() && !e.target.closest("#listWrap") &&
        !e.target.closest("#shareSelMenu")) closeDrawer();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !isModalOpen() && isDrawerOpen()) {
      if (searchOpen) { setSearchOpen(false); return; }  // 先收搜索，再收抽屉
      closeDrawer();
    }
  });
  window.addEventListener("resize", () => {
    if (pull > 0) applyPull(Math.min(pull, drawerMaxPull()));
    if (searchOpen) $("searchRow").style.width = searchSlotMax() + "px";
  });
  $("searchToggle").addEventListener("click", () => setSearchOpen(!searchOpen));
  $("doneToggle").addEventListener("click", () => {
    $("listDone").classList.toggle("collapsed");
    updateDoneToggle($("listDone").children.length);
  });
  $("searchBox").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(refreshItems, 300);
  });
}
