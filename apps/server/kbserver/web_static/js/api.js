// api.js — 统一 API 访问与用户语言契约（docs/17 §10.3、§11）
//
// 错误协议：服务端 ApiError 返回 {error:{code,user_message,action,...}}。
// 本模块只把 user_message 交给界面；响应不符合协议时降级为固定文案，
// 绝不把 e.message、HTTP 状态码或响应正文透传给用户。
// 同时承载三页共用的小型 UI 原语（toast/modal/$/esc），保持模块边界清晰。

export const $ = (id) => document.getElementById(id);
export const esc = (s) => String(s == null ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;").replace(/'/g, "&#39;");

const FALLBACK_MESSAGE = "刚才没有完成，请再试一次。";

export class ApiUiError extends Error {
  constructor(message, { code = "", action = null, status = 0, net = false } = {}) {
    super(message);
    this.userMessage = message;
    this.code = code;
    this.action = action;
    this.status = status;
    this.net = net;
  }
}

export function csrfToken() {
  const m = document.cookie.match(/(?:^|;\s*)kb_csrf=([^;]*)/);
  return m ? decodeURIComponent(m[1]) : "";
}

let onUnauthorized = null;
export function setUnauthorizedHandler(fn) { onUnauthorized = fn; }

export async function api(path, opts = {}) {
  const method = opts.method || "GET";
  const headers = {};
  if (method !== "GET") headers["X-CSRF-Token"] = csrfToken();
  if (opts.idempotencyKey) headers["Idempotency-Key"] = opts.idempotencyKey;
  let body;
  if (opts.formData) { body = opts.formData; }
  else if (opts.rawBody !== undefined) { body = opts.rawBody; }
  else if (opts.body !== undefined) { headers["Content-Type"] = "application/json"; body = JSON.stringify(opts.body); }
  for (const k of Object.keys(opts.headers || {})) headers[k] = opts.headers[k];
  let res;
  try { res = await fetch(path, { method, headers, body, credentials: "same-origin" }); }
  catch (e) { throw new ApiUiError("网络似乎断开了，请检查连接后重试", { net: true }); }
  if (res.status === 401) {
    if (onUnauthorized) onUnauthorized();
    throw new ApiUiError("登录已过期", { status: 401 });
  }
  let data = null;
  try { data = await res.json(); } catch (e) { /* 空响应 */ }
  if (!res.ok) {
    const env = data && data.error ? data.error : null;
    // 用户语言契约：只显示受控 user_message；非协议响应统一降级（§10.3）
    throw new ApiUiError(
      (env && env.user_message) || FALLBACK_MESSAGE,
      { code: (env && env.code) || "", action: (env && env.action) || null, status: res.status },
    );
  }
  return data;
}

// 统一错误入口：任何 catch 都经过这里，保证不透传技术细节
export function showErr(e) {
  if (e && (e.status === 401 || e.status === 503)) return;  // 已跳登录/认证不可用另有提示
  if (e && e.net) { toast("网络似乎断开了，请检查连接后重试", { type: "error" }); return; }
  toast((e && e.userMessage) || FALLBACK_MESSAGE, { type: "error" });
}

// ---------- Toast ----------
export function toast(text, o = {}) {
  const host = $("toasts");
  const el = document.createElement("div");
  el.className = "toast t-" + (o.type || "info");
  el.setAttribute("role", o.type === "error" ? "alert" : "status");
  const t = document.createElement("div"); t.className = "ttext"; t.textContent = text; el.appendChild(t);
  if (o.action) {
    const b = document.createElement("button");
    b.textContent = o.action.label;
    b.onclick = () => { dismissToast(el); if (o.action.fn) o.action.fn(); };
    el.appendChild(b);
  }
  const x = document.createElement("button");
  x.className = "tx"; x.setAttribute("aria-label", "关闭"); x.textContent = "✕";
  x.onclick = () => dismissToast(el);
  el.appendChild(x);
  host.appendChild(el);
  requestAnimationFrame(() => el.classList.add("show"));
  setTimeout(() => dismissToast(el), 3000);  // 走查反馈：所有提示（含错误）3 秒后自动消失
  return el;
}
export function dismissToast(el) {
  if (!el.isConnected) return;
  el.classList.remove("show");
  setTimeout(() => el.remove(), 220);
}

// ---------- Modal 三件套（焦点管理沿用既有验收标准） ----------
let modalResolve = null;
let modalLastFocus = null;
const MODAL_FOCUSABLE = 'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';

export function openModalHTML(html) {
  modalLastFocus = document.activeElement;
  $("modalBox").innerHTML = html;
  const ov = $("overlay");
  ov.hidden = false;
  requestAnimationFrame(() => {
    ov.classList.add("show");
    const f = $("modalBox").querySelector(MODAL_FOCUSABLE);
    if (f) f.focus();
  });
}
export function closeModal(val) {
  const ov = $("overlay");
  ov.classList.remove("show");
  setTimeout(() => { ov.hidden = true; $("modalBox").innerHTML = ""; }, 200);
  if (modalLastFocus && document.contains(modalLastFocus)) {
    try { modalLastFocus.focus(); } catch (e) { /* 元素已不可聚焦则忽略 */ }
  }
  modalLastFocus = null;
  if (modalResolve) { const r = modalResolve; modalResolve = null; r(val); }
}
document.addEventListener("keydown", (e) => {
  if (e.key !== "Tab" || $("overlay").hidden) return;
  const items = Array.from($("modalBox").querySelectorAll(MODAL_FOCUSABLE))
    .filter((el) => !el.disabled && el.offsetParent !== null);
  if (!items.length) return;
  const first = items[0], last = items[items.length - 1];
  if (!$("modalBox").contains(document.activeElement)) { e.preventDefault(); first.focus(); }
  else if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
});
export function confirmModal(o = {}) {
  return new Promise((res) => {
    modalResolve = res;
    openModalHTML(
      '<div class="modal-title">' + esc(o.title || "确认操作") + '</div>' +
      '<div class="modal-body">' + (o.body || "") + '</div>' +
      '<div class="modal-foot"><button id="mCancel">取消</button>' +
      '<button id="mOk" class="' + (o.danger ? "danger" : "primary") + '">' + esc(o.confirmLabel || "确认") + "</button></div>");
    $("mOk").onclick = () => closeModal(true);
    $("mCancel").onclick = () => closeModal(false);
  });
}
export function promptModal(o = {}) {
  return new Promise((res) => {
    modalResolve = res;
    openModalHTML(
      '<div class="modal-title">' + esc(o.title || "输入") + '</div>' +
      '<div class="modal-body">' + (o.body ? '<div style="margin-bottom:8px">' + o.body + "</div>" : "") +
      '<div class="pwrow"><input id="mInput" type="' + (o.type || "text") + '" placeholder="' + esc(o.placeholder || "") +
      '" autocomplete="off"><button type="button" class="ghost eye" id="mEye">显示</button></div>' +
      '<div class="modal-foot"><button id="mCancel">取消</button>' +
      '<button id="mOk" class="primary">' + esc(o.confirmLabel || "确定") + "</button></div>");
    const inp = $("mInput");
    inp.value = o.value || "";
    setTimeout(() => inp.focus(), 60);
    $("mEye").onclick = () => {
      const pw = inp.type === "password";
      inp.type = pw ? "text" : "password";
      $("mEye").textContent = pw ? "隐藏" : "显示";
      inp.focus();
    };
    const ok = () => closeModal(inp.value.trim() || null);
    $("mOk").onclick = ok;
    inp.onkeydown = (e) => { if (e.key === "Enter") ok(); };
    $("mCancel").onclick = () => closeModal(null);
  });
}

// ---------- 通用小工具 ----------
export const fmtTime = (iso) => { if (!iso) return "—"; const d = new Date(iso); return isNaN(d.getTime()) ? "—" : d.toLocaleString("zh-CN", { hour12: false }); };
export function fmtShort(iso) {
  // 相对时间优先（docs/17 §7.1：来源与相对时间作为弱化元信息）
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  const diff = Date.now() - d.getTime();
  if (diff < 60_000) return "刚刚";
  if (diff < 3_600_000) return Math.floor(diff / 60_000) + " 分钟前";
  if (diff < 86_400_000) return Math.floor(diff / 3_600_000) + " 小时前";
  if (diff < 172_800_000) return "昨天";
  const p = (n) => String(n).padStart(2, "0");
  return p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes());
}
export function shortUrl(u) {
  if (!u) return "";
  let s = u.replace(/^https?:\/\//, "").replace(/^www\./, "");
  if (s.length > 36) s = s.slice(0, 36) + "…";
  return s;
}
export function sanitizeFilename(name) {
  return name.replace(/[\\/:*?"<>|\x00-\x1f]/g, "_").slice(0, 80).trim() || "原文";
}
export function sourceTypeLabel(t) {
  const MAP = {
    bilibili: "B 站", wechat_mp: "微信公众号", xiaohongshu: "小红书",
    web: "网页", web_audio: "网页音频", audio_upload: "上传录音",
    weichat_mp: "微信公众号", wechat: "微信公众号", webpage: "网页",
    note: "笔记", file: "文件", unknown: "未知", web_inbox: "未知",
  };
  return MAP[t] || t || "未知";
}
export function uploadFiles(fileList, onProgress) {
  // 普通附件逐个上传；返回 upload_id 列表
  return (async () => {
    const ids = [];
    for (const f of fileList) {
      if (onProgress) onProgress("上传中：" + f.name);
      const fd = new FormData(); fd.append("file", f);
      const r = await api("/v1/uploads", { method: "POST", formData: fd, idempotencyKey: crypto.randomUUID() });
      ids.push(r.upload_id);
    }
    if (onProgress) onProgress("");
    return ids;
  })();
}
export async function sha256Hex(buf) {
  const d = await crypto.subtle.digest("SHA-256", buf);
  return Array.from(new Uint8Array(d)).map((b) => b.toString(16).padStart(2, "0")).join("");
}
