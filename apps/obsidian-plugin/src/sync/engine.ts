/**
 * 同步引擎（docs/02 §13）：事件 → 本地待办 → 游标推进 → 下载校验 → 原子落盘 →
 * Source 笔记/冲突 → commit 标记 → 回执 → 清理。
 * 本地状态：发现（pending）→ downloading → verified → committed → ack_pending → synced。
 */

import { assertSafeRelativePath, bundleDirName, joinUnder, sourceNotePath } from "../vault/paths";
import {
  GEN_END,
  GEN_START,
  extractGenerated,
  mergeKbFrontmatter,
  renderGenerated,
  renderInboxIndex,
  renderSourceNote,
  sha256Hex,
} from "../vault/template";
import { CommitStore, Suppression } from "../vault/records";
import type { VaultFs } from "../vault/vaultfs";
import type { KbClient } from "../api";
import { ApiError } from "../api";
import type { EngineStatus, KbSettings, PendingEntry } from "../types";

export interface SyncState {
  cursor: number;
  pending: Record<string, PendingEntry>;
  lastRunAt: number | null;
}

export const EMPTY_STATE: SyncState = { cursor: 0, pending: {}, lastRunAt: null };

export interface EngineDeps {
  fs: VaultFs;
  getClient: () => KbClient | null;
  settings: () => KbSettings;
  loadState: () => Promise<SyncState>;
  saveState: (s: SyncState) => Promise<void>;
  onStatus: (s: EngineStatus) => void;
  log: (msg: string) => void;
}

const SUPPORTED_SCHEMA = "1.0";
const MAX_EVENT_PAGES = 50;

export class EpochConflictError extends Error {
  constructor() {
    super("本设备已不是主要写入设备（consumer_epoch 过期）");
    this.name = "EpochConflictError";
  }
}

function backoffMs(attempts: number): number {
  return Math.min(30_000 * 2 ** Math.max(0, attempts - 1), 10 * 60_000);
}

async function sha256HexOfBinary(data: ArrayBuffer): Promise<string> {
  const buf = await crypto.subtle.digest("SHA-256", new Uint8Array(data));
  return Array.from(new Uint8Array(buf), (b) => b.toString(16).padStart(2, "0")).join("");
}

export class SyncEngine {
  private running = false;
  private epochConflict = false;
  private lastError: string | null = null;

  constructor(private deps: EngineDeps) {
    this.deps.fs = deps.fs;
  }

  get status(): EngineStatus {
    return {
      running: this.running,
      cursor: 0,
      pendingCount: 0,
      lastRunAt: null,
      lastError: this.lastError,
      epochConflict: this.epochConflict,
    };
  }

  resetEpochConflict(): void {
    this.epochConflict = false;
  }

  /** 单实例任务锁；重复触发直接跳过（docs/02 §13.3）。 */
  async runOnce(_reason: string): Promise<void> {
    if (this.running) {
      this.deps.log("同步已在进行，跳过本次触发");
      return;
    }
    this.running = true;
    try {
      const client = this.deps.getClient();
      if (!client) {
        this.deps.onStatus({ running: false, cursor: 0, pendingCount: 0, lastRunAt: Date.now(), lastError: "尚未配对", epochConflict: false });
        return;
      }
      const state = await this.deps.loadState();
      await this.pullEvents(client, state);
      const { pendingCount, lastError: processError } = await this.processPending(client, state);
      state.lastRunAt = Date.now();
      await this.deps.saveState(state);
      this.lastError = processError;
      this.deps.onStatus({
        running: false, cursor: state.cursor, pendingCount,
        lastRunAt: state.lastRunAt, lastError: processError, epochConflict: this.epochConflict,
      });
    } catch (err) {
      this.lastError = err instanceof Error ? err.message : String(err);
      this.deps.log(`同步失败：${this.lastError}`);
      const state = await this.deps.loadState();
      this.deps.onStatus({
        running: false, cursor: state.cursor,
        pendingCount: Object.keys(state.pending).length,
        lastRunAt: Date.now(), lastError: this.lastError, epochConflict: this.epochConflict,
      });
    } finally {
      this.running = false;
    }
  }

