// 隔离浏览器检查（docs/20 §10.2）：检查的是最终交付文件，不是开发源文件。
// 断网上下文 + 记录任何网络尝试；固定 ready 信号；按有限交互声明做真实操作。
import { pathToFileURL } from 'node:url';
import path from 'node:path';
import { mkdir, writeFile } from 'node:fs/promises';

// 正文最少字符数：服务端没给 check.min_body_chars 时的保守值，不让空页面自我认证「检查通过」。
const DEFAULT_MIN_BODY_CHARS = 300;

const VIEWPORTS = [
  { id: 'desktop', width: 1440, height: 900 },
  { id: 'mobile', width: 390, height: 844 },
];

/**
 * 启动固定 Chromium，并留下两样东西：沙箱是否真的在跑、墙钟到点能用的中止手段。
 *
 * 沙箱必须显式开：Playwright 的 chromiumSandbox 默认 false，默认值会往启动参数里加
 * --no-sandbox，光以非 root 运行不会改变它。反过来说，显式传 true 之后 Chromium 就
 * 没有静默退路——沙箱初始化不了（缺 setuid helper、user namespaces 被挡）就是启动失败，
 * 报 BROWSER_UNAVAILABLE，不会出现「以为开着其实关着」。
 *
 * 这里刻意不用 launchServer + connect：那条要在 127.0.0.1 上开 WebSocket，而 runner
 * 容器是 network_mode: "none"，回环网卡未必起着；launch() 走 --remote-debugging-pipe，
 * 不需要任何网络。代价是 1.60 的 Browser 上没有 process()，中止只能用 close——
 * 卡死的是 renderer，浏览器主进程照常响应，close 会把整个进程树收掉。
 */
async function launchChromium() {
  const { chromium } = await import('playwright');
  let browser;
  try {
    browser = await chromium.launch({ chromiumSandbox: true, handleSIGINT: false });
  } catch (err) {
    // 固定构建的浏览器缺失、或沙箱在当前主机上起不来时明确失败，不用 --no-sandbox 蒙过去
    throw Object.assign(new Error(`浏览器无法启动：${err.message}`), { code: 'BROWSER_UNAVAILABLE' });
  }
  let closing = null;
  const closeOnce = (timeout) => (closing ??= browser.close({ timeout }).then(
    () => 'closed', () => 'unclean',
  ));
  return {
    browser,
    sandboxEnabled: true, // 见上：请求了就是真的在跑，否则这一步已经抛了
    kill: () => { closeOnce(5000); },
    close: () => closeOnce(10000),
  };
}

function structureChecks() {
  const doc = document;
  const images = [...doc.querySelectorAll('img')];
  return {
    text_length: (doc.body?.innerText ?? '').trim().length,
    headings: doc.querySelectorAll('h1,h2,h3').length,
    horizontal_overflow: doc.documentElement.scrollWidth - doc.clientWidth > 1,
    images_total: images.length,
    images_broken: images.filter((i) => i.complete && i.naturalWidth === 0).length,
    scroll_height: Math.ceil(doc.documentElement.getBoundingClientRect().height),
  };
}

/** 子页面是不透明源，外层读不到 contentDocument；用 Playwright 的 frame 句柄等待固定 ready 信号。 */
async function childFrame(page, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const frame = page.mainFrame().childFrames().find((f) => f !== page.mainFrame());
    if (frame) {
      await frame.waitForLoadState('load', { timeout: Math.max(500, deadline - Date.now()) }).catch(() => {});
      await frame.waitForFunction(() => document.readyState !== 'loading', null, { timeout: Math.max(500, deadline - Date.now()) });
      return frame;
    }
    if (Date.now() > deadline) throw new Error('等待生成子页面超时');
    await new Promise((r) => setTimeout(r, 50));
  }
}

