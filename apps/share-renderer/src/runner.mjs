// 任务交接目录（docs/20 §14.3）：编排器写 ready，runner 原子领取到 working，结果写 done/failed。
// 只传源文件与已选素材，不传完整 source_pack、模型 Key 或数据库。
import { mkdir, readFile, readdir, rename, rm, stat, writeFile } from 'node:fs/promises';
import path from 'node:path';

import { RenderError, renderTask } from './build.mjs';
import { checkInBrowser } from './browser.mjs';
import { loadManifest } from './manifest.mjs';

export const SPOOL_STATES = ['ready', 'working', 'done', 'failed'];
export const TASK_FILE = 'task.json';

export function spoolPaths(spoolDir) {
  return {
    ...Object.fromEntries(SPOOL_STATES.map((s) => [s, path.join(spoolDir, s)])),
    tmp: path.join(spoolDir, '.tmp'),
  };
}

export async function ensureSpool(spoolDir) {
  const dirs = spoolPaths(spoolDir);
  for (const dir of Object.values(dirs)) await mkdir(dir, { recursive: true });
  return dirs;
}

/** 原子领取：同一卷上的 rename 只有一个进程能成功。 */
export async function claimOne(dirs) {
  const entries = (await readdir(dirs.ready).catch(() => [])).sort();
  for (const name of entries) {
    const from = path.join(dirs.ready, name);
    if (!(await stat(from).then((s) => s.isDirectory()).catch(() => false))) continue;
    const to = path.join(dirs.working, name);
    try {
      await rename(from, to);
    } catch {
      continue; // 已被其他领取者拿走
    }
    return { taskId: name, dir: to };
  }
  return null;
}

/** 处理一个已领取的任务：构建 → 隔离检查 → 结果原子落到 done/failed（回传租约标识）。 */
export async function processTask({ workingDir, taskId, spoolDir, manifest = null }) {
  const man = manifest ?? (await loadManifest());
  const dirs = await ensureSpool(spoolDir);
  const outDir = path.join(dirs.tmp, taskId);
  await rm(outDir, { recursive: true, force: true });
  await mkdir(path.join(outDir, 'out'), { recursive: true });
  const task = JSON.parse(await readFile(path.join(workingDir, TASK_FILE), 'utf8'));
  const base = { schema_version: '1.0', task_id: taskId, lease_id: task.lease_id ?? null, runtime_version: man.runtimeVersion };

  let built;
  try {
    built = await renderTask({
      taskDir: workingDir,
      outFile: path.join(outDir, 'out', 'index.html'),
      reportFile: path.join(outDir, 'build_report.json'),
      manifest: man,
    });
  } catch (err) {
    const diagnostics = err instanceof RenderError
      ? err.diagnostics
      : [{ code: 'BUILD_ERROR', message: String(err.message ?? err), severity: 'error' }];
    const result = { ...base, ok: false, stage: 'build', diagnostics: diagnostics.slice(0, 60), checks: [], screenshots: [] };
    await writeFile(path.join(outDir, 'result.json'), JSON.stringify(result, null, 2));
    return { result, resultDir: await moveInto(outDir, dirs.failed, taskId) };
  }

  const check = task.check ?? {};
  let browser = null;
  if (check.enabled !== false) {
    try {
      browser = await checkInBrowser({
        htmlFile: path.join(outDir, 'out', 'index.html'),
        outDir: path.join(outDir, 'out', 'screenshots'),
        interactions: built.source.interactions ?? [],
        maxScreenshots: check.max_screenshots ?? 6,
        readyTimeoutMs: check.ready_timeout_ms ?? 10000,
        viewports: check.viewports?.length
          ? check.viewports.map(([w, h]) => ({ id: `${w}x${h}`, width: w, height: h }))
          : undefined,
        runInteractions: check.interactions !== false,
      });
    } catch (err) {
      browser = {
        ok: false,
        diagnostics: [{ code: err.code ?? 'BROWSER_ERROR', message: String(err.message ?? err) }],
        results: [],
        screenshots: [],
      };
    }
    built.report.diagnostics.push(...browser.diagnostics.map((d) => ({ ...d, severity: 'error' })));
  }

  const ok = built.report.diagnostics.every((d) => d.severity === 'warning') && (browser ? browser.ok : true);
  const result = {
    ...base,
    ok,
    stage: browser ? 'checked' : 'built',
    html_sha256: built.report.html_sha256,
    html_bytes: built.report.html_bytes,
    input_hash: built.report.input_hash,
    diagnostics: built.report.diagnostics.slice(0, 60),
    checks: browser?.results ?? [],
    screenshots: browser?.screenshots ?? [],
    build: { ...built.report, diagnostics: undefined },
  };
  await writeFile(path.join(outDir, 'result.json'), JSON.stringify(result, null, 2));
  return { result, resultDir: await moveInto(outDir, ok ? dirs.done : dirs.failed, taskId) };
}

async function moveInto(fromDir, toRoot, taskId) {
  const to = path.join(toRoot, taskId);
  await rm(to, { recursive: true, force: true });
  await rename(fromDir, to);
  return to;
}

/** 串行取任务；每个任务的浏览器在检查结束时关闭，runner 不常驻浏览器。 */
export async function runSpool({ spoolDir, once = false, idleMs = 1000 }) {
  const dirs = await ensureSpool(spoolDir);
  const man = await loadManifest();
  const processed = [];
  for (;;) {
    const claimed = await claimOne(dirs);
    if (!claimed) {
      if (once) return processed;
      await new Promise((r) => setTimeout(r, idleMs));
      continue;
    }
    const { result, resultDir } = await processTask({
      workingDir: claimed.dir, taskId: claimed.taskId, spoolDir, manifest: man,
    });
    processed.push({ taskId: claimed.taskId, ok: result.ok, resultDir });
    if (once) return processed;
  }
}
