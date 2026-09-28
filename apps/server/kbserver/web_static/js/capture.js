// capture.js — 统一采集框（docs/17 §4.2–§4.5）
//
// 一个框接收链接、文字与文件，不先选来源类型：
// - 粘贴 URL → 来源条目；纯文字 → 文字条目；音频/视频 → 自动进入加工（转写）
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

// ---------- 文件与音视频状态 ----------
const capFilesState = [];   // 普通附件
const capAudiosState = [];  // 待转写的音视频原件：{ file, kind, sessionId, uploadId }
const MAX_AUDIOS = 10;
// 支持的容器清单由服务端给（/v1/media-formats）：界面不复制一份，免得两边各自漂移。
// 清单还没到就退回浏览器 MIME 判定（音视频仍能识别），真正的接受/拒绝在创建上传
// 会话时由服务端裁定（docs/13 §6.2）。
let mediaFormats = null;
const capRejected = [];     // 认成音视频但格式不支持的文件，只在框里提示不入队

function loadMediaFormats() {
  api("/v1/media-formats").then((r) => { mediaFormats = r; })
    .catch(() => { /* 取不到清单不拦用户：按 MIME 判定，服务端仍会拒绝不支持的容器 */ });
}

function extOf(name) {
  const base = String(name || "").replace(/\\/g, "/").split("/").pop();
  const i = base.lastIndexOf(".");
  return i > 0 ? base.slice(i).toLowerCase() : "";
}

// 返回 "audio" | "video" | "unsupported" | null（null = 不是音视频，走普通附件）
function classifyMedia(f) {
  const mime = f.type || "";
  const ext = extOf(f.name);
  const mediaMime = mime.indexOf("audio/") === 0 || mime.indexOf("video/") === 0;
  if (mediaFormats) {
    if (ext && (mediaFormats.audio || []).indexOf(ext) >= 0) return "audio";
    if (ext && (mediaFormats.video || []).indexOf(ext) >= 0) return "video";
    // 明确不接受的音视频容器要说「不支持」，不能悄悄当成普通附件收下一条空条目；
    // 图片/PDF/文字这类本来就不是音视频的文件仍走附件（返回 null）
    if (ext && (mediaFormats.rejected || []).indexOf(ext) >= 0) return "unsupported";
    return mediaMime ? "unsupported" : null;
  }
  if (!ext) return null;
  if (mime.indexOf("audio/") === 0) return "audio";
  if (mime.indexOf("video/") === 0) return "video";
  return null;
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
// 形态只看「有没有在输入」，不做高度测量：点进来了、或者框里已经有东西（文字、链接
// 胶囊、文件、录音任一），就是圆角矩形（文字在上排，文件钮、链接胶囊、发送钮同在下排）；
// 空框且没焦点就是单行胶囊。旧的 scrollHeight 判定在真机上会把空态误判成多行，表现为
// 提交后卡在圆角矩形回不去（2026-09-17），改成纯状态判定后这条路径不存在了。
function syncCapShape() {
  const box = $("smartBox");
  const hasText = $("capText").value.trim() !== "";
  const focused = box.contains(document.activeElement);
  box.classList.toggle("open",
    focused || hasText || capFilesState.length > 0 || capAudiosState.length > 0);
}

function autosizeCap() {
  const t = $("capText");
  syncCapShape();
  const hasText = t.value.trim() !== "";
  t.style.height = "auto";
  t.style.height = hasText ? Math.min(t.scrollHeight, 220) + "px" : "";
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
    '<span class="chip"><span class="lbl" title="' + esc(a.file.name) + '">' +
    (a.kind === "video" ? "视频 " : "录音 ") + esc(a.file.name) +
    "（" + (a.file.size / (1024 * 1024)).toFixed(1) + " MiB）</span>" +
    '<button class="xbtn" data-ai="' + i + '" aria-label="移除该音视频">✕</button></span>').join("");
  renderContextOptions(urls);
  renderMultipleNote(urls);
  syncCapShape();
}

// 上下文选项（§4.3）：识别到网页/公众号链接才出现「同时保存正文图片」；
// B 站字幕/音频转写是自动行为，不需要选项
function renderContextOptions(urls) {
  const hasWebPage = capAudiosState.length === 0 &&
    urls.some((u) => !/bilibili\.com/i.test(u));
  const box = $("ctxOpts");
  box.hidden = !hasWebPage;
  // 选项行隐藏不等于用户取消：不复位的话，删掉网页链接改传录音后
  // 仍按「提取图片/音轨」提交，产生用户没要求的抓取与转写开销
  if (!hasWebPage) $("capImages").checked = $("capAsr").checked = false;
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
  const parts = [];
  const n = capAudiosState.length;
  if (n) {
    const total = capAudiosState.reduce((s, a) => s + a.file.size, 0);
    parts.push("已选择 " + n + " 个音视频，共 " + (total / (1024 * 1024)).toFixed(1) + " MiB");
  }
  if (capRejected.length) {
    const names = capRejected.slice(0, 3).map((f) => f.name).join("、");
    const more = capRejected.length > 3 ? " 等 " + capRejected.length + " 个" : "";
    const hint = (mediaFormats && mediaFormats.hint) || "可以上传常见的视频与音频文件";
    parts.push("不支持的格式：" + names + more + "；" + hint);
  }
  st.textContent = parts.join("；");
  st.classList.toggle("warn", capRejected.length > 0);
}

