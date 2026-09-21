// 任务交接目录（docs/20 §14.3）：编排器写 ready，runner 原子领取到 working，结果写 done/failed。
// 只传源文件与已选素材，不传完整 source_pack、模型 Key 或数据库。
import { chmod, mkdir, readFile, readdir, rename, rm, stat, writeFile } from 'node:fs/promises';
import path from 'node:path';

import { RenderError, renderTask } from './build.mjs';
import { checkInBrowser } from './browser.mjs';
import { loadManifest } from './manifest.mjs';

export const SPOOL_STATES = ['ready', 'working', 'done', 'failed'];
export const TASK_FILE = 'task.json';

// 信封没给 check.task_timeout_ms 时的兜底墙钟，与用户可见异常文本的限长。
const DEFAULT_TASK_TIMEOUT_MS = 120_000;
const MAX_ERROR_MESSAGE = 240;

// 交接目录靠「两个 uid、一个共享组」互通（docs/20 §14.3、审查 C-02）：本进程建的目录
// 一律 2770——同组可读写删，setgid 让里面的子目录与文件继续留在这个组里，
// 服务端 worker 才删得掉 runner 留下的产物树。改不动就忽略：那一层可能是服务端先建的，
// 按组一样能读写。
const SPOOL_DIR_MODE = 0o2770;

async function mkdirShared(dir) {
  await mkdir(dir, { recursive: true });
  await chmod(dir, SPOOL_DIR_MODE).catch(() => {});
}

