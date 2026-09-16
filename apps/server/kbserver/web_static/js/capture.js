// capture.js — 统一采集框（docs/17 §4.2–§4.5）
//
// 一个框接收链接、文字与文件，不先选来源类型：
// - 粘贴 URL → 来源条目；纯文字 → 文字条目；音频 → 自动进入加工（转写）
// - 提取图片/音轨不再常驻：识别到网页/公众号链接才出现「同时保存正文图片」轻量选项
// - 多链接拆分为独立条目；文字自动成为备注；文件独立成条，不绑到第一个链接

import { $, api, esc, toast, showErr, uploadFiles, shortUrl, sha256Hex } from "./api.js";

const URL_RE = /https?:\/\/[^\s"'<>，。；！？、：（）【】《》「」『』…]+/gi;
export function extractUrls(raw) {
  const out = []; const seen = new Set();
  const re = new RegExp(URL_RE.source, "gi");
  let m;
  while ((m = re.exec(raw)) !== null) {
    let u = m[0];
    while (u.length) {
      const last = u.slice(-1);
      if (last === ")") {
        if ((u.match(/\)/g) || []).length > (u.match(/\(/g) || []).length) { u = u.slice(0, -1); continue; }
        break;
      }
      if (/[.,;:!?'"]/.test(last)) { u = u.slice(0, -1); continue; }
      break;
    }
    if (u.length > 8 && !seen.has(u)) { seen.add(u); out.push(u); }
  }
  return out;
}

function bodyText() {
  let text = $("capText").value;
  for (const u of extractUrls(text)) text = text.split(u).join(" ");
  return text.trim();
}

// ---------- 文件与录音状态 ----------
const capFilesState = [];   // 普通附件
const capAudiosState = [];  // 录音：{ file, sessionId, uploadId }
const MAX_AUDIOS = 10;
const AUDIO_EXT_RE = /\.(m4a|mp3|aac|wav|flac|ogg|opus|wma|amr|mka|aiff?|caf)$/i;
function isAudioFile(f) {
  return (f.type || "").startsWith("audio/") || AUDIO_EXT_RE.test(f.name || "");
}

export function saveDrafts() {
  try {
    const text = $("capText").value;
    if (text) sessionStorage.setItem("kb_capture_draft", JSON.stringify({ text }));
  } catch (e) { /* 隐私模式等场景忽略 */ }
}
export function clearDraft() { try { sessionStorage.removeItem("kb_capture_draft"); } catch (e) {} }
export function restoreDraft() {
  let raw = null;
  try { raw = sessionStorage.getItem("kb_capture_draft"); } catch (e) {}
  if (!raw) return;
  try {
    const d = JSON.parse(raw);
    if (d.text) {
      $("capText").value = d.text;
      autosizeCap(); renderCapChips();
      toast("登录已过期，已为你恢复未提交的内容", { type: "info" });
    }
    sessionStorage.removeItem("kb_capture_draft");
  } catch (e) { /* 坏数据直接丢弃 */ }
}

// ---------- 渲染 ----------
function autosizeCap() {
  const t = $("capText");
  t.style.height = "auto";
  t.style.height = Math.min(t.scrollHeight, 220) + "px";
}

function renderCapChips() {
  const urls = extractUrls($("capText").value);
  const host = $("urlChips");
  host.hidden = urls.length === 0;
  host.innerHTML = urls.map((u, i) =>
    '<span class="chip"><span class="lbl" title="' + esc(u) + '">' + esc(shortUrl(u)) +
    '</span><button class="xbtn" data-i="' + i + '" aria-label="移除该链接">✕</button></span>').join("");
  const fh = $("fileChips");
  fh.hidden = capFilesState.length === 0;
  fh.innerHTML = capFilesState.map((f, i) =>
    '<span class="chip"><span class="lbl" title="' + esc(f.name) + '">' + esc(f.name) +
    "（" + (f.size / 1024).toFixed(0) + " KB）</span>" +
    '<button class="xbtn" data-fi="' + i + '" aria-label="移除该附件">✕</button></span>').join("");
  const ah = $("audioChips");
  ah.hidden = capAudiosState.length === 0;
  ah.innerHTML = capAudiosState.map((a, i) =>
    '<span class="chip"><span class="lbl" title="' + esc(a.file.name) + '">录音 ' + esc(a.file.name) +
    "（" + (a.file.size / (1024 * 1024)).toFixed(1) + " MiB）</span>" +
    '<button class="xbtn" data-ai="' + i + '" aria-label="移除该录音">✕</button></span>').join("");
  renderContextOptions(urls);
  renderMultipleNote(urls);
}

// 上下文选项（§4.3）：识别到网页/公众号链接才出现「同时保存正文图片」；
// B 站字幕/音频转写是自动行为，不需要选项
function renderContextOptions(urls) {
  const hasWebPage = capAudiosState.length === 0 &&
    urls.some((u) => !/bilibili\.com/i.test(u));
  $("ctxOpts").hidden = !hasWebPage;
}

// 歧义说明（§4.4）：只在检测到多内容时出现一行说明
function renderMultipleNote(urls) {
  const note = $("multipleNote");
  const text = bodyText();
  const n = urls.length + capAudiosState.length + (capFilesState.length && urls.length > 1 ? 1 : 0);
  if (urls.length > 1 && text) {
    note.textContent = "将创建 " + urls.length + " 条内容；这段文字将作为它们的共同备注。";
    note.hidden = false;
  } else if (n > 1) {
    note.textContent = "将创建 " + n + " 条内容。";
    note.hidden = false;
  } else {
    note.hidden = true;
  }
}

function refreshAudioSummary() {
  const st = $("capAudioState");
  const n = capAudiosState.length;
  if (!n) { st.textContent = ""; return; }
  const total = capAudiosState.reduce((s, a) => s + a.file.size, 0);
  st.textContent = "已选择 " + n + " 个录音，共 " + (total / (1024 * 1024)).toFixed(1) + " MiB";
}

// ---------- 录音分块续传（沿用已验证协议，docs/13 §6.2） ----------
async function uploadAudioFile(entry, onProgress) {
  if (entry.uploadId) return entry.uploadId;
  if (!crypto.subtle || !crypto.subtle.digest) {
    throw new Error("当前浏览器不支持分块上传所需的 SHA-256，请改用桌面端上传。");
  }
  const file = entry.file;
  let session = null;
  if (entry.sessionId) {
    session = await api("/v1/audio-uploads/" + encodeURIComponent(entry.sessionId)).catch(() => null);
  }
  if (!session || session.state !== "receiving" || session.total_bytes !== file.size) {
    session = await api("/v1/audio-uploads", { method: "POST", body: {
      filename: file.name, total_bytes: file.size,
      mime: file.type || "application/octet-stream" } });
  }
  entry.sessionId = session.session_id;
  let offset = session.offset || 0;
  const chunkSize = session.chunk_size || 16 * 1024 * 1024;
  const mib = (n) => (n / (1024 * 1024)).toFixed(0);
  while (offset < file.size) {
    const end = Math.min(offset + chunkSize, file.size);
    const buf = await file.slice(offset, end).arrayBuffer();
    if (onProgress) onProgress("上传 " + mib(offset) + "/" + mib(file.size) + " MiB…");
    const digest = await sha256Hex(buf);
    const r = await api("/v1/audio-uploads/" + encodeURIComponent(session.session_id) + "/chunks", {
      method: "PUT", rawBody: buf,
      headers: { "X-Upload-Offset": String(offset), "X-Chunk-Sha256": digest,
                 "Content-Type": "application/octet-stream" } });
    offset = r.offset;
  }
  const done = await api("/v1/audio-uploads/" + encodeURIComponent(session.session_id) + "/complete",
    { method: "POST", body: {} });
  entry.uploadId = done.upload_id;
  entry.sessionId = null;
  if (onProgress) onProgress("");
  return done.upload_id;
}

// ---------- 提交 ----------
let submitting = false;
let onSubmitted = null;
export function setSubmittedHandler(fn) { onSubmitted = fn; }

// —— 金蔷薇回馈：金粒从提交钮飞向花冠，花冠播一次摇曳+流光（平时页面完全静止） ——
let rosePlayTimer = null;
function roseCelebrate() {
  const wrap = document.querySelector(".rose-wrap");
  const rose = document.querySelector("svg.rose");
  if (!wrap || !rose || wrap.offsetParent === null) return;
  const reduce = window.matchMedia
    && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (!reduce) {
    const tr = rose.getBoundingClientRect();
    const fr = $("capSubmit").getBoundingClientRect();
    const fx = fr.left + fr.width / 2, fy = fr.top + fr.height / 2;
    const tx = tr.left + tr.width / 2, ty = tr.top + tr.height * 0.5;
    const colors = ["#ffd98e", "#f4ca72", "#e2b04a", "#fff3c9"];
    for (let i = 0; i < 22; i++) {
      const p = document.createElement("i");
      p.className = "gold-spark";
      const s = (4 + Math.random() * 5).toFixed(1);
      p.style.cssText = "width:" + s + "px;height:" + s + "px;background:" +
        colors[i % colors.length] + ";left:" + fx + "px;top:" + fy + "px";
      document.body.appendChild(p);
      const dx = (tx - fx) * (0.7 + Math.random() * 0.55) + (Math.random() - 0.5) * tr.width * 0.5;
      const dy = (ty - fy) * (0.7 + Math.random() * 0.55) + (Math.random() - 0.5) * tr.height * 0.16;
      p.animate([
        { transform: "translate(0,0) scale(1)", opacity: 0 },
        { opacity: 1, offset: 0.16 },
        { transform: "translate(" + dx * 0.55 + "px," + dy * 0.5 + "px) scale(.95)", opacity: 1, offset: 0.62 },
        { transform: "translate(" + dx + "px," + dy + "px) scale(.35)", opacity: 0 },
      ], { duration: 900 + Math.random() * 600, delay: i * 32,
           easing: "cubic-bezier(.3,.6,.3,1)", fill: "forwards" })
        .addEventListener("finish", () => p.remove());
    }
  }
  rose.classList.remove("play");
  void rose.getBoundingClientRect();  // 强制重排，连续提交也能重播
  rose.classList.add("play");
  clearTimeout(rosePlayTimer);
  rosePlayTimer = setTimeout(() => rose.classList.remove("play"), 2200);
}

export async function submitCapture() {
  if (submitting) return;
  const urls = extractUrls($("capText").value);
  const text = bodyText();
  const files = capFilesState.slice();
  const audios = capAudiosState.slice();
  if (!urls.length && !text && !files.length && !audios.length) {
    toast("先粘贴一条链接、一段文字，或添加文件", { type: "error" });
    return;
  }
  submitting = true;
  const btn = $("capSubmit");
  btn.disabled = true;
  btn.classList.add("busy");
  btn.setAttribute("aria-label", "提交中");
  roseCelebrate();
  const progress = $("capProgress");
  try {
    // 拆分规则（§4.4）：每个 URL 独立条目；每个录音独立条目；
    // 多 URL 时文件独立成条；纯文字/纯文件各自成条
    const jobs = urls.map((u) => ({ kind: "url", url: u }));
    if (files.length) jobs.push({ kind: "file", files });
    audios.forEach((a) => jobs.push({ kind: "audio", entry: a }));
    if (!jobs.length) jobs.push({ kind: "text" });

    const uploadIds = files.length ? await uploadFiles(files, (s) => (progress.textContent = s)) : [];
    for (const j of jobs) {
      if (j.kind === "audio" && !j.entry.uploadId) {
        await uploadAudioFile(j.entry, (s) => (progress.textContent = s));
      }
    }

    const ids = [];
    try {
      for (let i = 0; i < jobs.length; i++) {
        const j = jobs[i];
        if (jobs.length > 1) progress.textContent = "提交 " + (i + 1) + "/" + jobs.length + "…";
        const body = {
          schema_version: "1.0", client_capture_id: crypto.randomUUID(),
          capture_channel: "web_inbox", source_hint: "unknown",
          include_images: !!$("capImages").checked,
          include_asr: false,
          text: null, share_text: null, original_url: null,
          user_note: null, upload_ids: [],
          processing_intent: "default", primary_audio_upload_id: null,
          content_scope: "unknown", archive_policy: "source_materials",
          captured_at: null,
        };
        if (j.kind === "url") {
          body.input_kind = "url";
          body.original_url = j.url;
          // 文字自动成为备注（§4.4：一个 URL → 文字是它的用户备注）
          if (text) body.user_note = text;
          if (jobs.length === 1 && uploadIds.length && !urls.length) body.upload_ids = uploadIds;
        } else if (j.kind === "text") {
          body.input_kind = "text";
          body.text = text;
        } else if (j.kind === "file") {
          body.input_kind = "file";
          body.upload_ids = uploadIds;
        } else if (j.kind === "audio") {
          body.input_kind = "audio";
          body.processing_intent = "transcribe_audio";
          body.primary_audio_upload_id = j.entry.uploadId;
        }
        const r = await api("/v1/captures", { method: "POST", idempotencyKey: crypto.randomUUID(), body });
        ids.push(r.item_id);
      }
    } catch (e) {
      if (ids.length) {
        toast("已提交 " + ids.length + " 条，其余没有完成，请稍后再试", { type: "error" });
        if (onSubmitted) onSubmitted();
        return;
      }
      throw e;
    }
    // 成功：立即清空输入框，新条目出现在列表顶部（§4.2）
    $("capText").value = "";
    capFilesState.length = 0; $("capFiles").value = "";
    capAudiosState.length = 0;
    $("capAudioState").textContent = "";
    progress.textContent = "";
    renderCapChips(); autosizeCap(); clearDraft();
    toast("已接收 " + ids.length + " 条，后台开始处理", { type: "ok" });
    if (onSubmitted) onSubmitted();
  } catch (e) {
    progress.textContent = "";
    showErr(e);
  }
  btn.disabled = false;
  btn.classList.remove("busy");
  btn.setAttribute("aria-label", "提交");
}

export function initCapture({ onSubmit }) {
  setSubmittedHandler(onSubmit);
  $("capSubmit").addEventListener("click", submitCapture);
  $("capText").addEventListener("input", () => { autosizeCap(); renderCapChips(); });
  $("uploadPick").addEventListener("click", () => $("capFiles").click());
  $("capFiles").addEventListener("change", (e) => {
    const audios = [];
    for (const f of e.target.files) (isAudioFile(f) ? audios : capFilesState).push(f);
    e.target.value = "";
    if (audios.length) {
      let overflow = 0;
      for (const f of audios) {
        if (capAudiosState.length >= MAX_AUDIOS) { overflow++; continue; }
        capAudiosState.push({ file: f, sessionId: null, uploadId: null });
      }
      if (overflow) toast("一次最多提交 10 个录音，超出部分未加入", { type: "error" });
    }
    renderCapChips(); refreshAudioSummary();
  });
  $("urlChips").addEventListener("click", (e) => {
    const b = e.target.closest(".xbtn");
    if (!b) return;
    const urls = extractUrls($("capText").value);
    const u = urls[Number(b.dataset.i)];
    if (u) { $("capText").value = $("capText").value.replace(u, " "); renderCapChips(); autosizeCap(); }
  });
  $("fileChips").addEventListener("click", (e) => {
    const b = e.target.closest(".xbtn");
    if (!b) return;
    capFilesState.splice(Number(b.dataset.fi), 1);
    renderCapChips();
  });
  $("audioChips").addEventListener("click", (e) => {
    const b = e.target.closest(".xbtn");
    if (!b) return;
    capAudiosState.splice(Number(b.dataset.ai), 1);
    renderCapChips(); refreshAudioSummary();
  });
}
