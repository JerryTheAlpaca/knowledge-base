#!/usr/bin/env node
// runner 固定入口：不接受模型给出的目录、文件名或命令行参数（docs/20 §14.3）。
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { renderTask } from './build.mjs';
import { checkInBrowser } from './browser.mjs';
import { loadManifest } from './manifest.mjs';
import { runSpool, ensureSpool } from './runner.mjs';
import { sha256Hex } from './build.mjs';

function arg(name, fallback = null) {
  const i = process.argv.indexOf(`--${name}`);
  if (i === -1) return fallback;
  const next = process.argv[i + 1];
  return next && !next.startsWith('--') ? next : true;
}

async function readTaskDoc(taskPath) {
  return JSON.parse(await readFile(taskPath, 'utf8'));
}

/** 为样本/任务补算 input_hash（声明文件的相对路径与摘要按字典序拼接）。 */
async function seal(taskPath) {
  const taskDir = path.dirname(path.resolve(taskPath));
  const task = await readTaskDoc(taskPath);
  const files = [['input/page_source.json', task.page_source ?? 'input/page_source.json']];
  for (const asset of task.assets ?? []) files.push([asset.file, asset.file]);
  const digests = [];
  for (const [, rel] of files.sort()) {
    const bytes = await readFile(path.join(taskDir, ...String(rel).split('/')));
    digests.push(`${String(rel).replace(/\\/g, '/')}:${sha256Hex(bytes)}`);
  }
  task.input_hash = sha256Hex(Buffer.from(digests.join('\n'), 'utf8'));
  await writeFile(taskPath, `${JSON.stringify(task, null, 2)}\n`);
  console.log(JSON.stringify({ sealed: taskPath, input_hash: task.input_hash }));
}

async function buildOnly(taskPath, outDir) {
  const taskDir = path.dirname(path.resolve(taskPath));
  const out = outDir ?? path.join(taskDir, 'dist');
  await mkdir(path.join(out, 'screenshots'), { recursive: true });
  const { report, source } = await renderTask({
    taskDir,
    outFile: path.join(out, 'index.html'),
    reportFile: path.join(out, 'build_report.json'),
  });
  return { report, source, out, taskDir };
}

async function checkOnly(taskPath, outDir) {
  const task = await readTaskDoc(taskPath);
  const { report, source, out } = await buildOnly(taskPath, outDir);
  const check = task.check ?? {};
  const result = await checkInBrowser({
    htmlFile: path.join(out, 'index.html'),
    outDir: path.join(out, 'screenshots'),
    interactions: source.interactions ?? [],
    maxScreenshots: check.max_screenshots ?? 6,
    readyTimeoutMs: check.ready_timeout_ms ?? 10000,
    minBodyChars: check.min_body_chars,
    viewports: check.viewports?.length ? check.viewports.map(([w, h]) => ({ id: `${w}x${h}`, width: w, height: h })) : undefined,
  });
  const ok = result.ok && report.diagnostics.every((d) => d.severity === 'warning');
  await writeFile(path.join(out, 'check_report.json'), JSON.stringify({ ok, ...result }, null, 2));
  return { ok, out, ...result };
}

const command = process.argv[2];
const taskPath = arg('task');

if (command === 'seal') {
  await seal(taskPath);
} else if (command === 'build') {
  const { report } = await buildOnly(taskPath, arg('out'));
  console.log(JSON.stringify({ ok: true, html: report.html_sha256, bytes: report.html_bytes, diagnostics: report.diagnostics }));
} else if (command === 'check') {
  const res = await checkOnly(taskPath, arg('out'));
  console.log(JSON.stringify({ ok: res.ok, diagnostics: res.diagnostics, screenshots: res.screenshots.map((s) => s.file) }, null, 2));
  if (!res.ok) process.exitCode = 1;
} else if (command === 'runner') {
  const spool = arg('spool');
  if (!spool) throw new Error('runner 需要 --spool <目录>');
  // 交接目录由两个不同 uid 的容器共用，两端同在一个组里（kbshare，gid 950）：
  // 本进程建的文件让对端按组能读能删就够了，umask 002，不放开到任意进程可写（审查 C-02）
  process.umask(0o002);
  await ensureSpool(spool);
  const done = await runSpool({ spoolDir: spool, once: Boolean(arg('once')) });
  console.log(JSON.stringify({ processed: done }));
} else if (command === 'doctor') {
  const man = await loadManifest();
  let browser = 'unavailable';
  let detail = null;
  let sandbox = null; // 拿不到结论就是 unknown（浏览器没起来，连沙箱都没起跑）
  try {
    // 自检走真实交付件：空白页没有生成子页面，等不到 ready 信号，测不出可用与否
    const res = await checkOnly(taskPath ?? path.join(repoRoot(), 'samples', 'prose', 'task.json'), await tmpDir());
    sandbox = res.launch?.sandbox_enabled ?? null;
    browser = res.ok ? 'ok' : `degraded:${res.diagnostics.map((d) => d.code).join(',')}`;
    detail = res.diagnostics;
  } catch (err) {
    browser = `error:${err.code ?? err.message}`;
  }
  // 报的是「带沙箱的浏览器有没有真的起跑」：我们显式请求了 chromiumSandbox: true，
  // 沙箱初始化不了就是启动失败，不会静默退回 --no-sandbox。setuid helper 或
  // user namespaces 是否可用只有部署机说了算，所以 unknown/false 与 browser 不 ok
  // 同等对待，都按非 ok 退出。
  console.log(JSON.stringify({
    runtime_version: man.runtimeVersion,
    imports: [...man.imports.keys()],
    browser,
    sandbox_enabled: sandbox === null ? 'unknown' : sandbox,
    detail,
  }));
  if (browser !== 'ok' || sandbox !== true) process.exitCode = 1;
} else {
  console.log('用法：node src/cli.mjs <seal|build|check|runner|doctor> [--task 路径] [--out 目录] [--spool 目录] [--once]');
  process.exitCode = 2;
}

async function tmpDir() {
  const os = await import('node:os');
  return path.join(os.tmpdir(), `kb-share-doctor-${Date.now()}`);
}

function repoRoot() {
  return path.dirname(path.dirname(fileURLToPath(import.meta.url)));
}
