/**
 * 文档索引：`kb_id → 笔记路径`（docs/24 §8；docs/23 §7.2）。
 *
 * 文件名不再携带长 item_id，所以路径不能每次同步按目录重猜：新建即登记，
 * 索引丢失时扫描 frontmatter 重建。文档身份只在 frontmatter（`kb_id`），
 * 用户改名或移动后仍按身份识别；同一 `kb_id` 出现在两个文件时**报告身份冲突并
 * 保留两份**，绝不任选其一覆盖。
 *
 * 纯逻辑模块：文件访问通过 `FsLike` 注入，可独立测试。
 */

import type { FsLike } from "./records";
import { noteTitleFromPath } from "./paths";
import { readFrontmatterValue } from "./template";

export const DOCUMENT_INDEX_SCHEMA_VERSION = "1.0";

export type DocumentKind = "source" | "digest" | "knowledge" | "other";

export interface DocumentIndexEntry {
  kb_id: string;
  path: string;
  kind: DocumentKind;
  item_id: string | null;
  title: string;
  updated_at: string;
}

export interface DocumentConflict {
  kb_id: string;
  paths: string[];
  detected_at: string;
}

export interface DocumentIndexDoc {
  schema_version: string;
  built_at: string;
  docs: Record<string, DocumentIndexEntry>;
  conflicts: DocumentConflict[];
}

function emptyDoc(): DocumentIndexDoc {
  return { schema_version: DOCUMENT_INDEX_SCHEMA_VERSION, built_at: new Date().toISOString(), docs: {}, conflicts: [] };
}

/** 递归列出目录下的 .md，可跳过指定子目录（如 `01 Sources/_assets`）。 */
export async function listMarkdown(fs: FsLike, dir: string, skipDirs: string[] = []): Promise<string[]> {
  const out: string[] = [];
  const skipped = (name: string) => skipDirs.some((s) => name === s || name.startsWith(`${s}/`));
  if (skipped(dir)) return out;
  const walk = async (cur: string): Promise<void> => {
    for (const f of await fs.list(cur)) {
      if (f.endsWith(".md") && !skipped(f)) out.push(f);
    }
    for (const child of await fs.listDirs(cur)) {
      const base = child.split("/").pop() ?? child;
      if (skipped(base)) continue;
      await walk(child);
    }
  };
  await walk(dir);
  return out.sort();
}

/** 从笔记 frontmatter 还原文档身份；没有 `kb_id` 的普通笔记不进入索引。 */
export function documentEntryFromFrontmatter(text: string, path: string): DocumentIndexEntry | null {
  const kbId = readFrontmatterValue(text, "kb_id");
  if (!kbId) return null;
  const rawType = readFrontmatterValue(text, "kb_type") ?? "";
  const kind: DocumentKind = rawType === "source" ? "source"
    : rawType === "digest" ? "digest"
    : rawType === "knowledge" ? "knowledge" : "other";
  const heading = /^#\s+(.+)$/m.exec(text)?.[1]?.trim();
  return {
    kb_id: kbId,
    path,
    kind,
    item_id: readFrontmatterValue(text, "kb_item_id"),
    title: heading ?? noteTitleFromPath(path),
    updated_at: readFrontmatterValue(text, "kb_reviewed_at") ?? readFrontmatterValue(text, "kb_captured_at") ?? "",
  };
}

export class DocumentIndex {
  private doc: DocumentIndexDoc | null = null;

  constructor(private fs: FsLike, private file: string) {}

  private async load(): Promise<DocumentIndexDoc | null> {
    if (this.doc) return this.doc;
    if (!(await this.fs.exists(this.file))) return null;
    try {
      const parsed = JSON.parse(await this.fs.read(this.file)) as DocumentIndexDoc;
      if (parsed.schema_version !== DOCUMENT_INDEX_SCHEMA_VERSION || typeof parsed.docs !== "object" || parsed.docs === null) {
        return null;
      }
      parsed.conflicts = Array.isArray(parsed.conflicts) ? parsed.conflicts : [];
      this.doc = parsed;
      return parsed;
    } catch {
      return null;
    }
  }

  /** 索引缺失或损坏时按 frontmatter 扫描重建；已存在则原样使用（不每次重扫目录）。 */
  async ensure(roots: Array<{ folder: string; kind: DocumentKind; skipDirs?: string[] }>): Promise<DocumentIndexDoc> {
    const existing = await this.load();
    if (existing) return existing;
    return this.rebuild(roots);
  }

