// 固定顺序构建：源 JSON → 解析检查 → esbuild 编译 → 内联 → 子文档 + 可信外层（docs/20 §7.4）。
import { createHash } from 'node:crypto';
import { readFile, readdir, writeFile, mkdir } from 'node:fs/promises';
import path from 'node:path';

import { checkCss, checkHtml, checkJs, checkPageSource, DEFAULT_LIMITS } from './checks.mjs';
import { loadManifest, RENDERER_ROOT } from './manifest.mjs';
import { buildChildDoc, buildOuterDoc, childCsp, childScript, sha256Base64 } from './shell.mjs';

export class RenderError extends Error {
  constructor(diagnostics) {
    const hard = diagnostics.filter((d) => d.severity !== 'warning');
    super(hard.map((d) => `${d.code}: ${d.message}`).join('；') || '构建失败');
    this.diagnostics = diagnostics;
  }
}

export function sha256Hex(buf) {
  return createHash('sha256').update(buf).digest('hex');
}

const SUPPORTED_IMAGE_MIMES = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif']);

function sniffImageMime(bytes) {
  if (bytes.length < 13) return null;
  if (bytes.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) return 'image/png';
  if (bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff) return 'image/jpeg';
  if (bytes.subarray(0, 4).toString('latin1') === 'RIFF' && bytes.subarray(8, 12).toString('latin1') === 'WEBP') return 'image/webp';
  const head = bytes.subarray(0, 6).toString('latin1');
  if (head === 'GIF87a' || head === 'GIF89a') return 'image/gif';
  return null;
}

