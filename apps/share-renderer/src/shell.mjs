// 可信外层与生成子页面的装配（docs/20 §9.2、§9.3）。
// 外层由系统代码生成；模型 HTML 只进入 sandbox="allow-scripts" 的 srcdoc 子页面。
import { createHash } from 'node:crypto';

export function sha256Base64(bytes) {
  return createHash('sha256').update(bytes).digest('base64');
}

export function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;');
}

/** iframe srcdoc 属性值：属性上下文里必须转义 & 与引号，其余按文本处理。 */
export function escapeAttr(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('"', '&quot;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;');
}

/** 内联脚本/JSON 的 HTML 解析边界：不能被 </script> 或 <!-- 提前结束。 */
export function inlineScriptBytes(text) {
  return String(text ?? '')
    .replaceAll(/<\/(script)/gi, '<\\/$1')
    .replaceAll(/<!--/g, '<\\!--')
    .replaceAll(/<!(\[CDATA\[)/g, '<\\/$1');
}

/** JSON 放进 <script type="application/json">：转义所有 < ，避免解析边界。 */
export function jsonForScriptTag(value) {
  return JSON.stringify(value).replaceAll('<', '\\u003c').replaceAll(' ', '\\u2028').replaceAll(' ', '\\u2029');
}

/** srcdoc 继承外层策略，因此外层必须同时允许外层桥接与子页面脚本的哈希。
 * frame-ancestors 不能写在 meta 里（浏览器会忽略并报错），由分享站点的响应头设置。 */
export function outerCsp({ outerScriptHash, childScriptHash, frameSrc = "'none'" }) {
  const sources = [`'sha256-${outerScriptHash}'`];
  if (childScriptHash) sources.push(`'sha256-${childScriptHash}'`);
  return [
    "default-src 'none'",
    `script-src ${sources.join(' ')}`,
    "style-src 'unsafe-inline'",
    'img-src data:',
    'font-src data:',
    "connect-src 'none'",
    "object-src 'none'",
    `frame-src ${frameSrc}`,
    "base-uri 'none'",
    "form-action 'none'",
  ].join('; ');
}

export function childCsp({ scriptHash }) {
  return [
    "default-src 'none'",
    `script-src 'sha256-${scriptHash}'`,
    "style-src 'unsafe-inline'",
    'img-src data:',
    'font-src data:',
    "connect-src 'none'",
    "object-src 'none'",
    "frame-src 'none'",
    "base-uri 'none'",
    "form-action 'none'",
  ].join('; ');
}

// 子页面桥接：只转发受控事件，不暴露 postMessage 给模型代码
const CHILD_BRIDGE = `(() => {
  const send = (m) => { try { parent.postMessage(m, '*'); } catch (_) {} };
  document.addEventListener('click', (e) => {
    const t = e && e.target && e.target.closest ? e.target.closest('[data-ref]') : null;
    if (!t) return;
    const ref = String(t.getAttribute('data-ref') || '').slice(0, 64);
    if (ref) send({ type: 'kb-share:open-ref', ref });
  }, true);
  let last = 0;
  const report = () => {
    const h = Math.ceil(document.documentElement.getBoundingClientRect().height);
    if (!Number.isFinite(h) || Math.abs(h - last) < 24) return;
    last = h;
    send({ type: 'kb-share:height', height: h });
  };
  window.addEventListener('load', () => { report(); send({ type: 'kb-share:ready' }); });
  if (window.ResizeObserver) { try { new ResizeObserver(report).observe(document.documentElement); } catch (_) {} }
  report();
})();`;

export function childScript(modelJs) {
  return `${CHILD_BRIDGE}\n${modelJs ?? ''}`;
}

export function buildChildDoc({ title, htmlBody, css, extraCss, script, csp }) {
  return `<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="${escapeAttr(csp)}">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${escapeHtml(title)}</title>
<style>${css ?? ''}</style>
${extraCss ? `<style>${extraCss}</style>` : ''}
</head>
<body>
${htmlBody}
<script>${inlineScriptBytes(script)}</script>
</body>
</html>`;
}

const OUTER_CSS = `:root{color-scheme:light dark;--kb-fg:#1c1a17;--kb-bg:#f7f4ee;--kb-line:#d8d2c6;--kb-muted:#6c655c;--kb-accent:#8a6d1f}
@media (prefers-color-scheme:dark){:root{--kb-fg:#eae4d8;--kb-bg:#17161a;--kb-line:#39352f;--kb-muted:#a79f92;--kb-accent:#d7b45a}}
*{box-sizing:border-box}
body{margin:0;background:var(--kb-bg);color:var(--kb-fg);font:16px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif}
.kb-bar{position:sticky;top:0;z-index:2;display:flex;gap:.75rem;align-items:center;padding:.55rem .9rem;border-bottom:1px solid var(--kb-line);background:color-mix(in srgb,var(--kb-bg) 92%,transparent);backdrop-filter:blur(6px)}
.kb-title{font-weight:600;font-size:.95rem;letter-spacing:.01em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.kb-sp{flex:1}
.kb-btn{font:inherit;font-size:.85rem;color:inherit;background:transparent;border:1px solid var(--kb-line);border-radius:999px;padding:.3rem .7rem;cursor:pointer}
.kb-btn:hover{border-color:var(--kb-accent);color:var(--kb-accent)}
.kb-btn[aria-expanded=true]{border-color:var(--kb-accent);color:var(--kb-accent)}
.kb-ver{font-size:.75rem;color:var(--kb-muted);white-space:nowrap}
.kb-sources{border-bottom:1px solid var(--kb-line);background:color-mix(in srgb,var(--kb-line) 22%,var(--kb-bg));padding:.7rem .9rem}
.kb-sources[hidden]{display:none}
.kb-sources h2{margin:0 0 .45rem;font-size:.8rem;letter-spacing:.06em;color:var(--kb-muted);text-transform:uppercase}
.kb-sources ol{margin:0;padding-left:1.2rem;display:grid;gap:.5rem}
.kb-sources li{font-size:.9rem}
.kb-src-title{font-weight:600}
.kb-src-meta{color:var(--kb-muted);font-size:.82rem}
.kb-src-meta a{color:var(--kb-accent);text-decoration:underline}
.kb-quote{margin:.2rem 0 0;padding:.35rem .6rem;border-left:2px solid var(--kb-line);color:var(--kb-muted);font-size:.85rem;white-space:pre-wrap}
.kb-sources li.kb-active{outline:2px solid var(--kb-accent);outline-offset:.3rem;border-radius:.35rem}
.kb-note{margin:.55rem 0 0;font-size:.85rem;color:var(--kb-muted)}
.kb-frame{display:block;width:100%;border:0;min-height:calc(100vh - 3.2rem);background:transparent}
.kb-foot{padding:.8rem .9rem 1.4rem;font-size:.78rem;color:var(--kb-muted);border-top:1px solid var(--kb-line)}`;

function outerBridge(outerScriptHashPlaceholder) {
  return `(() => {
  const MIN_H = 240, MAX_H = 20000;
  const frame = document.getElementById('kb-frame');
  const panel = document.getElementById('kb-sources');
  const toggle = document.querySelector('[data-kb-toggle]');
  const metaEl = document.getElementById('kb-meta');
  const meta = metaEl ? JSON.parse(metaEl.textContent) : { references: [] };
  const byId = new Map((meta.references || []).map((r) => [String(r.ref_id), r]));
  function render(item) {
    const li = document.createElement('li');
    const title = document.createElement('div');
    title.className = 'kb-src-title';
    title.textContent = item.title || '（无标题）';
    li.appendChild(title);
    const metaLine = document.createElement('div');
    metaLine.className = 'kb-src-meta';
    const parts = [];
    if (item.author) parts.push(item.author);
    if (item.source_label) parts.push(item.source_label);
    if (item.revision) parts.push('版本 r' + item.revision);
    metaLine.textContent = parts.join(' · ');
    li.appendChild(metaLine);
    if (item.url && /^https?:\\/\\//i.test(item.url)) {
      const link = document.createElement('a');
      link.href = item.url;
      link.textContent = item.url_host || '查看原文';
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      const wrap = document.createElement('div');
      wrap.className = 'kb-src-meta';
      wrap.appendChild(link);
      li.appendChild(wrap);
    }
    if (item.quote) {
      const q = document.createElement('p');
      q.className = 'kb-quote';
      q.textContent = '「' + item.quote + '」';
      li.appendChild(q);
    }
    return li;
  }
  const list = panel.querySelector('ol');
  for (const item of meta.references || []) list.appendChild(render(item));
  if (!meta.references || !meta.references.length) {
    const p = document.createElement('p');
    p.className = 'kb-note';
    p.textContent = '本作品没有可公开引用的来源条目。';
    panel.appendChild(p);
  }
  function openPanel(ref) {
    panel.hidden = false;
    toggle.setAttribute('aria-expanded', 'true');
    if (!ref) return;
    const idx = (meta.references || []).findIndex((r) => String(r.ref_id) === String(ref));
    const li = list.children[idx];
    if (li) {
      for (const el of list.children) el.classList.remove('kb-active');
      li.classList.add('kb-active');
      li.scrollIntoView({ block: 'nearest' });
    }
  }
  toggle.addEventListener('click', () => {
    panel.hidden = !panel.hidden;
    toggle.setAttribute('aria-expanded', String(!panel.hidden));
  });
  let ready = false;
  window.addEventListener('message', (ev) => {
    // 不信任 origin === "null"：必须确认消息来自本 iframe
    if (!ev.source || ev.source !== frame.contentWindow) return;
    const d = ev.data;
    if (!d || typeof d !== 'object') return;
    if (d.type === 'kb-share:ready') { ready = true; return; }
    if (d.type === 'kb-share:open-ref' && typeof d.ref === 'string' && byId.has(d.ref)) { openPanel(d.ref); return; }
    if (d.type === 'kb-share:height' && Number.isFinite(d.height)) {
      frame.style.height = Math.min(MAX_H, Math.max(MIN_H, Math.round(d.height))) + 'px';
    }
  });
  setTimeout(() => { if (!ready) frame.classList.add('kb-slow'); }, 10000);
})();`;
}

/** 成品唯一的对外形态：可信外层 + sandbox srcdoc 子页面（预览、分享、下载同一结构）。 */
export function buildOuterDoc({
  title, childDoc, childScriptHash, references, coverageNotes, limitations,
  revisionLabel, runtimeVersion, frameSrc,
}) {
  const meta = {
    references: (references ?? []).map((r) => ({
      ref_id: String(r.ref_id ?? ''),
      title: String(r.title ?? '').slice(0, 200),
      author: r.author ? String(r.author).slice(0, 120) : null,
      source_label: r.source_label ? String(r.source_label).slice(0, 60) : null,
      url: typeof r.url === 'string' && /^https?:\/\//i.test(r.url) ? r.url.slice(0, 2000) : null,
      url_host: (() => {
        try { return r.url ? new URL(r.url).host : null; } catch { return null; }
      })(),
      quote: r.quote ? String(r.quote).slice(0, 400) : null,
      revision: r.revision ?? null,
    })),
  };
  const outerScript = outerBridge();
  const outerHash = sha256Base64(Buffer.from(outerScript, 'utf8'));
  const csp = outerCsp({ outerScriptHash: outerHash, childScriptHash, frameSrc });
  const notes = (coverageNotes ?? []).filter(Boolean).slice(0, 8);
  const gaps = (limitations ?? []).filter(Boolean).slice(0, 8);
  return `<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="${escapeAttr(csp)}">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>${escapeHtml(title)}</title>
<style>${OUTER_CSS}</style>
</head>
<body>
<header class="kb-bar">
  <div class="kb-title">${escapeHtml(title)}</div>
  <div class="kb-sp"></div>
  <button class="kb-btn" type="button" data-kb-toggle aria-expanded="false" aria-controls="kb-sources">来源（${meta.references.length}）</button>
  <span class="kb-ver">${escapeHtml(revisionLabel ?? '')}</span>
</header>
<section id="kb-sources" class="kb-sources" hidden>
  <h2>本作品使用的材料与来源</h2>
  <ol></ol>
  ${notes.length ? `<p class="kb-note">${notes.map((n) => escapeHtml(n)).join('；')}</p>` : ''}
  ${gaps.length ? `<p class="kb-note">未覆盖：${gaps.map((n) => escapeHtml(n)).join('；')}</p>` : ''}
</section>
<main>
  <iframe id="kb-frame" class="kb-frame" title="${escapeHtml(title)}" sandbox="allow-scripts" srcdoc="${escapeAttr(childDoc)}"></iframe>
</main>
<footer class="kb-foot">由本人选中的材料整理生成 · 运行环境 ${escapeHtml(runtimeVersion)} · 页面在隔离环境中运行，不联网</footer>
<script type="application/json" id="kb-meta">${jsonForScriptTag(meta)}</script>
<script>${inlineScriptBytes(outerScript)}</script>
</body>
</html>`;
}
