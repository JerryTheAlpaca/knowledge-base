// item-list.js — 首页条目流（docs/17 §4.5、§6.4）
//
// 三个用户分组：需要你处理 / 正在处理 / 最近完成；空组不显示。
// 状态文案与进度全部来自服务端 WorkflowView；本模块不做业务状态推断。
// 刷新按 item_id 做 DOM diff，只更新变化行，保护滚动位置（§6.4）。

import { $, api, esc, fmtShort, showErr } from "./api.js";
import { groupOf, listRowAux } from "./workflow.js";
import { openDetail } from "./item-detail.js";

const FETCH_LIMIT = 50;
let refreshing = false;
let searchTimer = null;

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
export function openDrawer() {
  $("listWrap").classList.add("open");
  $("drawerHandle").setAttribute("aria-expanded", "true");
  animatePull(drawerMaxPull());
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

function fillGroup(groupKey, items) {
  const section = $(groupKey === "search" ? "groupSearch" : "group" + groupKey.charAt(0).toUpperCase() + groupKey.slice(1));
  const ul = section.querySelector("ul.items");
  section.hidden = items.length === 0;
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

function updateEmptyState() {
  const searching = !$("searchRow").hidden && $("searchBox").value.trim();
  const any = ["groupAttention", "groupWorking", "groupPublished", "groupSearch"]
    .some((id) => !$(id).hidden);
  $("emptyState").hidden = any || !!searching;
  if (!any && searching) {
    $("emptyState").hidden = false;
    $("emptyState").querySelector(".empty-title").textContent = "没有匹配的内容";
    $("emptyState").querySelector("p").textContent = "换个关键词，或清空搜索看看全部。";
  } else {
    $("emptyState").querySelector(".empty-title").textContent = "收件箱还是空的";
    $("emptyState").querySelector("p").textContent = "粘贴一条你刚看到的文章或视频链接。";
  }
}

async function fetchView(view) {
  const q = new URLSearchParams({ view, limit: String(FETCH_LIMIT), offset: "0" });
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
    const searching = !$("searchRow").hidden && $("searchBox").value.trim();
    if (searching) {
      const items = await fetchSearch(searching);
      fillGroup("search", items);
    } else {
      $("groupSearch").hidden = true;
      const [attention, working, published] = await Promise.all([
        fetchView("attention"), fetchView("working"), fetchView("published"),
      ]);
      fillGroup("attention", attention);
      fillGroup("working", working);
      fillGroup("published", published);
    }
    updateEmptyState();
  } catch (e) {
    if (!(e && e.net && document.visibilityState === "hidden")) showErr(e);
  }
  refreshing = false;
}

// 轮询节奏（§6.4）：有活跃任务 4s；否则 30s；页面不可见时暂停
export function scheduleRefresh(getDetailActive) {
  setInterval(() => {
    if (document.visibilityState !== "visible") return;
    const active = getDetailActive();
    refreshItems();
    if (active) refreshDetailFromList();
  }, 4000);
  setInterval(() => {
    if (document.visibilityState !== "visible") return;
    refreshItems();
  }, 30000);
}

function refreshDetailFromList() {
  // 详情的增量刷新由 item-detail 自管，这里只负责列表
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
    if (isDrawerOpen() && !e.target.closest("#listWrap")) closeDrawer();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && isDrawerOpen()) closeDrawer();
  });
  window.addEventListener("resize", () => {
    if (pull > 0) applyPull(Math.min(pull, drawerMaxPull()));
  });
  $("searchToggle").addEventListener("click", () => {
    const row = $("searchRow");
    row.hidden = !row.hidden;
    if (!row.hidden) { $("searchBox").focus(); refreshItems(); }
    else { $("searchBox").value = ""; refreshItems(); }
  });
  $("searchBox").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(refreshItems, 300);
  });
}