async function runInteraction(frame, interaction) {
  const exp = interaction.expectation;
  // 动作前先记一次断言目标的命中数：目标压根不存在时，element_hidden 与 count_equals 0
  // 都会「通过」，等于空页面自我认证检查通过。
  const hitsBefore = exp ? await frame.locator(exp.target).count() : 0;
  for (const step of interaction.steps) {
    const locator = frame.locator(step.target).first();
    if (step.action === 'click') await locator.click({ timeout: 4000 });
    else if (step.action === 'fill') await locator.fill(step.value ?? '', { timeout: 4000 });
    else if (step.action === 'select') await locator.selectOption(step.value ?? '', { timeout: 4000 });
    else if (step.action === 'set_range') {
      await locator.evaluate((el, v) => {
        el.value = String(v);
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
      }, step.value ?? '');
    }
  }
  return assertExpectation(frame, exp, hitsBefore);
}

async function assertExpectation(frame, exp, hitsBefore = 0) {
  if (!exp) return { ok: true };
  const hitsAfter = await frame.locator(exp.target).count();
  if (!hitsBefore && !hitsAfter) {
    return { ok: false, invalid: true, actual: '动作前后都没有命中任何元素' };
  }
  const target = frame.locator(exp.target).first();
  if (exp.kind === 'element_visible') {
    return { ok: await target.isVisible().catch(() => false) };
  }
  if (exp.kind === 'element_hidden') {
    return { ok: !(await target.isVisible().catch(() => false)) };
  }
  if (exp.kind === 'text_visible') {
    const text = (await target.innerText({ timeout: 4000 }).catch(() => '')) ?? '';
    return { ok: text.includes(exp.value ?? ''), actual: text.slice(0, 160) };
  }
  if (exp.kind === 'value_equals') {
    const value = await target.inputValue().catch(() => null);
    return { ok: String(value) === String(exp.value), actual: value };
  }
  if (exp.kind === 'count_equals') {
    return { ok: String(hitsAfter) === String(exp.value), actual: hitsAfter };
  }
  return { ok: false, actual: `未知断言 ${exp.kind}` };
}

async function boundedScreenshots(page, outDir, prefix, maxScreenshots, viewport) {
  const files = [];
  const height = await page.evaluate(() => document.documentElement.scrollHeight);
  const stepPx = viewport.height;
  const segments = Math.min(maxScreenshots, Math.max(1, Math.ceil(height / stepPx)));
  for (let i = 0; i < segments; i += 1) {
    const buf = await page.screenshot({
      type: 'png',
      clip: { x: 0, y: Math.min(i * stepPx, Math.max(0, height - stepPx)), width: viewport.width, height: Math.min(stepPx, height) },
    });
    const file = path.join(outDir, `${prefix}-${i + 1}.png`);
    await writeFile(file, buf);
    files.push({ file: path.basename(file), bytes: buf.length, segment: i + 1, segments });
  }
  return files;
}

/**
 * 在隔离浏览器里检查最终 HTML。
 * 返回 { ok, results, violations, screenshots, launch }；任何一项不通过都不算「可以分享」。
 * onBrowser(句柄) 在浏览器起来后立刻回调，好让 runner 的墙钟到点能把它杀掉。
 */
