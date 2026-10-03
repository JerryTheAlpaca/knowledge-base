/**
 * 笔记文件重命名迁移（docs/23 §8.3）：旧路径 → 可读文件名。
 *
 * 只改路径与链接，不动正文：经 Obsidian 的重命名能力改名，更新受管理链接与路径索引，
 * 留可反查的恢复记录；不确定的旧路径保持原样（带 item_id 后缀或日期/标题无法确定时
 * 列入 blocked，不猜）。同一 `kb_id` 绝不出现第二份可写副本。
 *
 * 纯逻辑模块：文件访问与改名回调都通过注入，可在 Node 下测试。
 */

import type { FsLike } from "./records";
import { JsonStore } from "./records";
import { readFrontmatterValue } from "./template";
import {
  digestNotePath,
  documentsIndexPath,
  noteDateFromPath,
  renameRecordPath,
  sanitizeTitle,
  sourceNotePath,
  withConflictSuffix,
} from "./paths";
import { DocumentIndex, type DocumentIndexEntry } from "./documents";
import type { KbSettings } from "../types";

export interface RenameEntry {
  kb_id: string;
  path: string;
  kind: DocumentIndexEntry["kind"];
  title: string;
  /** 采集日期（`YYYY-MM-DD`）：Source/Digest 用它决定目录与文件名。 */
  captured_at: string | null;
}

export interface RenameItem {
  kb_id: string;
  old_path: string;
  new_path: string;
}

export interface RenamePlan {
  plan: RenameItem[];
  blocked: Array<{ kb_id: string; path: string; reason: string }>;
}

/** 参与改名与索引的目录：只同步 Source/Digest 两层。 */
export function indexRoots(settings: KbSettings): Array<{ folder: string; kind: DocumentIndexEntry["kind"]; skipDirs?: string[] }> {
  return [
    { folder: settings.sourcesFolder, kind: "source", skipDirs: ["_assets"] },
    { folder: settings.digestsFolder, kind: "digest" },
  ];
}

/**
 * 生成旧路径→新路径映射。
 *
 * 只处理仍带 `--<item_id>` 后缀或不在日期目录里的 Source/Digest 笔记；同目录同名按
 * 明确规则追加 `（2）`、`（3）`；日期或标题无法确定的列入 blocked 保持原样，不猜。
 */
export function planRenames(
  entries: RenameEntry[],
  folders: { sources: string; digests: string },
): RenamePlan {
  const plan: RenameItem[] = [];
  const blocked: RenamePlan["blocked"] = [];
  const used = new Set<string>();
  for (const entry of [...entries].sort((a, b) => a.path.localeCompare(b.path))) {
    const base = entry.path.split("/").pop() ?? entry.path;
    const hasIdSuffix = /--[A-Za-z0-9_-]+\.md$/.test(base);
    if (entry.kind !== "source" && entry.kind !== "digest") continue;
    const capturedAt = entry.captured_at ?? noteDateFromPath(entry.path);
    if (!capturedAt) {
      blocked.push({ kb_id: entry.kb_id, path: entry.path, reason: "无法确定采集日期，保持原路径" });
      continue;
    }
    const desired = entry.kind === "source"
      ? sourceNotePath(folders.sources, capturedAt, entry.title)
      : digestNotePath(folders.digests, capturedAt, entry.title);
    if (!hasIdSuffix && entry.path === desired) continue; // 已是可读名，不重复改名
    const target = used.has(desired) ? nextFreeSuffix(desired, used) : desired;
    used.add(target);
    if (target !== entry.path) plan.push({ kb_id: entry.kb_id, old_path: entry.path, new_path: target });
  }
  return { plan, blocked };
}

function nextFreeSuffix(target: string, used: Set<string>): string {
  for (let n = 2; n < 100; n++) {
    const candidate = withConflictSuffix(target, n);
    if (!used.has(candidate)) return candidate;
  }
  return withConflictSuffix(target, 100);
}

/** 只重写插件生成的完整路径链接（受管理链接），不动用户手写的其它文字。 */
export function rewriteManagedLinks(text: string, map: Map<string, string>): string {
  let out = text;
  for (const [oldPath, newPath] of map) {
    const esc = oldPath.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    out = out.replace(new RegExp(`\\[\\[${esc}(?=[\\]|#])`, "g"), `[[${newPath}`);
    out = out.replace(new RegExp(`\\[\\[${esc}\\]\\]`, "g"), `[[${newPath}]]`);
  }
  return out;
}

/** 兼容入口：只带跳转说明、不含 `kb_id`，因此不是同一文档的第二份可写副本。 */
export function renderRedirectNote(newPath: string): string {
  return [
    "---",
    "kb_type: redirect",
    "---",
    "",
    `> 这篇笔记已由 Golden-Rose-Inbox 改名为可阅读文件名，内容在 [[${newPath}]]。`,
    "> 本兼容入口不承载正文；确认没有链接指向它之后可以直接删除。",
    "",
  ].join("\n");
}

export interface RenameOutcome {
  renamed: RenameItem[];
  failed: Array<{ kb_id: string; old_path: string; reason: string }>;
  blocked: RenamePlan["blocked"];
  record_path: string;
  /** 全部改名成功才算完成；有失败时不得按“已迁移/已导入成功”处理。 */
  completed: boolean;
}

