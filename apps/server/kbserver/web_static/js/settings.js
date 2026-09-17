// settings.js — 设置页（docs/17 §8.3）
//
// 顺序：模型配置池（+ 在「模型管理」标题右侧，整理/优化在下方分区选择）
// → Obsidian 与设备 → 内容平台。账号不设独立卡：
// 用户名、管理员入口与退出登录在右上角账号菜单（docs/17 §4.1）。

import { $, api, esc, toast, showErr, confirmModal, promptModal, openModalHTML, closeModal, fmtTime, dismissToast } from "./api.js";

async function loadDevices() {
  const host = $("deviceStatus");
  const list = $("deviceList");
  try {
    const sum = await api("/v1/devices/summary");
    const devices = (await api("/v1/devices")).filter((d) => d.kind === "desktop" && !d.revoked);
    if (!sum.connected) {
      host.innerHTML = '<span class="st-warn" style="display:inline-flex;padding:2px 10px;border-radius:999px">未连接</span>' +
        '<span class="small" style="margin-left:8px">在 Obsidian 中安装 KB Inbox 插件并登录后自动连接。</span>';
    } else {
      const dev = sum.active_device || {};
      host.innerHTML = '<span class="st-ok" style="display:inline-flex;padding:2px 10px;border-radius:999px">已连接</span>' +
        '<span class="small" style="margin-left:8px">当前主要写入设备：<b>' + esc(dev.name || "桌面设备") + "</b>" +
        (dev.last_seen_at ? "，最近连接 " + fmtTime(dev.last_seen_at) : "") + "。</span>";
    }
    list.innerHTML = devices.map((d) =>
      '<div class="devrow"><div class="devmain"><div class="devname">' + esc(d.name) +
      (sum.active_device && sum.active_device.device_id === d.device_id ? ' <span class="muted small">（主要写入设备）</span>' : "") +
      '</div><div class="devmeta">最近连接：' + (d.last_seen_at ? fmtTime(d.last_seen_at) : "—") + "</div></div>" +
      (sum.active_device && sum.active_device.device_id === d.device_id ? "" :
        '<button class="small" data-device-activate="' + esc(d.device_id) + '">设为主要写入设备</button>') +
      '<button class="small danger" data-device-revoke="' + esc(d.device_id) + '">断开</button></div>').join("");
  } catch (e) {
    host.textContent = "设备状态加载失败。";
  }
}

async function activateDevice(deviceId) {
  try {
    await api("/v1/devices/" + encodeURIComponent(deviceId) + "/activate-consumer", { method: "POST" });
    toast("已切换主要写入设备", { type: "ok" });
    loadDevices();
  } catch (e) { showErr(e); }
}

async function revokeDevice(deviceId) {
  const ok = await confirmModal({
    title: "断开设备",
    body: "断开后，这台设备将停止自动写入你的知识库。可重新登录恢复。",
    confirmLabel: "断开", danger: true,
  });
  if (!ok) return;
  try {
    await api("/v1/devices/" + encodeURIComponent(deviceId), { method: "DELETE" });
    toast("已断开设备", { type: "ok" });
    loadDevices();
  } catch (e) { showErr(e); }
}

// ---------- 模型配置（统一配置池 + 整理/优化分档选择） ----------

let loadedProfiles = [];
let currentSettings = null;