  /** 事件拉取：先并入本地待办并持久化，才推进游标（docs/02 §13.1）。 */
  private async pullEvents(client: KbClient, state: SyncState): Promise<void> {
    let cursor = state.cursor;
    for (let page = 0; page < MAX_EVENT_PAGES; page++) {
      let res;
      try {
        res = await client.listEvents(cursor);
      } catch (err) {
        if (err instanceof ApiError && (err.code === "CURSOR_EXPIRED" || err.status === 410)) {
          // 游标过期：对账本地 commit 后重置游标重新拉取
          this.deps.log("游标过期，重置为 0 重新对账");
          cursor = 0;
          state.cursor = 0;
          await this.deps.saveState(state);
          continue;
        }
        throw err;
      }
      for (const ev of res.events) {
        if (ev.event_type !== "bundle_published" || !ev.item_id || !ev.bundle_revision) continue;
        const existing = state.pending[ev.item_id];
        state.pending[ev.item_id] = {
          item_id: ev.item_id,
          revision: Math.max(existing?.revision ?? 0, ev.bundle_revision),
          seq: Math.min(existing?.seq ?? Number.MAX_SAFE_INTEGER, ev.seq),
          enqueued_at: existing?.enqueued_at ?? ev.created_at,
          attempts: existing?.attempts ?? 0,
          next_try_at: existing?.next_try_at ?? 0,
          last_error: existing?.last_error,
        };
      }
      cursor = res.next_cursor;
      state.cursor = cursor;
      // 待办与游标同一次持久化：已读取 ≠ 已落盘，游标只在待办写入后推进
      await this.deps.saveState(state);
      if (!res.has_more) break;
    }
  }

  private async processPending(client: KbClient, state: SyncState): Promise<{ pendingCount: number; lastError: string | null }> {
    const entries = Object.values(state.pending).sort((a, b) => a.seq - b.seq);
    let lastError: string | null = null;
    let changed = false;

    for (const entry of entries) {
      if (Date.now() < entry.next_try_at) continue;
      if (this.epochConflict) break;
      try {
        await this.commitItem(client, entry);
        delete state.pending[entry.item_id];
        changed = true;
      } catch (err) {
        entry.attempts += 1;
        entry.next_try_at = Date.now() + backoffMs(entry.attempts);
        entry.last_error = err instanceof Error ? err.message : String(err);
        lastError = `${entry.item_id}: ${entry.last_error}`;
        changed = true;
        this.deps.log(`条目 ${entry.item_id} 第 ${entry.attempts} 次失败：${entry.last_error}`);
        if (err instanceof EpochConflictError) {
          this.epochConflict = true;
          break;
        }
        if (err instanceof ApiError && (err.code === "AUTH_EXPIRED" || err.code === "FORBIDDEN")) {
          break; // Token 失效：停止本轮，等用户重新配对
        }
        if (err instanceof ApiError && err.code === "VERSION_UNSUPPORTED") {
          break; // 契约主版本不识别：停止自动写入并提示升级（docs/02 §17）
        }
      }
      if (changed) await this.deps.saveState(state);
    }
    return { pendingCount: Object.keys(state.pending).length, lastError };
  }

