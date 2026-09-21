// 真实隔离验证（docs/20 §9.2、§9.3、验收 A15/A16）：静态检查不是安全边界，
// 这里用一段刻意绕过静态扫描的脚本，验证 sandbox + CSP + 不透明源确实挡住了越界访问。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { writeFile, mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { pathToFileURL } from 'node:url';

import { renderTask } from '../src/build.mjs';
import { makeTask, pageSource } from './helpers.mjs';

// 刻意用别名与拼接绕过静态扫描：验证真实隔离不依赖字符串匹配
const PROBE_JS = `
const w = window;
const d = document;
const out = {};
try { out.net = 'called'; w['fe' + 'tch']('/v1/items').then(() => finish('net-ok'), () => finish('net-blocked')); }
catch (e) { out.net = 'throw'; finish('net-blocked'); }
function finish(extra) {
  try { out.parent = w.top.document.body ? 'leak' : 'leak'; } catch (e) { out.parent = 'blocked'; }
  try { out.cookie = d['co' + 'okie'] ? 'leak' : 'empty'; } catch (e) { out.cookie = 'blocked'; }
  const p = d.createElement('p');
  p.id = 'probe';
  p.textContent = JSON.stringify({ ...out, extra });
  d.body.appendChild(p);
}
setTimeout(() => finish('timeout'), 400);
`;

test('子页面脚本拿不到账号接口、父页面 DOM 与 Cookie，正文仍可阅读', async () => {
  const ctx = await makeTask({
    source: pageSource({
      html_body: '<main><h1>正文标题</h1><p>即使交互初始化失败，这段正文也必须可读。</p></main>',
      javascript: PROBE_JS,
    }),
  });
  const outDir = path.join(ctx.root, 'out');
  const { html } = await renderTask({ taskDir: ctx.taskDir, outFile: path.join(outDir, 'index.html') });
  assert.ok(html.length > 500);

  const { chromium } = await import('playwright');
  const browser = await chromium.launch({ handleSIGINT: false });
  try {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, offline: true });
    const page = await context.newPage();
    const requests = [];
    page.on('request', (req) => {
      if (!req.url().startsWith('file:') && !req.url().startsWith('data:')) requests.push(req.url());
    });
    await page.goto(pathToFileURL(path.join(outDir, 'index.html')).href, { waitUntil: 'load' });
    const frame = page.mainFrame().childFrames()[0];
    await frame.waitForSelector('#probe', { timeout: 8000 });
    const probe = JSON.parse(await frame.textContent('#probe'));
    assert.equal(probe.net, 'called', 'fetch 调用应能发出（随后被 CSP 拦下）');
    assert.equal(probe.extra, 'net-blocked', 'CSP connect-src 未拦截网络请求');
    assert.equal(probe.parent, 'blocked', '子页面读到了父页面 DOM');
    assert.ok(['blocked', 'empty'].includes(probe.cookie), `Cookie 访问异常：${probe.cookie}`);
    await context.clearCookies();
    const heading = await frame.textContent('h1');
    assert.equal(heading, '正文标题', '脚本失败后正文应仍然可读');
    assert.deepEqual(requests, [], '断网成品仍发起了外部请求');
    // 外层必须真的把子页面关进不透明源
    assert.equal(await page.getAttribute('#kb-frame', 'sandbox'), 'allow-scripts');
  } finally {
    await browser.close();
    if (!process.env.KB_KEEP_TMP) await rm(ctx.root, { recursive: true, force: true });
  }
});

test('导航、弹窗与表单提交在契约检查阶段就被拒绝（A16）', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'kb-share-nav-'));
  const file = path.join(root, 'nav.html');
  await writeFile(file, '<!doctype html><p>占位</p>');
  await rm(root, { recursive: true, force: true });
  const snippets = [
    "window.location.assign('https://evil.example')",
    "window.open('https://evil.example')",
    "document.querySelector('form') && document.querySelector('form').submit()",
  ];
  const { checkJs } = await import('../src/checks.mjs');
  for (const js of snippets) {
    assert.ok(checkJs(js).errors.length, `未拒绝：${js}`);
  }
});
