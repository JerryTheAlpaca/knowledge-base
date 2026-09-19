// onboarding.js — 首次初始化三步引导（docs/17 §8.1、§8.2）
//
// 进入条件由服务端判定（/v1/onboarding）：没有可用默认模型，或没有已授权
// 桌面设备。平台连接可选，不阻塞完成；已完成的存量用户不进向导。

import { $, api, esc, toast, showErr, openModalHTML, closeModal } from "./api.js";
import { openProfileForm } from "./settings.js";

let polling = null;

export async function maybeShowOnboarding() {
  let ob;
  try { ob = await api("/v1/onboarding"); }
  catch (e) { return; }  // 引导失败不打断首页
  // 已完成初始化的账号没有可重跑的步骤，菜单项点了也不会出现卡片
  $("menuOnboarding").hidden = ob.completed;
  if (ob.completed || ob.dismissed) { $("onboardingHost").hidden = true; return; }
  render(ob);
}

function render(ob) {
  const host = $("onboardingHost");
  host.hidden = false;
  const model = ob.model || {};
  const obsidian = ob.obsidian || {};
  const platforms = ob.platforms || {};
  const steps = [];

  // 第一步：选择整理模型
  if (model.completed) {
    steps.push(stepHTML(1, "选择整理模型", true, "已选择 <b>" + esc(model.label || "默认模型") + "</b>。"));
  } else {
    steps.push(stepHTML(1, "选择整理模型", false,
      '<div id="obModelBody">加载中…</div>'));
  }

  // 第二步：连接 Obsidian
  if (obsidian.completed) {
    const dev = obsidian.active_device || {};
    steps.push(stepHTML(2, "连接 Obsidian", true,
      "已连接 <b>" + esc(dev.name || "桌面设备") + "</b>，整理完成的笔记会自动写入你的知识库。"));
  } else {
    steps.push(stepHTML(2, "连接 Obsidian", false,
      '<div class="small">两步即可连接：</div>' +
      '<ol class="small steps-list">' +
      "<li>在电脑版 Obsidian 中安装 KB Inbox 插件。</li>" +
      "<li>在插件设置里登录你的账号。</li></ol>" +
      '<div class="small" id="obDeviceWait" class="mt-6">等待连接中，连接成功后此步骤会自动完成…</div>'));
  }

  // 第三步：可选连接内容平台
  if (platforms.completed) {
    steps.push(stepHTML(3, "连接内容平台（可选）", true, "已连接 B 站，可以读取需要登录的字幕。"));
  } else {
    steps.push(stepHTML(3, "连接内容平台（可选）", false,
      '<div class="small">可选，可以稍后设置。连接 B 站后，需要登录才能查看的字幕也能读取。</div>' +
      '<div class="row mt-8">' +
      '<button class="small" data-ob="connect-bili">连接 B 站</button></div>'));
  }

  const allDone = model.completed && obsidian.completed;
  host.innerHTML =
    '<div class="onboard" id="onboardCard">' +
      '<div class="onboard-title">欢迎使用金蔷薇</div>' +
      '<div class="onboard-sub">三步完成初始化：选择整理模型、连接 Obsidian 知识库；内容平台是可选项。</div>' +
      '<div class="obsteps">' + steps.join("") + "</div>" +
      '<div class="row mt-14">' +
        (allDone ? '<button class="primary" data-ob="done">添加第一条内容</button>' : "") +
        '<button class="ghost" data-ob="dismiss">稍后再说</button>' +
      "</div>" +
    "</div>";

  if (!model.completed) loadModelPicker();
  if (!obsidian.completed || !model.completed) startPolling();
}

function stepHTML(n, title, done, body) {
  return '<div class="obstep' + (done ? " done" : "") + '">' +
    '<div class="row"><span class="obstate' + (done ? " done" : "") + '"></span>' +
    '<span class="obstep-current">' + n + ". " + esc(title) + "</span></div>" +
    '<div class="obbody">' + body + "</div></div>";
}