// ---------- 音视频分块续传（沿用已验证协议，docs/13 §6.2） ----------
function addPickedFiles(picked) {
  let overflow = 0;
  for (const f of picked) {
    const kind = classifyMedia(f);
    if (kind === "unsupported") {
      if (!capRejected.some((r) => r.name === f.name)) capRejected.push(f);
      continue;
    }
    if (!kind) { capFilesState.push(f); continue; }
    if (capAudiosState.length >= MAX_AUDIOS) { overflow++; continue; }
    capAudiosState.push({ file: f, kind, sessionId: null, uploadId: null });
  }
  if (overflow) toast("一次最多提交 " + MAX_AUDIOS + " 个音视频，超出部分未加入", { type: "error" });
}

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

// —— 金蔷薇回馈：金粉从发送钮喷出来，沿弧线汇聚到花冠上；粉落定的那一刻整朵点亮一下。
//    顺序感全靠 DUST_RISE 这个延时：点亮动画（inbox.css 的 .rose.play）在多数粉落定时挂上。
const DUST_RISE = 760;   // 金粉从发送钮汇聚到花冠所需时间，与下面的飞行时长+错峰相配
const LIT_HOLD = 2900;   // 点亮序列总长，略大于最晚结束的 sway(2.6s)
let litTimer = null, holdTimer = null, settleTimer = null;

// 手机上键盘把整页顶起来，提交后键盘收起、版面要往下走两三百毫秒。这中间量到的花冠
// 是「被顶着」时的位置，金粉就会落在花的上方。所以先等版面停住（视口尺寸与滚动连续
// 120ms 不变，最多等 520ms）再按落定后的位置放动画；桌面没有这档事，一次轮询就过了。
function layoutSettled(next) {
  const vv = window.visualViewport;
  const sig = () => (vv ? [vv.height, vv.width, vv.offsetTop] : []).concat(
    [innerHeight, innerWidth, scrollY]).join();
  let last = sig(), quiet = 0, waited = 0;
  const tick = () => {
    const s = sig();
    quiet = s === last ? quiet + 40 : 0;
    last = s;
    waited += 40;
    if (quiet >= 120 || waited >= 520) next();
    else settleTimer = setTimeout(tick, 40);
  };
  settleTimer = setTimeout(tick, 40);
}

function dustToRose(rose) {
  const tr = rose.getBoundingClientRect();
  const fr = $("capSubmit").getBoundingClientRect();
  const fx = fr.left + fr.width / 2, fy = fr.top + fr.height / 2;
  // 花心就是 svg 方框的正中：viewBox「120 80 360 360」的中心正落在 (300,260) 的花心上
  const cx = tr.left + tr.width / 2, cy = tr.top + tr.height / 2;
  const R = tr.width * 0.3;     // 落点散布半径：压在看得见的瓣圈内（瓣只长到约 .35 宽），不撒到花外的黑底上
  const colors = ["#fffdf2", "#fff3d0", "#ffe4a8", "#f4ca72"];
  for (let i = 0; i < 32; i++) {
    const p = document.createElement("i");
    p.className = "gold-spark";
    const s = (1.8 + Math.random() * 2.2).toFixed(1);   // 1.8–4px：粉要细，亮度交给外发光撑
    p.style.cssText = "width:" + s + "px;height:" + s + "px;background:" +
      colors[i % colors.length] + ";left:" + fx + "px;top:" + fy + "px";
    document.body.appendChild(p);
    // 落点均匀撒满瓣圈（sqrt 才不往圆心堆），最后收在这一点上
    const a = Math.random() * Math.PI * 2;
    const r = Math.sqrt(Math.random()) * R;
    const dx = cx + Math.cos(a) * r - fx;
    const dy = cy + Math.sin(a) * r * 0.94 - fy;
    // 中途沿切向鼓一点，走弧线汇进花里，而不是直愣愣四散开
    const bow = (Math.random() - 0.5) * 0.26;
    const mx = dx * 0.5 - dy * bow, my = dy * 0.5 + dx * bow;
    p.animate([
      { transform: "translate(0,0) scale(.5)", opacity: 0 },
      { transform: "translate(" + dx * 0.12 + "px," + dy * 0.14 + "px) scale(1)", opacity: 1, offset: 0.18 },
      { transform: "translate(" + mx + "px," + my + "px) scale(.9)", opacity: 1, offset: 0.58 },
      { transform: "translate(" + dx + "px," + dy + "px) scale(.5)", opacity: 1, offset: 0.9 },
      { transform: "translate(" + dx + "px," + dy + "px) scale(0)", opacity: 0 },
    ], { duration: 520 + Math.random() * 190, delay: i * 13,
         easing: "cubic-bezier(.34,.56,.28,1)", fill: "forwards" })
      .addEventListener("finish", () => p.remove());
  }
}

