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

// 够长的正文：检查阶段的正文下限默认 300 字，别让 BODY_EMPTY 混进别的用例里
const LONG_TEXT = '这段正文用来占够检查阶段要求的最少字符数，内容本身没有额外含义，只是空页面不能算通过。';
const longBody = (extra = '') => `<main><h1>标题</h1><p>${LONG_TEXT.repeat(10)}</p>${extra}</main>`;

const CLICK_JS = "document.getElementById('b').addEventListener('click', () => {"
  + "document.getElementById('o').textContent = '已点击';});";

/** 交互声明本身合法，但断言的目标在页面里根本不存在（动作前后都 0 命中）。 */
test('无效断言不算通过：element_hidden 不能被压根不存在的目标自我认证', async (t) => {
  const spoolDir = await newSpool(t);
  await seedTask(spoolDir, 'ghost', {
    source: pageSource({
      title: '断言目标不存在',
      html_body: longBody('<button id="b" type="button">点一下</button><output id="o"></output>'),
      javascript: CLICK_JS,
      interactions: [{
        id: 'hide-ghost',
        description: '点一下之后 #ghost 应当不可见',
        steps: [{ action: 'click', target: '#b' }],
        expectation: { kind: 'element_hidden', target: '#ghost' },
      }],
    }),
    taskOverrides: { check: { enabled: true, max_screenshots: 2, viewports: [[1280, 900]] } },
  });
  const dirs = spoolPaths(spoolDir);
  const processed = await runSpool({ spoolDir, once: true });
  assert.equal(processed.length, 1);
  assert.equal(processed[0].ok, false, '断言无效的交互不得算检查通过');
  assert.ok((await readdirOrEmpty(dirs.failed)).includes('ghost'));
  const result = JSON.parse(await readFile(path.join(processed[0].resultDir, 'result.json'), 'utf8'));
  const codes = result.diagnostics.map((d) => d.code);
  assert.ok(codes.includes('ASSERTION_INVALID'), `实际诊断：${codes.join(',')}`);
  assert.ok(!codes.includes('INTERACTION_FAILED'), '目标不存在不该被当成"交互未达预期"的通过式结论');
  assert.ok(!codes.includes('BODY_EMPTY'), '正文长度已达标，用例只验断言有效性');
  assert.equal(result.checks[0].interactions[0].ok, false);
});

test('正文下限按信封给的 min_body_chars 判，不再用 30 字', async (t) => {
  const spoolDir = await newSpool(t);
  await seedTask(spoolDir, 'thin', {
    source: pageSource({ title: '正文很短', html_body: '<main><h1>标题</h1><p>只有这一句。</p></main>' }),
    taskOverrides: { check: { enabled: true, max_screenshots: 2, viewports: [[1280, 900]] } },
  });
  const processed = await runSpool({ spoolDir, once: true });
  assert.equal(processed[0].ok, false);
  const result = JSON.parse(await readFile(path.join(processed[0].resultDir, 'result.json'), 'utf8'));
  const body = result.diagnostics.find((d) => d.code === 'BODY_EMPTY');
  assert.ok(body, '默认 300 字下限没生效');
  assert.match(body.message, /300/);
});

test('墙钟到点：杀掉浏览器落 TASK_TIMEOUT，后面的任务照常处理', async (t) => {
  const spoolDir = await newSpool(t);
  await seedTask(spoolDir, 'a-loop', {
    source: pageSource({
      title: '卡死的页面',
      html_body: longBody(),
      // 顶层死循环会连 load 事件都等不到（那是 READY_TIMEOUT）；这里要的是页面已经就绪、
      // 检查读到一半时把浏览器拖死的情形
      javascript: 'setTimeout(() => { let n = 0; while (true) { n += 1; } }, 30);',
    }),
    taskOverrides: { check: { enabled: true, task_timeout_ms: 3000, max_screenshots: 2, viewports: [[1280, 900]] } },
  });
  await seedTask(spoolDir, 'b-after', {
    source: pageSource({ title: '排在后面的任务' }),
    taskOverrides: { check: { enabled: false } },
  });
  const dirs = spoolPaths(spoolDir);
  const first = await runSpool({ spoolDir, once: true });
  assert.equal(first[0].taskId, 'a-loop');
  assert.equal(first[0].ok, false);
  const result = JSON.parse(await readFile(path.join(first[0].resultDir, 'result.json'), 'utf8'));
  assert.equal(result.ok, false);
  assert.equal(result.lease_id, 'lease-a-loop', '超时结果也要带回租约标识，服务端才认这份结论');
  assert.deepEqual((result.diagnostics ?? []).map((d) => d.code), ['TASK_TIMEOUT']);
  assert.ok((await readdirOrEmpty(dirs.failed)).includes('a-loop'));
  // 唯一的 runner 没有被卡死的页面带走：下一个任务照常领取
  const second = await runSpool({ spoolDir, once: true });
  assert.equal(second.length, 1);
  assert.equal(second[0].taskId, 'b-after');
  assert.equal(second[0].ok, true, `后续任务被前一个拖累了：${JSON.stringify(second[0])}`);
});

test('信封读不出来的任务记成失败，runner 进程不死也不带路径', async (t) => {
  const spoolDir = await newSpool(t);
  await seedTask(spoolDir, 'a-broken', { source: pageSource() });
  await rm(path.join(spoolPaths(spoolDir).ready, 'a-broken', 'task.json'), { force: true });
  await seedTask(spoolDir, 'b-after', { source: pageSource(), taskOverrides: { check: { enabled: false } } });
  const dirs = spoolPaths(spoolDir);
  const first = await runSpool({ spoolDir, once: true });
  assert.equal(first.length, 1);
  const broken = first[0];
  assert.equal(broken.taskId, 'a-broken');
  assert.equal(broken.ok, false);
  assert.equal(broken.resultDir, null);
  assert.ok(broken.error, '异常任务要留下可读的失败原因');
  assert.ok(!broken.error.includes(spoolDir), `报错里带着交接目录绝对路径：${broken.error}`);
  assert.ok(broken.error.length <= 240, `异常文本没限长：${broken.error.length}`);
  assert.ok((await readdirOrEmpty(dirs.ready)).includes('b-after'), '坏信封不该把后面的任务一起吃掉');
  const second = await runSpool({ spoolDir, once: true });
  assert.equal(second[0].taskId, 'b-after');
  assert.equal(second[0].ok, true, '坏信封之后的任务没能处理');
});