  /** 单个 Bundle 的完整提交；任何失败抛出，由 processPending 记账退避。 */
  private async commitItem(client: KbClient, entry: PendingEntry): Promise<void> {
    const s = this.deps.settings();
    const fs = this.deps.fs;
    const commits = new CommitStore(fs, `${s.systemFolder}/KnowledgeInbox/commits`);
    const suppression = new Suppression(fs, `${s.systemFolder}/KnowledgeInbox/suppression.json`);

    // 已被用户删除的条目：记录 suppression，停止复建（docs/02 §8.2）
    if (await suppression.isSuppressed(entry.item_id)) return;

    const latest = await commits.latestForItem(entry.item_id);
    if (latest && latest.bundle_revision >= entry.revision) {
      if (!latest.ack_sent) await this.ackRecord(client, commits, latest);
      return; // 已提交更新或相同版本：跳过旧快照（docs/02 §13.1）
    }
    if (latest && !(await fs.exists(latest.note_path))) {
      await suppression.suppress(entry.item_id, "用户删除了 Source 笔记");
      return;
    }

    const { manifest, sha256: manifestSha } = await client.getManifest(entry.item_id, entry.revision);
    if (manifest.item_id !== entry.item_id || manifest.bundle_revision !== entry.revision) {
      throw new ApiError("REVISION_CONFLICT", "清单与请求版本不一致", 409);
    }
    if (manifest.schema_version !== SUPPORTED_SCHEMA) {
      throw new ApiError("VERSION_UNSUPPORTED", `清单 schema_version ${manifest.schema_version} 不受支持，请升级插件`, 400);
    }
    const relPaths = manifest.files.map((f) => assertSafeRelativePath(f.relative_path));

    // 1-2. 下载全部文件到同卷暂存目录并逐个校验大小与 SHA-256
    const stagingBase = `${s.assetsFolder}/KnowledgeInbox/${entry.item_id}/.staging-${entry.revision}`;
    for (let i = 0; i < manifest.files.length; i++) {
      const f = manifest.files[i];
      const stagePath = joinUnder(stagingBase, relPaths[i]);
      let ok = false;
      if (await fs.exists(stagePath)) {
        try {
          const data = await fs.readBinary(stagePath);
          ok = data.byteLength === f.bytes && (await sha256HexOfBinary(data)) === f.sha256;
        } catch { ok = false; }
      }
      if (!ok) {
        const blob = await client.getFile(entry.item_id, entry.revision, f.file_id);
        const checked = await sha256HexOfBinary(blob);
        if (checked !== f.sha256 || blob.byteLength !== f.bytes) {
          throw new Error(`文件校验失败：${f.relative_path}`);
        }
        await fs.writeBinary(stagePath, blob);
      }
    }

    // 3. 移入最终不可变目录
    const finalBase = `${s.assetsFolder}/KnowledgeInbox/${entry.item_id}/${bundleDirName(entry.revision)}`;
    for (let i = 0; i < manifest.files.length; i++) {
      const stagePath = joinUnder(stagingBase, relPaths[i]);
      const finalPath = joinUnder(finalBase, relPaths[i]);
      if (await fs.exists(finalPath)) {
        await fs.remove(stagePath);
        continue;
      }
      await fs.rename(stagePath, finalPath);
    }
    for (const leftover of await fs.list(stagingBase)) await fs.remove(leftover);

    // 4. 渲染生成区
    const assetsBase = finalBase;
    let previewText: string | null = null;
    if (manifest.processing.result_file_id) {
      const previewPath = joinUnder(finalBase, "preview.md");
      if (await fs.exists(previewPath)) previewText = await fs.read(previewPath);
    }
    const generatedMd = renderGenerated(manifest, previewText, assetsBase);
    const generatedDigest = await sha256Hex(generatedMd);

    // 5. 创建/安全更新 Source 笔记
    let userNote: string | null = null;
    const capturePath = joinUnder(finalBase, "capture.json");
    if (await fs.exists(capturePath)) {
      try {
        // 服务端 capture.json 结构为 {"capture": <原始 payload>, "received_at": ...}（pipeline.py §接收）
        const cap = JSON.parse(await fs.read(capturePath)) as {
          user_note?: string | null;
          capture?: { user_note?: string | null } | null;
        };
        userNote = cap.capture?.user_note ?? cap.user_note ?? null;
      } catch { userNote = null; }
    }

    const notePath = latest?.note_path
      ?? sourceNotePath(s.sourcesFolder, manifest.source.captured_at, manifest.source.title, entry.item_id);
    const status = manifest.processing.state;
    let conflicts = latest?.conflicts ?? [];
    let digestForRecord: string | null = generatedDigest;

    if (await fs.exists(notePath)) {
      let conflicted = false;
      // processNote 回调是同步的，无法在回调内做异步哈希：先读一遍算生成区摘要，
      // 回调内用原文比对确认期间无外部修改（真机验收 A11 发现：原文与哈希直接比较恒不相等，导致每次更新都误判冲突）。
      const preText = await fs.read(notePath);
      const preInner = extractGenerated(preText);
      const preDigest = preInner === null ? null : await sha256Hex(preInner);
      await fs.processNote(notePath, (current) => {
        const innerNow = extractGenerated(current);
        const userEdited = innerNow === null
          || (latest?.generated_digest != null && preDigest !== latest.generated_digest)
          || (preInner !== null && innerNow !== preInner);
        if (userEdited) {
          // 用户改过生成区：新结果进冲突文件，笔记不动生成区（docs/02 §8.2 / A12）
          conflicted = true;
          return mergeFrontmatterOnly(current, manifest, "merge_needed");
        }
        return mergeNoteContent(current, manifest, generatedMd, status);
      });
      if (conflicted) {
        const conflictPath = `${s.systemFolder}/KnowledgeInbox/conflicts/${entry.item_id}--${String(entry.revision).padStart(6, "0")}.md`;
        await fs.write(conflictPath, [
          `# 待合并：${manifest.source.title ?? entry.item_id}（bundle r${entry.revision}）`,
          "",
          "新版本 AI 结果如下；请手动合并到 Source 笔记的生成区，然后可删除本文件。",
          "",
          GEN_START,
          generatedMd,
          GEN_END,
          "",
        ].join("\n"));
        conflicts = [...conflicts, conflictPath];
        digestForRecord = latest?.generated_digest ?? null;
      }
    } else {
      await fs.write(notePath, renderSourceNote(manifest, generatedMd, userNote, assetsBase, status));
    }

    // 6. 写入本地 commit 标记（可恢复完成点）
    const record = {
      item_id: entry.item_id,
      bundle_revision: entry.revision,
      manifest_sha256: manifestSha,
      note_path: notePath,
      generated_digest: digestForRecord,
      local_commit_id: crypto.randomUUID(),
      committed_at: new Date().toISOString(),
      ack_sent: false,
      conflicts,
    };
    await commits.put(record);

    // 7. 回执；成功后才算 synced
    await this.ackRecord(client, commits, record);

    // 8. 重建 00 Inbox 索引
    await this.rebuildInboxIndex(s, commits);
  }

