// 隔离浏览器检查（docs/20 §10.2）：检查的是最终交付文件，不是开发源文件。
// 断网上下文 + 记录任何网络尝试；固定 ready 信号；按有限交互声明做真实操作。
import { pathToFileURL } from 'node:url';
import path from 'node:path';
import { mkdir, writeFile } from 'node:fs/promises';

const VIEWPORTS = [
  { id: 'desktop', width: 1440, height: 900 },
  { id: 'mobile', width: 390, height: 844 },
];

async function launchChromium() {
  const { chromium } = await import('playwright');
  try {
    return await chromium.launch({ handleSIGINT: false });
  } catch (err) {
    // 沙箱内以非 root 运行，Chromium 自带的 sandbox 保持开启；
    // 固定构建的浏览器缺失时明确失败，不用 --no-sandbox 蒙过去
    throw Object.assign(new Error(`浏览器无法启动：${err.message}`), { code: 'BROWSER_UNAVAILABLE' });
  }
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
  return assertExpectation(frame, interaction.expectation);
}

async function assertExpectation(frame, exp) {
  if (!exp) return { ok: true };
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
    const n = await frame.locator(exp.target).count();
    return { ok: String(n) === String(exp.value), actual: n };
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
 * 返回 { ok, results, violations, screenshots }；任何一项不通过都不算「可以分享」。
 */
export async function checkInBrowser({
  htmlFile, outDir, interactions = [], maxScreenshots = 6, readyTimeoutMs = 10000,
  viewports = VIEWPORTS, runInteractions = true,
}) {
  await mkdir(outDir, { recursive: true });
  const diagnostics = [];
  const screenshots = [];
  const results = [];
  const browser = await launchChromium();
  try {
    for (const viewport of viewports) {
      const context = await browser.newContext({
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
      if (structure.text_length < 30) diagnostics.push({ code: 'BODY_EMPTY', message: '页面正文过短或为空' });
      if (structure.images_broken) diagnostics.push({ code: 'IMG_BROKEN', message: `${structure.images_broken} 张图片加载失败` });
      if (structure.horizontal_overflow) diagnostics.push({ code: 'H_OVERFLOW', message: '页面存在全局横向溢出' });
      const interactionResults = [];
      if (runInteractions) {
        for (const it of interactions) {
          try {
            const outcome = await runInteraction(frame, it);
            interactionResults.push({ id: it.id, ...outcome });
            if (!outcome.ok) {
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
    await browser.close();
  }
  return { ok: diagnostics.length === 0, diagnostics, results, screenshots };
}

export { VIEWPORTS };
