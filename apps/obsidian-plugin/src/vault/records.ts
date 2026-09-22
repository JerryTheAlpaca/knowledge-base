/**
 * 本地 commit 标记、suppression 与历史快照
 * （docs/02 §13.2、§8.2；docs/08 §2、§7.2、§9）。
 * 依赖注入极小的文件系统接口，纯逻辑可在 Node 下测试。
 *
 * 一个 Bundle 可能产出 Source + Digest 两篇笔记：commit 记录升级为多笔记布局，
 * 每篇独立路径与哈希；失败不标记已全部完成（docs/08 §7.2、§9）。
 */

import { commitMarkerName, revisionDir, revisionFileName } from "./paths";
import type { CommitNoteRecord, CommitRecord } from "../types";
import { LAYOUT_VERSION } from "../types";

export interface FsLike {
  exists(path: string): Promise<boolean>;
  read(path: string): Promise<string>;
  write(path: string, data: string): Promise<void>;
  remove(path: string): Promise<void>;
  list(path: string): Promise<string[]>;
  /** 子目录列表（本地索引扫描 03 Knowledge 用，docs/08 §5）。 */
  listDirs(path: string): Promise<string[]>;
}

export class CommitStore {
  constructor(private fs: FsLike, private commitsDir: string) {}

  private path(itemId: string, revision: number): string {
    return `${this.commitsDir}/${commitMarkerName(itemId, revision)}`;
  }

  /** fs.list 可能返回完整路径；统一取 basename。 */
  private baseName(entry: string): string {
    return entry.split("/").pop() ?? entry;
  }

  private parseName(name: string): { itemId: string; revision: number } | null {
    const m = /^(.+)--(\d{6})\.json$/.exec(this.baseName(name));
    if (!m) return null;
    return { itemId: m[1], revision: Number(m[2]) };
  }

  /** 读取并升级旧格式记录：layout_version=1 只有 note_path。 */
  private normalize(raw: unknown): CommitRecord | null {
    if (!raw || typeof raw !== "object") return null;
    const rec = raw as Partial<CommitRecord> & { note_path?: string };
    if (!rec.item_id || rec.bundle_revision === undefined || !rec.manifest_sha256) return null;
    const layout = rec.layout_version ?? 1;
    let notes: CommitNoteRecord[] = Array.isArray(rec.notes) ? rec.notes : [];
    if (!notes.length && rec.note_path) {
      // 旧记录：单篇 Source 笔记，状态由 conflicts 推断
      notes = [{
        role: "source",
        note_path: rec.note_path,
        managed_digest: rec.generated_digest ?? null,
        state: (rec.conflicts ?? []).length > 0 ? "merge_needed" : "written",
        conflicts: rec.conflicts ?? [],
      }];
    }
    return {
      item_id: rec.item_id,
      bundle_revision: rec.bundle_revision,
      manifest_sha256: rec.manifest_sha256,
      layout_version: layout,
      note_path: rec.note_path ?? notes[0]?.note_path ?? "",
      generated_digest: rec.generated_digest ?? notes[0]?.managed_digest ?? null,
      notes,
      local_commit_id: rec.local_commit_id ?? "",
      committed_at: rec.committed_at ?? new Date().toISOString(),
      ack_sent: Boolean(rec.ack_sent),
      conflicts: rec.conflicts ?? [],
    };
  }

  async get(itemId: string, revision: number): Promise<CommitRecord | null> {
    const p = this.path(itemId, revision);
    if (!(await this.fs.exists(p))) return null;
    try {
      return this.normalize(JSON.parse(await this.fs.read(p)));
    } catch {
      return null;
    }
  }

  /** 该条目最新（revision 最大）的 commit 记录；没有则 null。 */
  async latestForItem(itemId: string): Promise<CommitRecord | null> {
    let best: CommitRecord | null = null;
    for (const entry of await this.fs.list(this.commitsDir)) {
      const parsed = this.parseName(entry);
      if (!parsed || parsed.itemId !== itemId) continue;
      const rec = await this.get(itemId, parsed.revision);
      if (rec && (best === null || rec.bundle_revision > best.bundle_revision)) best = rec;
    }
    return best;
  }

  async put(record: CommitRecord): Promise<void> {
    const normalized: CommitRecord = { ...record, layout_version: LAYOUT_VERSION };
    await this.fs.write(
      this.path(record.item_id, record.bundle_revision),
      JSON.stringify(normalized, null, 2),
    );
  }

  /** 删除该条目的全部 commit 标记；返回删除数量。恢复命令用它让下次事件走全新建路径。 */
  async removeForItem(itemId: string): Promise<number> {
    let n = 0;
    for (const entry of await this.fs.list(this.commitsDir)) {
      const parsed = this.parseName(entry);
      if (!parsed || parsed.itemId !== itemId) continue;
      await this.fs.remove(this.path(parsed.itemId, parsed.revision));
      n += 1;
    }
    return n;
  }

  async all(): Promise<CommitRecord[]> {
    const out: CommitRecord[] = [];
    for (const entry of await this.fs.list(this.commitsDir)) {
      const parsed = this.parseName(entry);
      if (!parsed) continue;
      const rec = await this.get(parsed.itemId, parsed.revision);
      if (rec) out.push(rec);
    }
    return out;
  }

  /** 某篇笔记当前是否已写入（用于「失败不标记已全部完成」）。 */
  static noteState(record: CommitRecord, role: string): CommitNoteRecord | null {
    return record.notes.find((n) => n.role === role) ?? null;
  }
}