function normalizeRel(value) {
  const rel = String(value ?? '').replace(/\\/g, '/').replace(/^\.\//, '');
  if (!rel || rel.startsWith('/') || rel.split('/').some((p) => !p || p === '.' || p === '..')) return null;
  return rel;
}

/** 任务目录只接受声明过的普通文件；不跟随软链接（docs/20 §14.3）。 */
export async function inspectTaskDir(taskDir, task) {
  const errors = [];
  const declared = new Map([['input/page_source.json', 'page_source']]);
  for (const asset of task.assets ?? []) {
    const rel = normalizeRel(asset.file);
    if (!rel) {
      errors.push({ code: 'TASK_FILE_ESCAPE', message: `素材路径非法：${asset.file}` });
      continue;
    }
    declared.set(rel, 'asset');
  }
  const digests = new Map();
  async function walk(rel) {
    const entries = await readdir(path.join(taskDir, ...rel.split('/')), { withFileTypes: true });
    for (const entry of entries) {
      const childRel = rel ? `${rel}/${entry.name}` : entry.name;
      if (entry.isSymbolicLink()) {
        errors.push({ code: 'TASK_FILE_SYMLINK', message: `任务目录不允许软链接：${childRel}` });
        continue;
      }
      if (entry.isDirectory()) {
        if (childRel === 'dist') continue; // 构建产物目录不参与输入校验
        if (childRel !== 'input' && childRel !== 'assets') {
          errors.push({ code: 'TASK_DIR_UNEXPECTED', message: `任务目录不允许的子目录：${childRel}` });
          continue;
        }
        await walk(childRel);
        continue;
      }
      if (!entry.isFile() || childRel === 'task.json') continue;
      if (!declared.has(childRel)) {
        errors.push({ code: 'TASK_FILE_UNDECLARED', message: `任务目录含未声明文件：${childRel}` });
        continue;
      }
      const bytes = await readFile(path.join(taskDir, ...childRel.split('/')));
      digests.set(childRel, sha256Hex(bytes));
    }
  }
  await walk('');
  for (const rel of declared.keys()) {
    if (!digests.has(rel)) errors.push({ code: 'TASK_FILE_MISSING', message: `任务文件缺失：${rel}` });
  }
  const inputHash = sha256Hex(
    Buffer.from([...digests.entries()].sort().map(([rel, sha]) => `${rel}:${sha}`).join('\n'), 'utf8'),
  );
  if (task.input_hash && task.input_hash !== inputHash) {
    errors.push({ code: 'INPUT_HASH_MISMATCH', message: '输入摘要与任务信封不一致（快照可能被改动）' });
  }
  return { errors, inputHash };
}

/** 素材 → data: URL；只接受声明且摘要匹配、类型可核对的栅格图片。 */
async function loadAssets(task, taskDir, limits) {
  const errors = [];
  const byId = new Map();
  for (const asset of task.assets ?? []) {
    const id = String(asset.asset_id ?? '');
    const rel = normalizeRel(asset.file);
    if (!id || !rel) continue;
    let bytes;
    try {
      bytes = await readFile(path.join(taskDir, ...rel.split('/')));
    } catch {
      errors.push({ code: 'ASSET_MISSING', message: `素材文件读不到：${id}` });
      continue;
    }
    if (bytes.length > limits.max_asset_bytes) {
      errors.push({ code: 'ASSET_TOO_LARGE', message: `素材超过单文件上限：${id}` });
      continue;
    }
    const declared = String(asset.mime ?? '');
    const sniffed = sniffImageMime(bytes);
    if (!sniffed || !SUPPORTED_IMAGE_MIMES.has(declared) || sniffed !== declared) {
      errors.push({ code: 'ASSET_MIME', message: `素材类型不可信或未支持：${id}（声明 ${declared || '空'}）` });
      continue;
    }
    if (asset.sha256 && asset.sha256 !== sha256Hex(bytes)) {
      errors.push({ code: 'ASSET_DIGEST_MISMATCH', message: `素材摘要不一致：${id}` });
      continue;
    }
    byId.set(id, { dataUrl: `data:${sniffed};base64,${bytes.toString('base64')}`, bytes: bytes.length, mime: sniffed });
  }
  return { errors, byId };
}

/** 依赖自带样式内联：woff2 转 data: URL，其他远程引用一律丢弃。 */
async function inlineStylePackage(absPath) {
  const css = await readFile(absPath, 'utf8');
  const dir = path.dirname(absPath);
  const inner = (raw) => String(raw).trim().replace(/^['"]|['"]$/g, '');
  const replacement = new Map();
  for (const match of css.matchAll(/url\(([^)]*)\)/g)) {
    const value = inner(match[1]);
    if (replacement.has(value) || value.startsWith('data:')) continue;
    const rel = normalizeRel(value.split('?')[0]);
    if (!rel || !rel.endsWith('.woff2')) {
      replacement.set(value, 'url(data:font/woff2;base64,)');
      continue;
    }
    try {
      const bytes = await readFile(path.join(dir, ...rel.split('/')));
      replacement.set(value, `url(data:font/woff2;base64,${bytes.toString('base64')})`);
    } catch {
      replacement.set(value, 'url(data:font/woff2;base64,)');
    }
  }
  return css.replace(/url\(([^)]*)\)/g, (whole, raw) => {
    const value = inner(raw);
    return replacement.get(value) ?? (value.startsWith('data:') ? whole : 'url(data:font/woff2;base64,)');
  });
}

/** 用固定 esbuild 配置编译页面脚本；依赖解析只认清单登记过的包。 */
async function compileScript({ javascript, dependencies, manifest }) {
  const { build } = await import('esbuild');
  const roots = [...manifest.allowedRoots];
  const insideAllowed = (target) => {
    if (!target) return false;
    const abs = path.resolve(target);
    return roots.some((root) => abs === root || abs.startsWith(`${root}${path.sep}`));
  };
  const plugin = {
    name: 'kb-fixed-runtime',
    setup(b) {
      b.onResolve({ filter: /.*/ }, (args) => {
        if (args.namespace === 'kb-runtime') {
          // 虚拟 shim 只允许指向登记入口的绝对路径，其余交给默认解析
          if (path.isAbsolute(args.path)) return { path: args.path, namespace: 'file' };
          return undefined;
        }
        if (manifest.imports.has(args.path)) return { path: args.path, namespace: 'kb-runtime' };
        // 只有已登记包自身的内部解析可以继续，其他一律拒绝
        if (insideAllowed(args.importer)) return undefined;
        return { errors: [{ text: `不允许的依赖：${args.path}` }] };
      });
      b.onLoad({ filter: /.*/, namespace: 'kb-runtime' }, (args) => {
        const hit = manifest.imports.get(args.path);
        if (!hit) return { errors: [{ text: `不允许的依赖：${args.path}` }] };
        if (!dependencies.includes(hit.specifier)) {
          return { errors: [{ text: `未在 dependencies 中声明：${hit.specifier}` }] };
        }
        const entry = JSON.stringify(hit.absPath);
        // 构建器固定的兼容垫片：chart.js 的 ESM 入口没有默认导出，按清单映射
        const register = hit.registerAll
          ? `import { ${hit.defaultAs ?? 'Chart'} as __KbChart, registerables as __KbRegisterables } from ${entry};\n__KbChart.register(...__KbRegisterables);\n`
          : '';
        const shim = hit.defaultAs
          ? `${register}export * from ${entry};\nexport { ${hit.defaultAs} as default } from ${entry};\n`
          : `${register}export * from ${entry};\n`;
        return { contents: shim, loader: 'js', resolveDir: RENDERER_ROOT };
      });
    },
  };
  let result;
  try {
    result = await build({
      stdin: { contents: javascript || '', resolveDir: RENDERER_ROOT, loader: 'js', sourcefile: 'page.js' },
      absWorkingDir: RENDERER_ROOT,
      bundle: true,
      format: 'iife',
      platform: 'browser',
      target: manifest.build?.target ?? 'es2020',
      splitting: false,
      sourcemap: false,
      minify: false,
      legalComments: 'none',
      metafile: true,
      write: false,
      plugins: [plugin],
    });
  } catch (err) {
    const list = (err?.errors ?? []).map((e) => ({
      code: 'BUILD_ERROR',
      message: `${e.location?.file ?? ''}${e.location?.line ? `:${e.location.line}` : ''} ${e.text}${e.notes?.length ? `（${e.notes.map((n) => n.text).join('；')}）` : ''}`.trim(),
    }));
    return { code: '', errors: list.length ? list : [{ code: 'BUILD_ERROR', message: String(err?.message ?? err) }], inputCount: 0 };
  }
  const errors = [];
  const entries = Object.entries(result.metafile.outputs ?? {});
  if (entries.length !== 1) errors.push({ code: 'BUILD_OUTPUTS', message: `期望单个产物，实际 ${entries.length} 个` });
  const [, out] = entries[0] ?? [null, { imports: [] }];
  if ((out.imports ?? []).length) errors.push({ code: 'BUILD_DYNAMIC_DEPS', message: '构建后仍有动态依赖' });
  const inputs = Object.keys(result.metafile.inputs ?? {});
  for (const input of inputs) {
    // 只允许：页面自己的脚本、构建器虚拟垫片、登记包及其声明过的传递依赖
    const allowed = input === 'page.js' || input.startsWith('kb-runtime:') || insideAllowed(path.resolve(RENDERER_ROOT, input));
    if (!allowed) errors.push({ code: 'BUILD_UNEXPECTED_INPUT', message: `构建引入了未登记的文件：${input}` });
  }
  return { code: result.outputFiles?.[0]?.text ?? '', errors, inputCount: inputs.length };
}

/** 把 data-asset 换成 data: URL；成品不保留任何其他图片来源。 */
function applyAssets(html, assetMap) {
  return html.replace(/<img\b[^>]*>/gi, (tag) => {
    const match = /\bdata-asset\s*=\s*(["'])(.*?)\1/i.exec(tag);
    if (!match) return tag;
    const asset = assetMap.get(match[2]);
    if (!asset) return tag;
    return `${tag.slice(0, -1).replace(/\s*\/$/, '')} src="${asset.dataUrl}">`;
  });
}

/**
 * 构建一个任务。静态契约不通过即抛 RenderError：不产出半成品，也不标「可以分享」。
 * 返回最终单文件字节与机器可读报告（docs/20 §10.1）。
 */
export async function renderTask({ taskDir, outFile = null, reportFile = null, manifest = null }) {
  const startedAt = Date.now();
  const man = manifest ?? (await loadManifest());
  const task = JSON.parse(await readFile(path.join(taskDir, 'task.json'), 'utf8'));
  const limits = { ...DEFAULT_LIMITS, ...(task.limits ?? {}) };
  const diagnostics = [];
  const fail = (list = []) => {
    if (!list.length) return;
    diagnostics.push(...list);
    throw new RenderError(diagnostics);
  };
  const warn = (list) => diagnostics.push(...list.map((d) => ({ ...d, severity: 'warning' })));

  if (task.schema_version !== '1.0') fail([{ code: 'VERSION_UNSUPPORTED', message: `任务信封 schema_version 不支持：${task.schema_version}` }]);
  if (task.runtime_version && task.runtime_version !== man.runtimeVersion) {
    fail([{ code: 'RUNTIME_VERSION', message: `任务要求 ${task.runtime_version}，本 runner 为 ${man.runtimeVersion}` }]);
  }
  const inspected = await inspectTaskDir(taskDir, task);
  if (inspected.errors.length) fail(inspected.errors);

  const sourcePath = normalizeRel(task.page_source ?? 'input/page_source.json');
  const raw = JSON.parse(await readFile(path.join(taskDir, ...sourcePath.split('/')), 'utf8'));
  const { errors: schemaErrors, source } = checkPageSource(raw, {
    limits,
    allowedImports: new Set(man.imports.keys()),
    knownAssets: new Set((task.assets ?? []).map((a) => String(a.asset_id))),
    knownReferences: new Set((task.references ?? []).map((r) => String(r.ref_id))),
  });
  fail(schemaErrors);

  const assets = await loadAssets(task, taskDir, limits);
  fail(assets.errors);
  const html = checkHtml(source.html_body, { assetIds: new Set(assets.byId.keys()) });
  warn(html.warnings);
  fail(html.errors);
  fail(checkCss(source.css).errors);
  fail(checkJs(source.javascript, { allowedImports: new Set(source.dependencies) }).errors);

  const compiled = await compileScript({ javascript: source.javascript, dependencies: source.dependencies, manifest: man });
  fail(compiled.errors);

  const extraCssParts = [];
  for (const style of man.stylesFor(source.dependencies)) {
    extraCssParts.push(await inlineStylePackage(style.absPath));
  }
  const childScriptText = childScript(compiled.code);
  const scriptHash = sha256Base64(Buffer.from(childScriptText, 'utf8'));
  const childDoc = buildChildDoc({
    title: source.title,
    htmlBody: applyAssets(source.html_body, assets.byId),
    css: source.css,
    extraCss: extraCssParts.join('\n'),
    script: childScriptText,
    csp: childCsp({ scriptHash }),
  });
  const htmlText = buildOuterDoc({
    title: source.title,
    childDoc,
    childScriptHash: scriptHash,
    references: task.references ?? [],
    coverageNotes: task.coverage_notes ?? [],
    limitations: task.limitations ?? [],
    revisionLabel: task.revision_label ?? '',
    runtimeVersion: man.runtimeVersion,
  });
  const htmlBuf = Buffer.from(htmlText, 'utf8');
  if (htmlBuf.length > limits.max_html_bytes) {
    fail([{ code: 'HTML_TOO_LARGE', message: `成品 ${(htmlBuf.length / 1048576).toFixed(2)} MiB 超过上限 ${limits.max_html_bytes}` }]);
  }
  if (outFile) {
    await mkdir(path.dirname(outFile), { recursive: true });
    await writeFile(outFile, htmlBuf);
  }
  const report = {
    schema_version: '1.0',
    kind: 'share_build_report',
    runtime_version: man.runtimeVersion,
    title: source.title,
    built_at: new Date().toISOString(),
    duration_ms: Date.now() - startedAt,
    html_sha256: sha256Hex(htmlBuf),
    html_bytes: htmlBuf.length,
    input_hash: inspected.inputHash,
    dependencies: source.dependencies.map((spec) => ({
      specifier: spec, package: man.imports.get(spec)?.name ?? null, version: man.imports.get(spec)?.version ?? null,
    })),
    licenses: man.licenses,
    assets: [...assets.byId.entries()].map(([id, a]) => ({ asset_id: id, mime: a.mime, bytes: a.bytes })),
    reference_ids: (task.references ?? []).map((r) => String(r.ref_id)),
    interactions: (source.interactions ?? []).length,
    build: {
      bundle_bytes: Buffer.byteLength(childScriptText, 'utf8'),
      script_sha256_b64: scriptHash,
      esbuild_inputs: compiled.inputCount,
      dom_nodes: html.nodeCount,
    },
    diagnostics,
  };
  if (reportFile) {
    await mkdir(path.dirname(reportFile), { recursive: true });
    await writeFile(reportFile, JSON.stringify(report, null, 2));
  }
  return { html: htmlBuf, report, source };
}