const SPOOL_PATH_RE = /[^\s"'`，。；;:()]*\bspool\b[^\s"'`，。；;:()]*/gi;

/**
 * 异常文本会经诊断码进用户可见报错（run.error_detail → 前端），而 fs 错误自带
 * /spool/working/<task_id>/… 这类容器绝对路径。只在这里做一次：路径片段换成 <路径> 再限长。
 */
function userSafeMessage(err, spoolDir) {
  let text = String(err?.message ?? err ?? '未知错误').replace(SPOOL_PATH_RE, '<路径>');
  const root = String(spoolDir ?? '').replace(/[\\/]+$/, '');
  if (root.length > 1) text = text.split(root).join('<路径>');
  return text.length > MAX_ERROR_MESSAGE ? `${text.slice(0, MAX_ERROR_MESSAGE - 1)}…` : text;
}

function taskWallClockMs(check) {
  return Number.isInteger(check.task_timeout_ms) && check.task_timeout_ms > 0
    ? check.task_timeout_ms
    : DEFAULT_TASK_TIMEOUT_MS;
}

export function spoolPaths(spoolDir) {
  return {
    ...Object.fromEntries(SPOOL_STATES.map((s) => [s, path.join(spoolDir, s)])),
    tmp: path.join(spoolDir, '.tmp'),
  };
}

export async function ensureSpool(spoolDir) {
  const dirs = spoolPaths(spoolDir);
  for (const dir of Object.values(dirs)) await mkdirShared(dir);
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

/**
 * 处理一个已领取的任务，整个任务受墙钟约束（信封 check.task_timeout_ms）。
 *
 * 到点先收掉那个浏览器（handle.kill = 限时 close），在飞的 Playwright 调用会立刻 reject，
 * 再落一份结构合法的 TASK_TIMEOUT 失败结果：不然模型页面里一个死循环就永久占住唯一的
 * runner，后面所有人的任务只能排在一个没人管的进程上等各自的服务端 deadline。
 * 结果只有一方能落盘（墙钟或本体），输的那方等赢的那份结论。
 */
export async function processTask({ workingDir, taskId, spoolDir, manifest = null }) {
  const man = manifest ?? (await loadManifest());
  const dirs = await ensureSpool(spoolDir);
  const outDir = path.join(dirs.tmp, taskId);
  await rm(outDir, { recursive: true, force: true });
  await mkdirShared(outDir);            // 两层都要 2770：out/screenshots 才留在同一个组里
  await mkdirShared(path.join(outDir, 'out'));
  const task = JSON.parse(await readFile(path.join(workingDir, TASK_FILE), 'utf8'));
  const base = { schema_version: '1.0', task_id: taskId, lease_id: task.lease_id ?? null, runtime_version: man.runtimeVersion };
  const timeoutMs = taskWallClockMs(task.check ?? {});

  let outcome = null;
  const settle = (result, target) => {
    outcome ??= (async () => {
      await writeFile(path.join(outDir, 'result.json'), JSON.stringify(result, null, 2));
      return { result, resultDir: await moveInto(outDir, target, taskId) };
    })();
    return outcome;
  };

  let handle = null;
  let expired = false; // 墙钟到点时浏览器可能还没起来，起来的那一刻要立刻杀掉
  let timer = null;
  const wall = new Promise((_, reject) => {
    timer = setTimeout(() => {
      expired = true;
      handle?.kill();
      reject(Object.assign(new Error(`任务超过 ${timeoutMs}ms 墙钟仍未完成`), { code: 'TASK_TIMEOUT' }));
    }, timeoutMs);
  });
  const work = runTaskSteps({
    task, base, dirs, outDir, workingDir, spoolDir, manifest: man, settle,
    onBrowser: (h) => { handle = h; if (expired) h.kill(); },
  });
  work.catch(() => {}); // 超时后在飞的本体自己收尾，不再冒成未处理的 rejection
  try {
    return await Promise.race([work, wall]);
  } catch (err) {
    const code = err.code ?? 'TASK_ERROR';
    const result = {
      ...base,
      ok: false,
      stage: code === 'TASK_TIMEOUT' ? 'timeout' : 'runner',
      diagnostics: [{ code, severity: 'error', message: userSafeMessage(err, spoolDir) }],
      checks: [],
      screenshots: [],
    };
    return settle(result, dirs.failed);
  } finally {
    clearTimeout(timer);
  }
}

/** 构建 → 隔离检查 → 落 done/failed；检查阶段的异常原样上抛，由墙钟那一层收住。 */
async function runTaskSteps({ task, base, dirs, outDir, workingDir, spoolDir, manifest, settle, onBrowser }) {
  const htmlFile = path.join(outDir, 'out', 'index.html');
  let built;
  try {
    built = await renderTask({
      taskDir: workingDir,
      outFile: htmlFile,
      reportFile: path.join(outDir, 'build_report.json'),
      manifest,
    });
  } catch (err) {
    const diagnostics = err instanceof RenderError
      ? err.diagnostics
      : [{ code: 'BUILD_ERROR', message: userSafeMessage(err, spoolDir), severity: 'error' }];
    const result = { ...base, ok: false, stage: 'build', diagnostics: diagnostics.slice(0, 60), checks: [], screenshots: [] };
    return settle(result, dirs.failed);
  }

  const check = task.check ?? {};
  let browser = null;
  if (check.enabled !== false) {
    try {
      browser = await checkInBrowser({
        htmlFile,
        outDir: path.join(outDir, 'out', 'screenshots'),
        interactions: built.source.interactions ?? [],
        maxScreenshots: check.max_screenshots ?? 6,
        readyTimeoutMs: check.ready_timeout_ms ?? 10000,
        minBodyChars: check.min_body_chars,
        viewports: check.viewports?.length
          ? check.viewports.map(([w, h]) => ({ id: `${w}x${h}`, width: w, height: h }))
          : undefined,
        runInteractions: check.interactions !== false,
        onBrowser,
      });
    } catch (err) {
      browser = {
        ok: false,
        diagnostics: [{ code: err.code ?? 'BROWSER_ERROR', message: userSafeMessage(err, spoolDir) }],
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
  return settle(result, ok ? dirs.done : dirs.failed);
}

async function moveInto(fromDir, toRoot, taskId) {
  const to = path.join(toRoot, taskId);
  await rm(to, { recursive: true, force: true });
  await rename(fromDir, to);
  return to;
}

/**
 * 串行取任务；每个任务的浏览器在检查结束时关闭，runner 不常驻浏览器。
 * 循环体不许被任何上抛的异常带走：那会让唯一的 runner 停摆，一个任务都进不来。
 * 信封本身读不出来的任务（task.json 缺失/坏 JSON）连租约都不知道，记成失败继续下一个，
 * 由服务端按自己的 deadline 收尾。
 */
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
    try {
      const { result, resultDir } = await processTask({
        workingDir: claimed.dir, taskId: claimed.taskId, spoolDir, manifest: man,
      });
      processed.push({ taskId: claimed.taskId, ok: result.ok, resultDir });
    } catch (err) {
      processed.push({
        taskId: claimed.taskId, ok: false, resultDir: null,
        error: `${err.code ?? 'RUNNER_ERROR'}: ${userSafeMessage(err, spoolDir)}`,
      });
    }
    if (once) return processed;
  }
}