// ---- 历史快照（docs/08 §2、§6.1） ----

/** 被引用过的 Digest／Knowledge 历史快照；不按普通缓存清理。 */
export class RevisionStore {
  constructor(private fs: FsLike, private systemFolder: string) {}

  dir(kind: "digests" | "knowledge", id: string): string {
    return revisionDir(this.systemFolder, kind, id);
  }

  path(kind: "digests" | "knowledge", id: string, revision: number): string {
    return `${this.dir(kind, id)}/${revisionFileName(revision)}`;
  }

  /** 写入快照（幂等：同版本同内容不重写）。 */
  async save(kind: "digests" | "knowledge", id: string, revision: number, content: string): Promise<void> {
    const p = this.path(kind, id, revision);
    if (await this.fs.exists(p)) {
      try {
        if ((await this.fs.read(p)) === content) return;
      } catch {
        // 读取失败按需要重写处理
      }
    }
    await this.fs.write(p, content);
  }

  async read(kind: "digests" | "knowledge", id: string, revision: number): Promise<string | null> {
    const p = this.path(kind, id, revision);
    if (!(await this.fs.exists(p))) return null;
    try {
      return await this.fs.read(p);
    } catch {
      return null;
    }
  }

  /** 已有快照版本号（升序）。 */
  async revisions(kind: "digests" | "knowledge", id: string): Promise<number[]> {
    const out: number[] = [];
    for (const entry of await this.fs.list(this.dir(kind, id))) {
      const m = /r(\d{6})\.md$/.exec(entry.split("/").pop() ?? entry);
      if (m) out.push(Number(m[1]));
    }
    return out.sort((a, b) => a - b);
  }
}

export interface SuppressionDoc {
  items: Record<string, { suppressed_at: string; reason: string }>;
}

export class Suppression {
  private doc: SuppressionDoc | null = null;

  constructor(private fs: FsLike, private file: string) {}

  private async load(): Promise<SuppressionDoc> {
    if (this.doc) return this.doc;
    if (await this.fs.exists(this.file)) {
      try {
        this.doc = JSON.parse(await this.fs.read(this.file)) as SuppressionDoc;
      } catch {
        this.doc = { items: {} };
      }
    } else {
      this.doc = { items: {} };
    }
    this.doc.items ??= {};
    return this.doc;
  }

  private async save(doc: SuppressionDoc): Promise<void> {
    this.doc = doc;
    await this.fs.write(this.file, JSON.stringify(doc, null, 2));
  }

  async isSuppressed(itemId: string): Promise<boolean> {
    return itemId in (await this.load()).items;
  }

  /** 当前被抑制的条目 ID（供状态显示与恢复命令编排）。 */
  async list(): Promise<string[]> {
    return Object.keys((await this.load()).items);
  }

  async suppress(itemId: string, reason: string): Promise<void> {
    const doc = await this.load();
    doc.items[itemId] = { suppressed_at: new Date().toISOString(), reason };
    await this.save(doc);
  }

  /** 清空抑制并返回被清除的条目 ID；调用方应同步删除这些条目的本地 commit，
   * 否则下次事件会因“commit 存在但笔记不在”立即再次抑制（真机验收遗留 #7）。 */
  async unsuppressAll(): Promise<string[]> {
    const doc = await this.load();
    const removed = Object.keys(doc.items);
    doc.items = {};
    await this.save(doc);
    return removed;
  }
}

// ---- 内容格式暂停记录（docs/24 §8） ----

export interface FormatGateEntry {
  item_id: string;
  bundle_revision: number;
  /** content_format_unsupported | content_file_missing | content_file_unparsable */
  code: string;
  format_version: string | null;
  reason: string;
  detected_at: string;
}

export interface FormatGateDoc {
  items: Record<string, FormatGateEntry>;
}

/** 不支持的内容格式：该项暂停导入、不发成功回执，升级插件后下次同步自动续做。 */
export class FormatGate {
  private doc: FormatGateDoc | null = null;

  constructor(private fs: FsLike, private file: string) {}

  private async load(): Promise<FormatGateDoc> {
    if (this.doc) return this.doc;
    if (await this.fs.exists(this.file)) {
      try {
        this.doc = JSON.parse(await this.fs.read(this.file)) as FormatGateDoc;
      } catch {
        this.doc = { items: {} };
      }
    } else {
      this.doc = { items: {} };
    }
    this.doc!.items ??= {};
    return this.doc!;
  }

  async hold(entry: FormatGateEntry): Promise<void> {
    const doc = await this.load();
    doc.items[entry.item_id] = entry;
    this.doc = doc;
    await this.fs.write(this.file, JSON.stringify(doc, null, 2));
  }

  async release(itemId: string): Promise<void> {
    const doc = await this.load();
    if (!(itemId in doc.items)) return;
    delete doc.items[itemId];
    this.doc = doc;
    await this.fs.write(this.file, JSON.stringify(doc, null, 2));
  }

  async list(): Promise<FormatGateEntry[]> {
    return Object.values((await this.load()).items);
  }
}

// ---- 通用 JSON 存储（任务、候选、索引） ----

export class JsonStore<T> {
  constructor(
    private fs: FsLike,
    private path: string,
    private fallback: () => T,
  ) {}

  async read(): Promise<T> {
    if (!(await this.fs.exists(this.path))) return this.fallback();
    try {
      return JSON.parse(await this.fs.read(this.path)) as T;
    } catch {
      return this.fallback();
    }
  }

  async write(value: T): Promise<void> {
    await this.fs.write(this.path, JSON.stringify(value, null, 2));
  }
}
