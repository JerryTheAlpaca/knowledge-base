// 任务交接目录：原子领取、结果落盘与租约回传（docs/20 §14.3）。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { cp, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { claimOne, ensureSpool, runSpool, spoolPaths } from '../src/runner.mjs';
import { makeTask, pageSource } from './helpers.mjs';

async function seedTask(spoolDir, taskId, opts) {
  const ctx = await makeTask(opts);
  const target = path.join(spoolPaths(spoolDir).ready, taskId);
  await mkdir(target, { recursive: true });
  await cp(ctx.taskDir, target, { recursive: true });
  const taskPath = path.join(target, 'task.json');
  const task = JSON.parse(await readFile(taskPath, 'utf8'));
  task.task_id = taskId;
  task.lease_id = `lease-${taskId}`;
  await writeFile(taskPath, JSON.stringify(task, null, 2));
  // 输入摘要与 task.json 无关（只覆盖 page_source 与素材），无需重算
  await rm(ctx.root, { recursive: true, force: true });
  return target;
}

async function newSpool(t) {
  const dir = path.join(await mkdtemp(path.join(tmpdir(), 'kb-share-spool-')), 'spool');
  await ensureSpool(dir);
  t.after(() => rm(path.dirname(dir), { recursive: true, force: true }));
  return dir;
}

test('领取是原子的：同一任务不会被两个 runner 拿走', async (t) => {
  const spoolDir = await newSpool(t);
  await seedTask(spoolDir, 'only', { source: pageSource(), taskOverrides: { check: { enabled: false } } });
  const dirs = spoolPaths(spoolDir);
  const first = await claimOne(dirs);
  assert.equal(first.taskId, 'only');
  assert.equal(await claimOne(dirs), null, 'ready 已空时不得重复领取');
  assert.ok((await readdirOrEmpty(dirs.working)).includes('only'));
});

test('runner 处理任务：成功落 done、契约失败落 failed，结果回传租约', async (t) => {
  const spoolDir = await newSpool(t);
  await seedTask(spoolDir, 'good', { source: pageSource(), taskOverrides: { check: { enabled: false } } });
  await seedTask(spoolDir, 'bad', {
    source: pageSource({ html_body: '<main><script>bad()</script><p>正文正文正文。</p></main>' }),
    taskOverrides: { check: { enabled: false } },
  });
  const processed = await runSpool({ spoolDir, once: true });
  assert.equal(processed.length, 1, 'once 模式处理一个任务后返回');
  const dirs = spoolPaths(spoolDir);
  const result = JSON.parse(await readFile(path.join(processed[0].resultDir, 'result.json'), 'utf8'));
  assert.equal(result.task_id, processed[0].taskId);
  assert.equal(result.lease_id, `lease-${processed[0].taskId}`);
  assert.match(result.runtime_version, /^share-runtime-/);
  const inDone = await readdirOrEmpty(dirs.done);
  const inFailed = await readdirOrEmpty(dirs.failed);
  if (processed[0].ok) {
    assert.ok(inDone.includes(processed[0].taskId));
    const html = await readFile(path.join(processed[0].resultDir, 'out', 'index.html'));
    assert.ok(html.includes('iframe id="kb-frame"'));
  } else {
    assert.ok(inFailed.includes(processed[0].taskId));
    assert.ok(result.diagnostics.some((d) => d.code === 'HTML_BANNED_TAG'));
  }
  // 剩余任务仍在 ready，等下一轮
  assert.equal((await readdirOrEmpty(dirs.ready)).length, 1);
  const rest = await runSpool({ spoolDir, once: true });
  assert.equal(rest.length, 1);
  assert.notEqual(rest[0].taskId, processed[0].taskId);
});

async function readdirOrEmpty(dir) {
  return (await import('node:fs/promises')).readdir(dir).catch(() => []);
}