function profileCard(p) {
  const hostName = (() => { try { return new URL(p.endpoint).host; } catch (e) { return p.endpoint; } })();
  const stateBadgeHtml = p.configured
    ? '<span class="badge b-ready">已配置密钥</span>'
    : '<span class="badge b-needs_input">未配置密钥</span>';
  const usageBadge =
    (currentSettings && currentSettings.default_profile_id === p.id
      ? '<span class="badge b-queued">整理使用中</span>' : "") +
    (currentSettings && currentSettings.optimize_profile_id === p.id
      ? '<span class="badge b-queued">优化使用中</span>' : "");
  const menuItems =
    '<button class="menu-item" data-act="edit" data-id="' + esc(p.id) + '">编辑配置</button>' +
    '<button class="menu-item" data-act="test" data-id="' + esc(p.id) + '">测试连接</button>' +
    '<button class="menu-item" data-act="rotate" data-id="' + esc(p.id) + '">更换密钥</button>' +
    '<button class="menu-item danger" data-act="delete" data-id="' + esc(p.id) + '">删除配置</button>';
  return '<div class="pcard" data-id="' + esc(p.id) + '">' +
    '<div class="pcard-main">' +
      '<div class="pcard-top"><span class="pcard-model">' + esc(p.model) + "</span>" + stateBadgeHtml + usageBadge + "</div>" +
      '<div class="small" style="margin-top:4px">' + esc(hostName) + "</div>" +
    "</div>" +
    '<div class="menuwrap"><button class="icon" data-menu aria-label="更多操作" aria-haspopup="menu">' +
      '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor">' +
      '<circle cx="5" cy="12" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="19" cy="12" r="1.8"/></svg></button>' +
      '<div class="menu" hidden>' + menuItems + "</div></div>" +
    "</div>";
}

function profileOption(p, selected) {
  return '<option value="' + esc(p.id) + '"' + (selected ? " selected" : "") + ">" +
    esc(p.model) + (p.configured ? "" : "（未配置密钥）") + "</option>";
}

async function loadSettingsData() {
  try {
    const [profiles, settings, bili, asrSettings] = await Promise.all([
      api("/v1/provider-profiles"), api("/v1/settings"), api("/v1/bilibili-session"),
      api("/v1/asr-settings").catch(() => null),
    ]);
    loadedProfiles = profiles.filter((p) => p.kind === "llm");
    currentSettings = settings;
    // 统一配置池：所有 llm 配置都列在这里，用途由下面两个分区选择
    $("profileList").innerHTML = loadedProfiles.length
      ? loadedProfiles.map((p) => profileCard(p)).join("")
      : '<div class="muted" style="padding:8px 0">还没有模型配置——点右上角的 + 添加。</div>';
    $("defaultProfile").innerHTML = '<option value="">（未设置）</option>' +
      loadedProfiles.map((p) => profileOption(p, p.id === settings.default_profile_id)).join("");
    $("optimizeProfile").innerHTML = '<option value="">（未设置）</option>' +
      loadedProfiles.map((p) => profileOption(p, p.id === settings.optimize_profile_id)).join("");
    renderThinkingLevels();
    renderBili(bili);
    renderAsrSettings(asrSettings);
    renderAutoEnrich(settings);
    renderAiParagraphing(settings);
    loadDevices();
  } catch (e) { showErr(e); }
}

function renderAutoEnrich(settings) {
  const cb = $("autoEnrich");
  cb.checked = !(settings && settings.auto_enrich === false);
  cb.onchange = async () => {
    try {
      await api("/v1/settings", { method: "PATCH", body: { auto_enrich: cb.checked } });
      toast(cb.checked ? "已开启 AI 自动整理" : "已关闭：条目只提取原文，不做 AI 整理", { type: "ok" });
    } catch (e) { showErr(e); cb.checked = !cb.checked; }
  };
}

function renderAiParagraphing(settings) {
  const cb = $("aiParagraphing");
  cb.checked = !(settings && settings.ai_paragraphing === false);
  cb.onchange = async () => {
    try {
      await api("/v1/settings", { method: "PATCH", body: { ai_paragraphing: cb.checked } });
      toast(cb.checked ? "已开启：AI 整理将按话题分段并修正听错字词"
                       : "已关闭：分段与纠错改用本地规则，不再产生这部分模型开销", { type: "ok" });
    } catch (e) { showErr(e); cb.checked = !cb.checked; }
  };
}

