// app.js — 启动与导航（docs/17 §3、§4.1）
//
// 顶部不再有「收件箱 / 设置」页签：左侧产品名，右侧无边框齿轮与账号菜单。
// 设置走浏览器路由（?view=settings）进入，不与首页争夺导航层级。

import { $, api, setUnauthorizedHandler, closeModal } from "./api.js";
import { initCapture, submitCapture, restoreDraft, saveDrafts, clearDraft } from "./capture.js";
import { initItemList, refreshItems, scheduleRefresh, openDrawer } from "./item-list.js";
import { initDetail, openDetail, closeDetail, currentDetailId, isDetailBusy } from "./item-detail.js";
import { initOnboarding, maybeShowOnboarding, reopenOnboarding } from "./onboarding.js";
import { initSettings, showSettings, hideSettings } from "./settings.js";

let meInfo = null;

function showLogin() {
  $("topbar").hidden = true;
  $("appView").hidden = true;
  $("loginView").hidden = false;
}

function showApp(me) {
  meInfo = me;
  $("loginView").hidden = true;
  $("appView").hidden = false;
  $("topbar").hidden = false;
  const name = me.central_username || me.display_name || me.user_id;
  $("whoChip").textContent = name;
  $("menuUserName").textContent = name;
  $("menuAdmin").hidden = !me.is_admin;
}

// ---------- 视图路由：/inbox、/inbox?view=settings、/inbox?item=xx ----------
function route() {
  closeModal(null);  // 换视图时关闭遗留弹窗（确认/记录/补充）
  const params = new URLSearchParams(location.search);
  if (params.get("view") === "settings") {
    $("homeView").hidden = true;
    showSettings();
    return;
  }
  hideSettings();
  $("homeView").hidden = false;
  const item = params.get("item");
  if (item && item !== currentDetailId()) openDetail(item, { push: false });
  else if (!item && currentDetailId()) closeDetail();
}

export function openSettings(cardId) {
  try { history.pushState({ view: "settings" }, "", "/inbox?view=settings"); } catch (e) { /* 忽略 */ }
  route();
  if (cardId) {
    setTimeout(() => {
      const el = $(cardId);
      if (el) {
        el.scrollIntoView({ behavior: "smooth", block: "start" });
        el.classList.add("flash");
        setTimeout(() => el.classList.remove("flash"), 2200);
      }
    }, 60);
  }
}

function closeMenus() {
  $("userMenu").hidden = true;
}

// ---------- 登录 / 登出 ----------
async function doLogout() {
  try { await api("/v1/auth/logout", { method: "POST" }); } catch (e) { /* 会话已无效也清理界面 */ }
  clearDraft();
  showLogin();
}

// ---------- 启动 ----------
function wireTopbar() {
  $("gearBtn").addEventListener("click", () => {
    if (!$("settingsView").hidden) { try { history.pushState({}, "", "/inbox"); } catch (e) {} route(); }
    else openSettings();
  });
  $("settingsBack").addEventListener("click", () => {
    // openSettings 进入时带 {view:"settings"} 历史态：直接 back 回到进入前的位置；
    // 直接以 ?view=settings 打开（无历史态）时兜底推回收件箱
    if (history.state && history.state.view === "settings") { history.back(); return; }
    try { history.pushState({}, "", "/inbox"); } catch (e) { /* 忽略 */ }
    route();
  });
  $("userChip").addEventListener("click", (e) => {
    e.stopPropagation();
    $("userMenu").hidden = !$("userMenu").hidden;
  });
  $("menuAdmin").addEventListener("click", () => { closeMenus(); window.location.href = "/admin"; });
  $("menuOnboarding").addEventListener("click", () => {
    closeMenus();
    if (!$("settingsView").hidden) { try { history.pushState({}, "", "/inbox"); } catch (e) {} route(); }
    reopenOnboarding();
  });
  $("logoutBtn").addEventListener("click", doLogout);
  $("loginBtn").addEventListener("click", () => { window.location.href = "/login?next=%2Finbox"; });
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".menuwrap")) closeMenus();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (!$("overlay").hidden) { closeModalEscape(); return; }
      closeMenus();
    }
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      if (!$("appView").hidden && !$("homeView").hidden) submitCapture();
    }
  });
}
async function closeModalEscape() {
  const m = await import("./api.js");
  m.closeModal(null);
}

setUnauthorizedHandler(() => {
  saveDrafts();
  showLogin();
});

scheduleRefresh(() => isDetailBusy());

window.addEventListener("popstate", route);

(async function boot() {
  wireTopbar();
  // 提交成功后：刷新条目并拉开抽屉，看到新条目进入处理队列
  initCapture({ onSubmit: () => { refreshItems(); openDrawer(); } });
  initItemList();
  initDetail();
  initOnboarding();
  initSettings();
  try {
    const me = await api("/v1/auth/me");
    showApp(me);
    restoreDraft();
    refreshItems();
    maybeShowOnboarding();
    route();
  } catch (e) {
    if (e.status === 503) {
      $("loginView").hidden = false;
      $("loginError").innerHTML = '<div class="error">登录服务暂时不可用，请稍后重试。</div>';
    }
    /* 401 已由 api() 触发 showLogin */
  }
})();
