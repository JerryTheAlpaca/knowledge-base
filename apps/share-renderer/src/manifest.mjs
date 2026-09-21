// 固定运行环境清单（docs/20 §7.2）。
// 任务执行阶段不安装依赖、不取 CDN：可 import 的说明符与入口只来自本清单。
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const RENDERER_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export const MANIFEST_PATH = path.join(RENDERER_ROOT, 'runtime-manifest.json');

function requireString(obj, key, what) {
  const value = obj?.[key];
  if (typeof value !== 'string' || !value) throw new Error(`${what} 缺少 ${key}`);
  return value;
}

/** 读取并自检运行环境清单：登记的入口文件必须真实存在（镜像构建期即失败）。 */
export async function loadManifest(manifestPath = MANIFEST_PATH) {
  const doc = JSON.parse(await readFile(manifestPath, 'utf8'));
  const root = path.dirname(manifestPath);
  const runtimeVersion = requireString(doc, 'runtime_version', 'runtime-manifest');
  const imports = new Map();
  const licenses = [];
  const allowedRoots = new Set();
  for (const pkg of doc.packages ?? []) {
    const name = requireString(pkg, 'name', 'packages[]');
    const version = requireString(pkg, 'version', `packages[${name}]`);
    licenses.push({ name, version, license: pkg.license ?? '未知', file: pkg.license_file ?? null });
    allowedRoots.add(path.resolve(root, 'node_modules', name));
    for (const dep of pkg.transitive ?? []) {
      const depName = requireString(dep, 'name', `packages[${name}].transitive[]`);
      licenses.push({ name: depName, version: requireString(dep, 'version', `transitive[${depName}]`), license: dep.license ?? '未知', required_by: name });
      allowedRoots.add(path.resolve(root, 'node_modules', depName));
    }
    for (const item of pkg.imports ?? []) {
      const specifier = requireString(item, 'specifier', `packages[${name}].imports[]`);
      const entry = requireString(item, 'entry', `packages[${name}].imports[]`);
      const abs = path.resolve(root, entry);
      await readFile(abs); // 缺失即抛错：不允许登记不存在的入口
      imports.set(specifier, {
        specifier, name, version, entry, absPath: abs, example: item.example ?? '',
        defaultAs: item.default_as ?? null, registerAll: Boolean(item.register_all),
        pkgRoot: path.resolve(root, 'node_modules', name),
      });
    }
    for (const style of pkg.styles ?? []) {
      style.absPath = path.resolve(root, requireString(style, 'file', `packages[${name}].styles[]`));
    }
  }
  return {
    doc,
    runtimeVersion,
    imports,
    licenses,
    allowedRoots,
    stylesFor: (deps) =>
      (doc.packages ?? [])
        .filter((pkg) => (pkg.imports ?? []).some((i) => deps.includes(i.specifier)))
        .flatMap((pkg) => (pkg.styles ?? []).map((s) => ({ name: pkg.name, id: s.id, absPath: s.absPath }))),
    deniedRuntime: doc.denied_runtime ?? [],
    build: doc.build ?? {},
  };
}