function renderAsrSettings(asr) {
  const row = $("asrAutoRow");
  if (!asr || !asr.deployment_enabled) { row.hidden = true; return; }
  row.hidden = false;
  const cb = $("asrAuto");
  cb.checked = !!asr.auto_when_no_track;
  cb.disabled = false;
  cb.onchange = async () => {
    try {
      await api("/v1/asr-settings", { method: "PUT", body: { auto_when_no_track: cb.checked } });
      toast(cb.checked ? "已开启：无字幕条目将自动排队转写" : "已关闭自动转写", { type: "ok" });
    } catch (e) { showErr(e); cb.checked = !cb.checked; }
  };
}

function closeMenus() {
  document.querySelectorAll("#settingsView .menu").forEach((m) => (m.hidden = true));
}

async function testProfile(id) {
  const t = toast("正在测试连接…", { type: "info" });
  try {
    const r = await api("/v1/provider-profiles/" + id + "/test", { method: "POST" });
    dismissToast(t);
    if (r.ok) toast("连接正常", { type: "ok" });
    else toast("连接测试没有通过" + (r.message ? "：" + r.message : ""), { type: "error" });
  } catch (e) { dismissToast(t); showErr(e); }
}

async function rotateKey(id) {
  const s = await promptModal({ title: "更换密钥", type: "password",
    placeholder: "粘贴新的模型服务密钥（API Key）", confirmLabel: "保存" });
  if (!s) return;
  try {
    await api("/v1/provider-profiles/" + id, { method: "PATCH", body: { secret: s } });
    toast("密钥已更新", { type: "ok" });
    loadSettingsData();
  } catch (e) { showErr(e); }
}

async function deleteProfile(id) {
  const p = loadedProfiles.find((x) => x.id === id);
  const ok = await confirmModal({ title: "删除模型配置",
    body: "将删除这份模型配置及其托管密钥，正在使用它的整理/优化任务会转去用其他配置。",
    confirmLabel: "删除", danger: true });
  if (!ok) return;
  try {
    await api("/v1/provider-profiles/" + encodeURIComponent(id), { method: "DELETE" });
    toast("已删除配置" + (p ? "：" + p.model : ""), { type: "ok" });
    loadSettingsData();
  } catch (e) { showErr(e); }
}

// profile 传入时为编辑模式；新建为统一模型配置（不预设用途，也不含思考挡位——
// 那是用途档的设置），整理/优化用哪个在下方两个分区里选择
export function openProfileForm({ profile = null } = {}) {
  const isEdit = !!profile;
  const title = isEdit ? "编辑模型配置" : "新建模型配置";
  const secretLabel = "模型服务密钥（API Key）" + (isEdit ? "——留空则不修改" : "");
  openModalHTML(
    '<div class="modal-title">' + title + "</div>" +
    '<div class="modal-body">' +
      '<label for="pfEndpoint">服务地址（Endpoint，HTTPS）</label>' +
      '<input id="pfEndpoint" placeholder="https://api.deepseek.com/v1" value="' + esc(isEdit ? profile.endpoint : "") + '">' +
      '<label for="pfModel">模型名</label>' +
      '<input id="pfModel" placeholder="deepseek-chat" value="' + esc(isEdit ? profile.model : "") + '">' +
      '<label for="pfSecret">' + secretLabel + "</label>" +
      '<div class="pwrow"><input id="pfSecret" type="password" autocomplete="off">' +
      '<button type="button" class="ghost eye" id="pfEye">显示</button></div>' +
    "</div>" +
    '<div class="modal-foot"><button id="mCancel">取消</button>' +
    '<button id="pfOk" class="primary">' + (isEdit ? "保存修改" : "创建配置") + "</button></div>");
  const inp = $("pfSecret");
  $("pfEye").onclick = () => {
    const pw = inp.type === "password";
    inp.type = pw ? "text" : "password";
    $("pfEye").textContent = pw ? "隐藏" : "显示";
  };
  $("mCancel").onclick = () => closeModal(null);
  $("pfOk").onclick = async () => {
    const endpoint = $("pfEndpoint").value.trim(), model = $("pfModel").value.trim(), secret = $("pfSecret").value;
    if (!endpoint || !model) {
      toast("服务地址与模型名必填", { type: "error" }); return;
    }
    try {
      if (isEdit) {
        // capabilities 原样保留（context_tokens 等服务能力；思考挡位已上移到用途档）
        const body = { endpoint, model, capabilities: profile.capabilities || {} };
        if (secret) body.secret = secret;
        await api("/v1/provider-profiles/" + encodeURIComponent(profile.id), { method: "PATCH", body });
        toast("配置已更新", { type: "ok" });
      } else {
        if (!secret) {
          toast("服务地址、模型名与密钥均必填", { type: "error" }); return;
        }
        await api("/v1/provider-profiles", { method: "POST", body: {
          kind: "llm", adapter: "openai-compatible", endpoint, model, secret } });
        toast("配置已创建", { type: "ok" });
      }
      closeModal(null);
      loadSettingsData();
    } catch (e) { showErr(e); }
  };
}

