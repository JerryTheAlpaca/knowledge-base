// 测试公共工具：在临时目录里搭一个任务包（不依赖仓库内样本，互不干扰）。
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { sha256Hex } from '../src/build.mjs';
import { loadManifest } from '../src/manifest.mjs';

export const RUNTIME_VERSION = (await loadManifest()).runtimeVersion;

export function pageSource(overrides = {}) {
  return {
    schema_version: '1.0',
    title: '测试作品',
    html_body: '<main><h1>标题</h1><p>这是一段足够长的正文，用来检查页面主体不为空。</p></main>',
    css: 'main{max-width:44rem;margin:auto}',
    javascript: '',
    dependencies: [],
    asset_ids: [],
    reference_ids: [],
    interactions: [],
    ...overrides,
  };
}

/** 1×1 透明 PNG（素材检查用），避免把二进制塞进仓库。 */
export function tinyPng() {
  return Buffer.from(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
    'base64',
  );
}

export async function makeTask({ source, assets = [], references = [], extraFiles = {}, taskOverrides = {} }) {
  const root = await mkdtemp(path.join(tmpdir(), 'kb-share-task-'));
  const taskDir = path.join(root, 'task');
  await mkdir(path.join(taskDir, 'input'), { recursive: true });
  await mkdir(path.join(taskDir, 'assets'), { recursive: true });
  const sourceBytes = Buffer.from(JSON.stringify(source), 'utf8');
  await writeFile(path.join(taskDir, 'input', 'page_source.json'), sourceBytes);
  const assetRefs = [];
  for (const asset of assets) {
    const rel = `assets/${asset.name}`;
    await writeFile(path.join(taskDir, ...rel.split('/')), asset.bytes);
    assetRefs.push({ asset_id: asset.asset_id, file: rel, mime: asset.mime, sha256: sha256Hex(asset.bytes), bytes: asset.bytes.length });
  }
  for (const [rel, bytes] of Object.entries(extraFiles)) {
    await writeFile(path.join(taskDir, ...rel.split('/')), bytes);
  }
  const declared = [['input/page_source.json', sha256Hex(sourceBytes)], ...assetRefs.map((a) => [a.file, a.sha256])];
  const task = {
    schema_version: '1.0',
    kind: 'build_check',
    task_id: 'test',
    lease_id: 'test-lease',
    runtime_version: RUNTIME_VERSION,
    page_source: 'input/page_source.json',
    assets: assetRefs,
    references,
    coverage_notes: [],
    limitations: [],
    revision_label: '测试 v1',
    input_hash: sha256Hex(Buffer.from(declared.sort().map(([rel, sha]) => `${rel}:${sha}`).join('\n'), 'utf8')),
    check: { enabled: false },
    ...taskOverrides,
  };
  await writeFile(path.join(taskDir, 'task.json'), JSON.stringify(task, null, 2));
  return { root, taskDir, outDir: path.join(root, 'out'), cleanup: () => rm(root, { recursive: true, force: true }) };
}

export function codesOf(err) {
  return (err?.diagnostics ?? []).map((d) => d.code);
}