export async function checkInBrowser({
  htmlFile, outDir, interactions = [], maxScreenshots = 6, readyTimeoutMs = 10000,
  viewports = VIEWPORTS, runInteractions = true, minBodyChars = DEFAULT_MIN_BODY_CHARS,
  onBrowser = null,
}) {
  await mkdir(outDir, { recursive: true });
  const diagnostics = [];
  const screenshots = [];
  const results = [];
  const handle = await launchChromium();
  onBrowser?.(handle);
  try {
    for (const viewport of viewports) {
      const context = await handle.browser.newContext({
        viewport: { width: viewport.width, height: viewport.height },
        deviceScaleFactor: 1,
        offline: true, // 断网下打开：正文、字体、图表、交互都不得依赖外部资源
        bypassCSP: false,
      });
      const page = await context.newPage();
      const net = [];
      const consoleErrors = [];
      const pageErrors = [];
      page.on('request', (req) => {
        const url = req.url();
        if (!url.startsWith('file:') && !url.startsWith('data:') && !url.startsWith('blob:')) net.push(url.slice(0, 200));
      });
      page.on('requestfailed', (req) => {
        const url = req.url();
        if (!url.startsWith('data:')) net.push(`failed:${url.slice(0, 200)}`);
      });
      page.on('console', (msg) => {
        if (msg.type() === 'error') consoleErrors.push(msg.text().slice(0, 300));
      });
      page.on('pageerror', (err) => pageErrors.push(String(err.message ?? err).slice(0, 300)));
      await page.goto(pathToFileURL(htmlFile).href, { waitUntil: 'load', timeout: readyTimeoutMs });
      let frame;
      try {
        frame = await childFrame(page, readyTimeoutMs);
      } catch (err) {
        diagnostics.push({ code: 'READY_TIMEOUT', message: `子页面未就绪：${err.message}` });
        await context.close();
        continue;
      }
      const structure = await frame.evaluate(structureChecks);
      const shell = await page.evaluate(() => {
        const f = document.querySelector('iframe#kb-frame');
        return {
          sandbox: f?.getAttribute('sandbox') ?? '',
          has_srcdoc: f?.hasAttribute('srcdoc') ?? false,
          outer_meta_csp: document.querySelector('meta[http-equiv="Content-Security-Policy"]')?.content ?? '',
        };
      });
      if (shell.sandbox !== 'allow-scripts') diagnostics.push({ code: 'SHELL_SANDBOX', message: `iframe sandbox 异常：${shell.sandbox}` });
      if (!shell.has_srcdoc) diagnostics.push({ code: 'SHELL_SRCDOC', message: '子页面未通过 srcdoc 建立' });
      if (structure.text_length < minBodyChars) {
        diagnostics.push({ code: 'BODY_EMPTY', message: `页面正文只有 ${structure.text_length} 字，少于要求的 ${minBodyChars} 字` });
      }
      if (structure.images_broken) diagnostics.push({ code: 'IMG_BROKEN', message: `${structure.images_broken} 张图片加载失败` });
      if (structure.horizontal_overflow) diagnostics.push({ code: 'H_OVERFLOW', message: '页面存在全局横向溢出' });
      const interactionResults = [];
      if (runInteractions) {
        for (const it of interactions) {
          try {
            const outcome = await runInteraction(frame, it);
            interactionResults.push({ id: it.id, ...outcome });
            if (outcome.invalid) {
              diagnostics.push({ code: 'ASSERTION_INVALID', message: `交互 ${it.id} 的断言无效：目标 ${it.expectation?.target ?? '（未声明）'} 在页面里不存在，证明不了交互生效` });
            } else if (!outcome.ok) {
              diagnostics.push({ code: 'INTERACTION_FAILED', message: `交互 ${it.id} 未达预期${outcome.actual === undefined ? '' : `（实际：${String(outcome.actual).slice(0, 120)}）`}` });
            }
          } catch (err) {
            interactionResults.push({ id: it.id, ok: false, actual: String(err.message ?? err).slice(0, 160) });
            diagnostics.push({ code: 'INTERACTION_ERROR', message: `交互 ${it.id} 执行出错：${String(err.message ?? err).slice(0, 160)}` });
          }
        }
      }
      if (net.length) diagnostics.push({ code: 'NETWORK_ATTEMPT', message: `断网下仍有网络请求：${net.slice(0, 3).join(' | ')}` });
      if (consoleErrors.length) diagnostics.push({ code: 'CONSOLE_ERROR', message: `控制台错误 ${consoleErrors.length} 条：${consoleErrors[0]}` });
      if (pageErrors.length) diagnostics.push({ code: 'PAGE_ERROR', message: `页面脚本错误：${pageErrors[0]}` });
      screenshots.push(...(await boundedScreenshots(page, outDir, viewport.id, Math.max(1, Math.floor(maxScreenshots / viewports.length)), viewport)));
      results.push({ viewport: viewport.id, structure, shell, interactions: interactionResults, network_attempts: net.length });
      await context.close(); // 每次检查结束即清除该任务上下文
    }
  } finally {
    // 收尾失败不该翻掉已经定局的检查结论，但要留下痕迹：没关干净的浏览器进程由容器
    // 的 pids/内存限额兜住，下一次任务照常起跑
    if (await handle.close() !== 'closed') {
      diagnostics.push({ code: 'BROWSER_STUCK', message: '浏览器收尾超时，本次检查按不通过处理', severity: 'error' });
    }
  }
  return {
    ok: diagnostics.length === 0,
    diagnostics,
    results,
    screenshots,
    launch: { sandbox_enabled: handle.sandboxEnabled },
  };
}

export { VIEWPORTS };