// 思考挡位按用途设置（off/low/high/max）：同一份配置可整理开思考、优化关思考
function renderThinkingLevels() {
  const s = currentSettings || {};
  $("digestThinking").value = s.digest_thinking || "high";
  $("optimizeThinking").value = s.optimize_thinking || "off";
}

async function changeThinkingLevel(field, value) {
  const label = field === "digest_thinking" ? "整理" : "优化";
  try {
    currentSettings = await api("/v1/settings", { method: "PATCH", body: { [field]: value } });
    toast(label + "思考挡位已更新", { type: "ok" });
  } catch (e) { showErr(e); }
  renderThinkingLevels();
}

// 分区下拉选择即保存；保存后重载以刷新卡片上的「使用中」标记
async function selectProfile(field, value) {
  try {
    await api("/v1/settings", { method: "PATCH", body: { [field]: value || null } });
    toast(field === "default_profile_id" ? "整理模型已更新" : "优化模型已更新", { type: "ok" });
  } catch (e) { showErr(e); }
  loadSettingsData();
}

// ---------- B 站登录态 ----------
function renderBili(bili) {
  const v = bili.verification || "unverified";
  let ico = "…", cls = "st-info", title = "未连接", desc = "连接后可以读取需要登录才能查看的字幕。";
  if (bili.configured && v === "valid") {
    ico = "✓"; cls = "st-ok"; title = "登录态有效";
    desc = "需要登录的 B 站字幕可以直接取得。";
  } else if (v === "invalid" || v === "blocked") {
    ico = "✕"; cls = "st-bad"; title = "登录态已失效";
    desc = "B 站不再接受当前登录态，相关视频会提取失败——更新后自动重试。";
  } else if (v === "network_error") {
    ico = "!"; cls = "st-warn"; title = "上次检测受网络影响";
    desc = "登录态可能仍有效；稍后可再次检测。";
  }
  $("biliStatus").className = "bili-state";
  $("biliStatus").innerHTML = '<span class="bili-ico ' + cls + '">' + ico + "</span>" +
    '<div class="bili-main"><div class="bili-title">' + esc(title) + '</div><div class="bili-desc">' + esc(desc) + "</div></div>";
}

async function biliSave() {
  const s = $("biliSecret").value.trim();
  if (!s) { toast("先粘贴 SESSDATA", { type: "error" }); return; }
  try {
    const r = await api("/v1/bilibili-session", { method: "PUT", body: { secret: s } });
    $("biliSecret").value = "";
    toast("登录态已更新" + (r.requeued_items ? "，有 " + r.requeued_items + " 条内容会自动重新提取" : ""), { type: "ok" });
    loadSettingsData();
  } catch (e) { showErr(e); }
}

async function biliCheck() {
  try {
    const r = await api("/v1/bilibili-session/test", { method: "POST" });
    if (r.status === "valid") toast(r.detail || "登录态有效", { type: "ok" });
    else if (r.status === "network_error") toast(r.detail || "检测受网络影响，稍后再试", { type: "warn" });
    else toast(r.detail || "登录态已失效，请更新", { type: "error" });
    loadSettingsData();
  } catch (e) { showErr(e); }
}