async function loadModelPicker() {
  const host = $("obModelBody");
  if (!host) return;
  try {
    const profiles = (await api("/v1/provider-profiles")).filter((p) => p.kind === "llm" && p.configured);
    if (profiles.length) {
      host.innerHTML = profiles.map((p) =>
        '<label class="obradio"><input type="radio" name="obModel" value="' + esc(p.id) + '" data-model="' + esc(p.model) + '">' +
        esc(p.model) + "</label>").join("") +
        '<div class="row mt-6">' +
        '<button class="small primary" data-ob="save-model">使用选中的模型</button>' +
        '<button class="small ghost" data-ob="new-model">新建模型配置</button></div>';
    } else {
      host.innerHTML = '<div class="small">还没有模型配置。整理内容需要一个模型服务的密钥（API Key）。</div>' +
        '<div class="row mt-6"><button class="small primary" data-ob="new-model">新建模型配置</button></div>';
    }
  } catch (e) {
    host.textContent = "加载失败，请刷新重试。";
  }
}

function startPolling() {
  stopPolling();
  polling = setInterval(async () => {
    if ($("onboardingHost").hidden || document.visibilityState !== "visible") return;
    try {
      const ob = await api("/v1/onboarding");
      if (ob.completed) { stopPolling(); render(ob); return; }
      // 模型步骤完成而 Obsidian 未完成时刷新本卡
      const card = $("onboardCard");
      if (card) render(ob);
    } catch (e) { /* 静默重试 */ }
  }, 5000);
}
function stopPolling() { if (polling) { clearInterval(polling); polling = null; } }

async function saveDefaultModel() {
  const picked = document.querySelector('input[name="obModel"]:checked');
  if (!picked) { toast("先选择一个模型", { type: "error" }); return; }
  try {
    await api("/v1/settings", { method: "PATCH", body: { default_profile_id: picked.value } });
    toast("已选择整理模型", { type: "ok" });
    maybeShowOnboarding();
  } catch (e) { showErr(e); }
}

function openBiliConnectModal() {
  openModalHTML(
    '<div class="modal-title">连接 B 站</div>' +
    '<div class="modal-body">' +
    '<p class="small mt-0">登录 B 站网页版后，从浏览器 Cookie 中复制 SESSDATA 的值粘贴到下面。' +
    "它只用于读取你需要登录才能查看的字幕，加密保存在服务器上。</p>" +
    '<label for="obBiliSecret">B 站登录信息（SESSDATA）</label>' +
    '<input id="obBiliSecret" type="password" autocomplete="off" placeholder="粘贴 SESSDATA 值或整个 Cookie 串">' +
    "</div>" +
    '<div class="modal-foot"><button id="mCancel">取消</button>' +
    '<button id="obBiliOk" class="primary">连接</button></div>');
  $("mCancel").onclick = () => closeModal(null);
  $("obBiliOk").onclick = async () => {
    const s = $("obBiliSecret").value.trim();
    if (!s) { toast("先粘贴 SESSDATA", { type: "error" }); return; }
    try {
      await api("/v1/bilibili-session", { method: "PUT", body: { secret: s } });
      closeModal(null);
      toast("已连接 B 站", { type: "ok" });
      maybeShowOnboarding();
    } catch (e) { showErr(e); }
  };
}

export function initOnboarding() {
  $("onboardingHost").addEventListener("click", (e) => {
    const b = e.target.closest("[data-ob]");
    if (!b) return;
    const act = b.dataset.ob;
    if (act === "save-model") saveDefaultModel();
    else if (act === "new-model") openProfileForm();
    else if (act === "connect-bili") openBiliConnectModal();
    else if (act === "dismiss") {
      api("/v1/onboarding", { method: "PATCH", body: { dismiss: true } })
        .then(() => { $("onboardingHost").hidden = true; stopPolling(); })
        .catch(showErr);
    } else if (act === "done") {
      $("onboardingHost").hidden = true;
      stopPolling();
      $("captureCard").scrollIntoView({ behavior: "smooth", block: "start" });
      $("capText").focus();
    }
  });
}

export async function reopenOnboarding() {
  try { await api("/v1/onboarding", { method: "PATCH", body: { reopen: true } }); } catch (e) { /* 静默 */ }
  maybeShowOnboarding();
}