/**
 * 执行改名：经 Obsidian 的重命名能力（`rename` 由 main.ts 用 fileManager 提供），
 * 更新受管理链接与路径索引，并保存可反查的恢复记录。
 */
export async function runRenameMigration(
  fs: FsLike,
  settings: KbSettings,
  opts: {
    rename: (from: string, to: string) => Promise<void>;
    entries?: RenameEntry[];
    keepCompatEntries?: boolean;
  },
): Promise<RenameOutcome> {
  const docs = new DocumentIndex(fs, documentsIndexPath(settings.systemFolder));
  await docs.ensure(indexRoots(settings));
  const gathered = opts.entries ?? await gatherRenameEntries(fs, await docs.entries());
  const { plan, blocked } = planRenames(gathered, {
    sources: settings.sourcesFolder, digests: settings.digestsFolder,
  });
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const recordPath = renameRecordPath(settings.systemFolder, stamp);
  const renamed: RenameItem[] = [];
  const failed: RenameOutcome["failed"] = [];
  const map = new Map<string, string>();

  for (const item of plan) {
    try {
      if (await fs.exists(item.new_path)) throw new Error("目标路径已存在");
      await opts.rename(item.old_path, item.new_path);
      map.set(item.old_path, item.new_path);
      renamed.push(item);
      await docs.move(item.kb_id, item.new_path);
      if (opts.keepCompatEntries !== false) await fs.write(item.old_path, renderRedirectNote(item.new_path));
    } catch (err) {
      // 单项失败不影响已保存原文，也不把该项标为已迁移
      failed.push({ kb_id: item.kb_id, old_path: item.old_path, reason: err instanceof Error ? err.message : String(err) });
    }
  }

  if (map.size) await rewriteLinksInKnownNotes(fs, docs, map);

  const completed = failed.length === 0;
  await new JsonStore<Record<string, unknown>>(fs, recordPath, () => ({})).write({
    kind: "rename-migration",
    created_at: new Date().toISOString(),
    completed,
    renamed,
    failed,
    blocked,
    reverse: renamed.map((r) => ({ kb_id: r.kb_id, from: r.new_path, to: r.old_path })),
    compat_entries: opts.keepCompatEntries !== false ? renamed.map((r) => r.old_path) : [],
  });
  if (!completed) await docs.rebuild(indexRoots(settings));
  return { renamed, failed, blocked, record_path: recordPath, completed };
}

/** 改名后更新引用旧路径的受管理链接。 */
async function rewriteLinksInKnownNotes(fs: FsLike, docs: DocumentIndex, map: Map<string, string>): Promise<void> {
  for (const entry of await docs.entries()) {
    if (entry.kind === "other" || !(await fs.exists(entry.path))) continue;
    const text = await fs.read(entry.path);
    const next = rewriteManagedLinks(text, map);
    if (next !== text) await fs.write(entry.path, next);
  }
}

/** 从 frontmatter 收集改名输入：标题与采集日期都以文档身份为准。 */
export async function gatherRenameEntries(fs: FsLike, entries: DocumentIndexEntry[]): Promise<RenameEntry[]> {
  const out: RenameEntry[] = [];
  for (const entry of entries) {
    if (entry.kind === "other" || !(await fs.exists(entry.path))) continue;
    const text = await fs.read(entry.path);
    const heading = /^#\s+(.+)$/m.exec(text)?.[1]?.trim() ?? entry.title;
    const title = entry.kind === "digest" ? heading.replace(/：提炼$/, "").trim() : heading;
    out.push({
      kb_id: entry.kb_id,
      path: entry.path,
      kind: entry.kind,
      title: sanitizeTitle(title || entry.title),
      captured_at: readFrontmatterValue(text, "kb_captured_at") ?? noteDateFromPath(entry.path),
    });
  }
  return out;
}

/** 恢复：按恢复记录的反向映射退回原位（同一改名回调，不复制正文）。 */
export async function revertRenameMigration(
  fs: FsLike,
  settings: KbSettings,
  record: { renamed?: Array<{ kb_id: string; old_path: string; new_path: string }>; compat_entries?: string[] },
  rename: (from: string, to: string) => Promise<void>,
): Promise<{ restored: number; failed: Array<{ kb_id: string; reason: string }> }> {
  const docs = new DocumentIndex(fs, documentsIndexPath(settings.systemFolder));
  await docs.ensure(indexRoots(settings));
  let restored = 0;
  const failed: Array<{ kb_id: string; reason: string }> = [];
  const back = new Map<string, string>();
  for (const item of [...(record.renamed ?? [])].reverse()) {
    try {
      for (const compat of record.compat_entries ?? []) {
        if (compat === item.old_path && (await fs.exists(compat))) await fs.remove(compat);
      }
      if (await fs.exists(item.old_path)) throw new Error("原路径已被占用，不能自动恢复");
      await rename(item.new_path, item.old_path);
      back.set(item.new_path, item.old_path);
      await docs.move(item.kb_id, item.old_path);
      restored += 1;
    } catch (err) {
      failed.push({ kb_id: item.kb_id, reason: err instanceof Error ? err.message : String(err) });
    }
  }
  if (back.size) await rewriteLinksInKnownNotes(fs, docs, back);
  return { restored, failed };
}
