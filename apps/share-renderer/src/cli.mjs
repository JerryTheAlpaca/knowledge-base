#!/usr/bin/env node
// runner 固定入口：不接受模型给出的目录、文件名或命令行参数（docs/20 §14.3）。
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import path from 'node:path';

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
  await ensureSpool(spool);
  const done = await runSpool({ spoolDir: spool, once: Boolean(arg('once')) });
  console.log(JSON.stringify({ processed: done }));
} else if (command === 'doctor') {
  const man = await loadManifest();
  let browser = 'unavailable';
  try {
    const probe = await checkInBrowser({
      htmlFile: await fixtureBlank(), outDir: await tmpDir(), interactions: [], maxScreenshots: 1,
    });
    browser = probe.ok ? 'ok' : `degraded:${probe.diagnostics.map((d) => d.code).join(',')}`;
  } catch (err) {
    browser = `error:${err.code ?? err.message}`;
  }
  console.log(JSON.stringify({ runtime_version: man.runtimeVersion, imports: [...man.imports.keys()], browser }));
} else {
  console.log('用法：node src/cli.mjs <seal|build|check|runner|doctor> [--task 路径] [--out 目录] [--spool 目录] [--once]');
  process.exitCode = 2;
}

async function tmpDir() {
  const os = await import('node:os');
  return path.join(os.tmpdir(), `kb-share-doctor-${Date.now()}`);
}

async function fixtureBlank() {
  const { writeFile: wf, mkdir: mk } = await import('node:fs/promises');
  const dir = await tmpDir();
  await mk(dir, { recursive: true });
  const file = path.join(dir, 'blank.html');
  await wf(file, '<!doctype html><meta charset="utf-8"><title>blank</title><p>runner 自检占位页面</p>');
  return file;
}
