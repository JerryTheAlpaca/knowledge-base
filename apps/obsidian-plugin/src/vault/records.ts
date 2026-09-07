/**
 * 本地 commit 标记与 suppression（docs/02 §13.2、§8.2）。
 * 依赖注入极小的文件系统接口，纯逻辑可在 Node 下测试。
 */

import { commitMarkerName } from "./paths";
import type { CommitRecord } from "../types";

export interface FsLike {
  exists(path: string): Promise<boolean>;
  read(path: string): Promise<string>;
  write(path: string, data: string): Promise<void>;
  remove(path: string): Promise<void>;
  list(path: string): Promise<string[]>;
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

  async get(itemId: string, revision: number): Promise<CommitRecord | null> {
    const p = this.path(itemId, revision);
    if (!(await this.fs.exists(p))) return null;
    try {
      return JSON.parse(await this.fs.read(p)) as CommitRecord;
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
    await this.fs.write(
      this.path(record.item_id, record.bundle_revision),
      JSON.stringify(record, null, 2),
    );
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

  async suppress(itemId: string, reason: string): Promise<void> {
    const doc = await this.load();
    doc.items[itemId] = { suppressed_at: new Date().toISOString(), reason };
    await this.save(doc);
  }

  async unsuppressAll(): Promise<number> {
    const doc = await this.load();
    const n = Object.keys(doc.items).length;
    doc.items = {};
    await this.save(doc);
    return n;
  }
}
