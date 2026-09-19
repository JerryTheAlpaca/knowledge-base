// app.js — 启动与导航（docs/17 §3、§4.1）
//
// 顶部不再有「收件箱 / 设置」页签：左侧产品名，右侧无边框齿轮与账号菜单。
// 设置走浏览器路由（?view=settings）进入，不与首页争夺导航层级。

import { $, api, setUnauthorizedHandler, closeModal, isModalOpen } from "./api.js";
import { initCapture, submitCapture, restoreDraft, saveDrafts, clearDraft } from "./capture.js";
import { initItemList, refreshItems, scheduleRefresh, setListPollPaused,
         isDrawerOpen, openDrawer, closeDrawer } from "./item-list.js";
import { initDetail, openDetail, closeDetail, currentDetailId } from "./item-detail.js";
import { initOnboarding, maybeShowOnboarding, reopenOnboarding } from "./onboarding.js";
import { initSettings, showSettings, hideSettings } from "./settings.js";

let meInfo = null;
// 从展开的条目抽屉进入设置：返回时恢复抽屉而不是落回金蔷薇主页
let settingsFromDrawer = false;

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
// 首次加载若来自浏览器会话恢复/历史重开（back_forward），忽略遗留的 ?item=：
// 重新打开网站应停在金蔷薇主页；刷新/书签深链接仍保持详情。
let routeFirstLoad = true;
function route() {
  closeModal(null);  // 换视图时关闭遗留弹窗（确认/记录/补充）
  const params = new URLSearchParams(location.search);
  const onSettings = params.get("view") === "settings";
  const wasOnSettings = !$("settingsView").hidden;
  // 列表被详情/设置盖住时停掉它的轮询（审查 C-06）：抽屉展开时列表可见，继续轮询
  setListPollPaused(onSettings || !!params.get("item"));
  if (onSettings) {
    $("homeView").hidden = true;
    showSettings();
    return;
  }
  hideSettings();  // slide-out 浮层化收起，底下立即是 homeView/抽屉
  // 从设置返回且来时抽屉是开着的：定格恢复条目列表页，不闪主页、不播下拉动画
  const reopenDrawer = wasOnSettings && settingsFromDrawer;
  settingsFromDrawer = false;
  const item = params.get("item");
  const nav = performance.getEntriesByType("navigation")[0];
  const restored = routeFirstLoad && nav && nav.type === "back_forward";
  routeFirstLoad = false;
  if (item && restored) {
    try { history.replaceState({}, "", "/inbox"); } catch (e) { /* 忽略 */ }
    return;
  }
  if (item && item !== currentDetailId()) openDetail(item, { push: false });
  else if (!item && currentDetailId()) closeDetail();
  if (reopenDrawer) openDrawer(true);
}

export function openSettings(cardId) {
  settingsFromDrawer = isDrawerOpen();
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

// 品牌「金蔷薇」：任何二级视图（详情/设置）或展开的抽屉下一键回到金蔷薇主页面
function goHome() {
  const drawerWasOpen = isDrawerOpen();
  const atHome = location.pathname === "/inbox" && !location.search &&
    $("settingsView").hidden && $("detailView").hidden && !drawerWasOpen;
  settingsFromDrawer = false;
  if (atHome) return;
  try { history.pushState({}, "", "/inbox"); } catch (e) { /* 忽略 */ }
  route();
  if (drawerWasOpen) closeDrawer();
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
  $("brandHome").addEventListener("click", goHome);
  $("settingsClose").addEventListener("click", () => {
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
  // 登录入口有右上角落与结尾 CTA 两处，指向同一个中心登录页
  for (const el of document.querySelectorAll("[data-login-entry]")) {
    el.addEventListener("click", () => { window.location.href = "/login?next=%2Finbox"; });
  }
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".menuwrap")) closeMenus();
  });
  document.addEventListener("keydown", (e) => {
    // 弹窗的 Escape/点遮罩由共用 modal 模块负责，这里不重复关闭
    if (e.key === "Escape" && !isModalOpen()) closeMenus();
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      if (!$("appView").hidden && !$("homeView").hidden) submitCapture();
    }
  });
}

setUnauthorizedHandler(() => {
  saveDrafts();
  showLogin();
});

scheduleRefresh();

window.addEventListener("popstate", route);

(async function boot() {
  wireTopbar();
  // 提交成功后：刷新条目并更新拉手徽标；留在金蔷薇主页（走查反馈：不要自动跳页）
  initCapture({ onSubmit: () => { refreshItems(); } });
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