  /** 扫描各层目录的 frontmatter 重建索引，并记录身份冲突。 */
  async rebuild(roots: Array<{ folder: string; kind: DocumentKind; skipDirs?: string[] }>): Promise<DocumentIndexDoc> {
    const next = emptyDoc();
    const byId = new Map<string, string[]>();
    for (const root of roots) {
      for (const path of await listMarkdown(this.fs, root.folder, root.skipDirs ?? [])) {
        let text: string;
        try {
          text = await this.fs.read(path);
        } catch {
          continue;
        }
        const entry = documentEntryFromFrontmatter(text, path);
        if (!entry) continue;
        if (root.kind !== "other" && entry.kind === "other") entry.kind = root.kind;
        byId.set(entry.kb_id, [...(byId.get(entry.kb_id) ?? []), path]);
        if (!next.docs[entry.kb_id]) next.docs[entry.kb_id] = entry;
      }
    }
    for (const [kbId, paths] of byId) {
      if (paths.length > 1) {
        next.conflicts.push({ kb_id: kbId, paths, detected_at: new Date().toISOString() });
      }
    }
    next.conflicts.sort((a, b) => a.kb_id.localeCompare(b.kb_id));
    next.built_at = new Date().toISOString();
    this.doc = next;
    await this.persist(next);
    return next;
  }

  async pathOf(kbId: string): Promise<string | null> {
    return (await this.load())?.docs[kbId]?.path ?? null;
  }

  async entryOf(kbId: string): Promise<DocumentIndexEntry | null> {
    return (await this.load())?.docs[kbId] ?? null;
  }

  async entries(): Promise<DocumentIndexEntry[]> {
    const doc = await this.load();
    return doc ? Object.values(doc.docs) : [];
  }

  async conflicts(): Promise<DocumentConflict[]> {
    return (await this.load())?.conflicts ?? [];
  }

  /** 某路径是否已被**别的**文档占用（新建笔记选名时用）。 */
  async isTakenByOther(path: string, kbId: string): Promise<boolean> {
    const doc = await this.load();
    if (!doc) return false;
    for (const [id, entry] of Object.entries(doc.docs)) {
      if (id !== kbId && entry.path === path) return true;
    }
    return false;
  }

  /** 登记新建/移动后的路径；同 kb_id 指向不同文件时记冲突并保留原登记。 */
  async register(entry: DocumentIndexEntry): Promise<{ conflict: boolean }> {
    const doc = (await this.load()) ?? emptyDoc();
    const previous = doc.docs[entry.kb_id];
    if (previous && previous.path !== entry.path) {
      const paths = [...new Set([previous.path, entry.path, ...(doc.conflicts.find((c) => c.kb_id === entry.kb_id)?.paths ?? [])])].sort();
      doc.conflicts = doc.conflicts.filter((c) => c.kb_id !== entry.kb_id);
      doc.conflicts.push({ kb_id: entry.kb_id, paths, detected_at: new Date().toISOString() });
      doc.conflicts.sort((a, b) => a.kb_id.localeCompare(b.kb_id));
      doc.built_at = new Date().toISOString();
      this.doc = doc;
      await this.persist(doc);
      return { conflict: true };
    }
    doc.docs[entry.kb_id] = { ...entry, updated_at: new Date().toISOString() };
    doc.conflicts = doc.conflicts.filter((c) => c.kb_id !== entry.kb_id);
    doc.built_at = new Date().toISOString();
    this.doc = doc;
    await this.persist(doc);
    return { conflict: false };
  }

  /** 文件改名/移动后更新路径登记（身份不变，docs/23 §8.3 第 2 条）。 */
  async move(kbId: string, newPath: string): Promise<void> {
    const doc = (await this.load()) ?? emptyDoc();
    if (!doc.docs[kbId]) return;
    doc.docs[kbId] = { ...doc.docs[kbId], path: newPath, updated_at: new Date().toISOString() };
    doc.built_at = new Date().toISOString();
    this.doc = doc;
    await this.persist(doc);
  }

  async forget(kbId: string): Promise<void> {
    const doc = (await this.load()) ?? emptyDoc();
    delete doc.docs[kbId];
    doc.built_at = new Date().toISOString();
    this.doc = doc;
    await this.persist(doc);
  }

  private async persist(doc: DocumentIndexDoc): Promise<void> {
    await this.fs.write(this.file, JSON.stringify(doc, null, 2));
  }
}
