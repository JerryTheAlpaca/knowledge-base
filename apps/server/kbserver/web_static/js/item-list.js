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

// —— 顶部抽屉（金蔷薇布局）：条目折叠在顶栏下，点击/下拉展开，点外部或 Esc 收起 ——
export function isDrawerOpen() { return $("listWrap").classList.contains("open"); }
export function openDrawer() {
  $("listWrap").classList.add("open");
  $("drawerHandle").setAttribute("aria-expanded", "true");
}
let drawerClosedAt = 0;
export function closeDrawer() {
  $("listWrap").classList.remove("open");
  $("drawerHandle").setAttribute("aria-expanded", "false");
  drawerClosedAt = Date.now();
}

// 下拉手势状态：收起态按住拉手往下拖 ≥28px 直接展开
let pressY = null;
let drawerDragging = false;

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
  // —— 抽屉：点击拉手切换；收起态往下拖 ≥28px 展开；点面板外/Esc 收起 ——
  $("drawerHandle").addEventListener("click", () => {
    if (drawerDragging) { drawerDragging = false; return; }  // 手势展开后吞掉这次 click
    // 收起动画期间落在拉手上的连点不回开：否则快速连点时按钮在开/关之间来回弹（走查反馈：按钮跳）
    if (Date.now() - drawerClosedAt < 400) return;
    if (isDrawerOpen()) closeDrawer(); else openDrawer();
  });
  $("drawerHandle").addEventListener("pointerdown", (e) => {
    pressY = e.clientY; drawerDragging = false;
  });
  $("drawerClose").addEventListener("click", () => closeDrawer());
  document.addEventListener("pointermove", (e) => {
    if (pressY === null || drawerDragging) return;
    if (e.clientY - pressY > 28) { drawerDragging = true; openDrawer(); }
  });
  document.addEventListener("pointerup", () => {
    pressY = null;
    if (drawerDragging) {
      // 紧随的 click（若有）会先于这个定时器派发并被拉手吞掉；
      // 之后无论如何都复位，避免标志卡死吞掉下一次正常点击（走查反馈：有时按了没反应）
      setTimeout(() => { drawerDragging = false; }, 0);
    }
  });
  document.addEventListener("pointercancel", () => { pressY = null; });
  document.addEventListener("click", (e) => {
    if (isDrawerOpen() && !e.target.closest("#listWrap")) closeDrawer();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && isDrawerOpen()) closeDrawer();
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