  private async ackRecord(client: KbClient, commits: CommitStore, record: Awaited<ReturnType<CommitStore["get"]>> & object): Promise<void> {
    if (!record || record.ack_sent) return;
    try {
      await client.sendReceipt(record.item_id, record.bundle_revision, record.manifest_sha256, record.local_commit_id);
    } catch (err) {
      if (err instanceof ApiError && err.code === "REVISION_CONFLICT") {
        throw new EpochConflictError();
      }
      throw err;
    }
    record.ack_sent = true;
    await commits.put(record);
  }

  private async rebuildInboxIndex(s: KbSettings, commits: CommitStore): Promise<void> {
    const records = await commits.all();
    const entries = [] as Array<{ notePath: string; title: string; status: string; capturedAt: string | null }>;
    for (const r of records) {
      const base = r.note_path.split("/").pop() ?? r.note_path;
      const title = base.replace(/\.md$/, "").split("--")[0];
      entries.push({ notePath: r.note_path, title, status: r.conflicts.length > 0 ? "merge_needed" : "synced", capturedAt: null });
    }
    await this.deps.fs.write(`${s.inboxFolder}/Knowledge Inbox.md`, renderInboxIndex(entries));
  }

  /** 启动恢复：补发尚未确认的回执（docs/02 §13.2 重启扫描）。 */
  async recoverReceipts(): Promise<number> {
    const client = this.deps.getClient();
    if (!client) return 0;
    const s = this.deps.settings();
    const commits = new CommitStore(this.deps.fs, `${s.systemFolder}/KnowledgeInbox/commits`);
    let n = 0;
    for (const record of await commits.all()) {
      if (record.ack_sent) continue;
      try {
        await this.ackRecord(client, commits, record);
        n += 1;
      } catch (err) {
        if (err instanceof EpochConflictError) {
          this.epochConflict = true;
          this.deps.onStatus({ running: false, cursor: 0, pendingCount: 0, lastRunAt: null, lastError: err.message, epochConflict: true });
          break;
        }
        this.deps.log(`补发回执失败（${record.item_id} r${record.bundle_revision}）：${err instanceof Error ? err.message : String(err)}`);
      }
    }
    return n;
  }
}

function mergeFrontmatterOnly(current: string, manifest: import("../types").KbManifest, status: string): string {
  return mergeKbFrontmatter(current, manifest, status);
}

function mergeNoteContent(current: string, manifest: import("../types").KbManifest, generatedMd: string, status: string): string {
  const withFm = mergeKbFrontmatter(current, manifest, status);
  const start = withFm.indexOf(GEN_START);
  const end = withFm.indexOf(GEN_END);
  if (start === -1 || end === -1) return withFm;
  return `${withFm.slice(0, start + GEN_START.length)}\n${generatedMd}\n${withFm.slice(end)}`;
}