function roseCelebrate() {
  const wrap = document.querySelector(".rose-wrap");
  const rose = document.querySelector("svg.rose");
  if (!wrap || !rose || wrap.offsetParent === null) return;
  const reduce = window.matchMedia
    && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  clearTimeout(litTimer);
  clearTimeout(holdTimer);
  clearTimeout(settleTimer);
  rose.classList.remove("play");
  const go = () => {
    if (!reduce) dustToRose(rose);
    litTimer = setTimeout(() => {
      rose.classList.add("play");
      holdTimer = setTimeout(() => rose.classList.remove("play"), LIT_HOLD);
    }, reduce ? 0 : DUST_RISE);
  };
  if (reduce) go(); else layoutSettled(go);
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
    for (const j of jobs) {
      if (jobs.length > 1) progress.textContent = "提交 " + (ids.length + 1) + "/" + jobs.length + "…";
      const body = {
        schema_version: "1.0", client_capture_id: crypto.randomUUID(),
        capture_channel: "web_inbox", source_hint: "unknown",
        include_images: !!$("capImages").checked,
        // 「提取音轨」开关（§4.3）：网页/公众号条目提取完成后自动排队转写；
        // 后端只在网页适配分支消费该值，B 站沿用「无字幕自动转写」设置不受影响
        include_asr: !!$("capAsr").checked,
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
      } else if (j.kind === "text") {
        body.input_kind = "text";
        body.text = text;
      } else if (j.kind === "file") {
        body.input_kind = "file";
        body.upload_ids = uploadIds;
      } else if (j.kind === "audio") {
        // 录音与视频同一条路：都只取音轨转写；input_kind 如实记文件类型
        body.input_kind = j.entry.kind;
        body.processing_intent = "transcribe_audio";
        body.primary_audio_upload_id = j.entry.uploadId;
      }
      try {
        const r = await api("/v1/captures", { method: "POST", idempotencyKey: crypto.randomUUID(), body });
        ids.push(r.item_id);
      } catch (e) {
        if (!ids.length) throw e;
        // 顺序提交，ids 恰好是 jobs 的成功前缀：把已完成部分从输入里摘掉，
        // 剩下的留在框里重试，否则第二次提交会重复建条目
        jobs.slice(0, ids.length).forEach(dropSubmittedJob);
        progress.textContent = "";
        renderCapChips(); autosizeCap();
        toast("已提交 " + ids.length + " 条，其余没有完成，可直接重试", { type: "error" });
        if (onSubmitted) onSubmitted();
        return;
      }
    }
    // 成功：立即清空输入框，新条目出现在列表顶部（§4.2）
    $("capText").value = "";
    capFilesState.length = 0; $("capFiles").value = "";
    capAudiosState.length = 0;
    capRejected.length = 0;
    refreshAudioSummary();
    progress.textContent = "";
    renderCapChips(); autosizeCap(); clearDraft();
    toast("已接收 " + ids.length + " 条，后台开始处理", { type: "ok" });
    if (onSubmitted) onSubmitted();
  } catch (e) {
    progress.textContent = "";
    showErr(e);
  } finally {
    // 复位必须在 finally：漏掉一次就让采集框在一次页面会话里永久卡死
    submitting = false;
    btn.disabled = false;
    btn.classList.remove("busy");
    btn.setAttribute("aria-label", "提交");
  }
}

// 部分成功后把已建条目那份从草稿里去掉（与 submitCapture 的 jobs 构造对应）
function dropSubmittedJob(job) {
  if (job.kind === "url") {
    $("capText").value = $("capText").value.split(job.url).join(" ");
  } else if (job.kind === "file") {
    capFilesState.length = 0; $("capFiles").value = "";
  } else if (job.kind === "audio") {
    const i = capAudiosState.indexOf(job.entry);
    if (i >= 0) capAudiosState.splice(i, 1);
    refreshAudioSummary();
  }
}

export function initCapture({ onSubmit }) {
  setSubmittedHandler(onSubmit);
  loadMediaFormats();
  $("capSubmit").addEventListener("click", submitCapture);
  $("capText").addEventListener("input", () => { autosizeCap(); renderCapChips(); });
  // focusin/focusout 会冒泡，挂在采集框上就同时接得住输入框和两个按钮的进出
  const box = $("smartBox");
  box.addEventListener("focusin", syncCapShape);
  box.addEventListener("focusout", syncCapShape);
  // 展开动画走完再量一次高度：胶囊态文字行两端要给按钮让位（padding 52/60→12 是渐变的），
  // 中途量到的 scrollHeight 会偏大，长链接粘进空框时底下会多留一行空白
  $("capText").addEventListener("transitionend", (e) => {
    if (e.propertyName === "padding-right") autosizeCap();
  });
  window.addEventListener("resize", autosizeCap);
  $("uploadPick").addEventListener("click", () => $("capFiles").click());
  $("capFiles").addEventListener("change", (e) => {
    addPickedFiles(Array.from(e.target.files || []));
    e.target.value = "";
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