async function biliRevoke() {
  const ok = await confirmModal({ title: "撤销 B 站登录态",
    body: "撤销后，需要登录才能读取的字幕将无法取得。",
    confirmLabel: "撤销", danger: true });
  if (!ok) return;
  try { await api("/v1/bilibili-session", { method: "DELETE" }); toast("已撤销 B 站登录态", { type: "ok" }); loadSettingsData(); }
  catch (e) { showErr(e); }
}

export function initSettings() {
  $("newProfileBtn").addEventListener("click", () => openProfileForm());
  $("defaultProfile").addEventListener("change", (e) => selectProfile("default_profile_id", e.target.value));
  $("optimizeProfile").addEventListener("change", (e) => selectProfile("optimize_profile_id", e.target.value));
  $("digestThinking").addEventListener("change", (e) => changeThinkingLevel("digest_thinking", e.target.value));
  $("optimizeThinking").addEventListener("change", (e) => changeThinkingLevel("optimize_thinking", e.target.value));
  $("biliSave").addEventListener("click", biliSave);
  $("biliCheck").addEventListener("click", biliCheck);
  $("biliRevoke").addEventListener("click", biliRevoke);
  $("deviceList").addEventListener("click", (e) => {
    const act = e.target.closest("[data-device-activate]");
    if (act) { activateDevice(act.dataset.deviceActivate); return; }
    const rev = e.target.closest("[data-device-revoke]");
    if (rev) revokeDevice(rev.dataset.deviceRevoke);
  });
  const onProfileCardClick = (e) => {
    const menuBtn = e.target.closest("[data-menu]");
    if (menuBtn) {
      const menu = menuBtn.parentElement.querySelector(".menu");
      const open = menu.hidden;
      closeMenus();
      menu.hidden = !open;
      return;
    }
    const item = e.target.closest("[data-act]");
    if (!item) return;
    const act = item.dataset.act, id = item.dataset.id;
    closeMenus();
    if (act === "edit") {
      const p = loadedProfiles.find((x) => x.id === id);
      if (p) openProfileForm({ profile: p });
    }
    else if (act === "test") testProfile(id);
    else if (act === "rotate") rotateKey(id);
    else if (act === "delete") deleteProfile(id);
  };
  $("profileList").addEventListener("click", onProfileCardClick);
  document.addEventListener("click", onDocClick);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeTips(); });
}

// ---------- 开关说明气泡：点 ⓘ 冒在标题上方，点别处或 Escape 收起 ----------
function setTip(btn, open) {
  const tip = btn.closest(".asrtoggle-text").querySelector(".infotip");
  tip.hidden = !open;
  btn.setAttribute("aria-expanded", String(open));
  if (!open) return;
  // 气泡横向铺满标题列，尾巴要单独对准 ⓘ
  const box = tip.getBoundingClientRect();
  const x = btn.getBoundingClientRect().left - box.left + btn.offsetWidth / 2 - 5;
  tip.style.setProperty("--tail", Math.round(Math.max(14, Math.min(x, box.width - 18))) + "px");
}
function closeTips() {
  document.querySelectorAll(".infobtn[aria-expanded='true']").forEach((b) => setTip(b, false));
}
function onDocClick(e) {
  const btn = e.target.closest(".infobtn");
  if (!btn) { if (!e.target.closest(".infotip")) closeTips(); return; }
  e.preventDefault();  // 按钮在 <label> 内，不能连带切换开关
  const open = btn.getAttribute("aria-expanded") !== "true";
  closeTips();
  setTip(btn, open);
}

export function showSettings() {
  $("settingsView").hidden = false;
  $("homeView").hidden = true;
  loadSettingsData();
}
export function hideSettings() {
  $("settingsView").hidden = true;
  $("homeView").hidden = false;
}
