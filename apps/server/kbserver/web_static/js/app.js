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
import { initShares, openSharesView, hideSharesView, isSharesOpen } from "./shares.js";

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
  // 分享创作未启用时不露出入口（选择模式与作品列表都只在启用后出现）
  const sharesOn = !!me.shares_enabled;
  const sharesBtn = $("sharesBtn");
  if (sharesBtn) sharesBtn.hidden = !sharesOn;
  const selectBtn = $("selectToggle");
  if (selectBtn) selectBtn.hidden = !sharesOn;
}

// ---------- 视图路由：/inbox、/inbox?view=settings、/inbox?item=xx ----------
// 首次加载若来自浏览器会话恢复/历史重开（back_forward），忽略遗留的 ?item=：
// 重新打开网站应停在金蔷薇主页；刷新/书签深链接仍保持详情。
let routeFirstLoad = true;
function route() {
  closeModal(null);  // 换视图时关闭遗留弹窗（确认/记录/补充）
  const params = new URLSearchParams(location.search);
  const onShares = params.get("view") === "shares";
  const onSettings = params.get("view") === "settings";
  if (onShares) {
    $("homeView").hidden = true;
    hideSettings();
    openSharesView(params.get("share") || null);
    return;
  }
  if (isSharesOpen()) { hideSharesView(); renderHomeAfterShares(); }
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
function renderHomeAfterShares() {
  $("sharesView").hidden = true;
  $("homeView").hidden = false;
}

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
  // 先停轮询：在飞与后续的认证请求都带着中心的滑动续期 Cookie，
  // 晚到一步就把刚刚清掉的凭据原样种回浏览器（退出后按返回键仍是登录态）。
  setListPollPaused(true);
  try { await api("/v1/auth/logout", { method: "POST" }); } catch (e) { /* 会话已无效也清理界面 */ }
  clearDraft();
  showLogin();
  // 再用一次真实加载替掉当前这条历史记录：返回键取的就是停在历史里的那份文档，
  // 只切视图的话，浏览器仍可能把退出前那份登录态 DOM 原样还给你（不发请求，也不跑启动检查）。
  location.replace("/inbox");
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
  // 登录与注册是两个中心页面：点注册就直接到注册表单，不再让用户去登录页自己找链接
  for (const [sel, target] of [["[data-login-entry]", "/login?next=%2Finbox"],
                               ["[data-register-entry]", "/register?next=%2Finbox"]]) {
    for (const el of document.querySelectorAll(sel)) {
      el.addEventListener("click", () => { window.location.href = target; });
    }
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

// 前进/后退缓存里复活的页面不会重跑 boot()：回来先问一次服务端还会不会认这个会话。
// 别处（或本标签页退出后）会话已失效时，401 由 api() 统一交给 showLogin。
window.addEventListener("pageshow", (e) => {
  if (e.persisted) api("/v1/auth/me").catch(() => { /* 401 已切到登录视图 */ });
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
  initShares();
  window.__kbRefreshItems = refreshItems;
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
