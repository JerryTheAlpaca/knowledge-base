/**
 * 同步引擎（docs/02 §13；docs/24 §5、§8；docs/08 §2、§3、§7.2、§9）：
 * 事件 → 本地待办 → 游标推进 → 下载校验 → **内容格式闸门** → 原子落盘 →
 * Source + Digest 两篇笔记 → 文档索引登记 → commit 标记 → 回执 → 清理。
 *
 * 本地状态：发现（pending）→ downloading → verified → committed → ack_pending → synced。
 *
 * 格式闸门（docs/24 §8）：清单主版本不识别、Bundle 缺 `content.json`，或内容文件的
 * `format_version` 不是 `3.0` → 该项暂停导入并提示升级；**不落盘空白笔记、不发成功回执**。
 * 这是与 manifest `VERSION_UNSUPPORTED` 分开的独立结果，两者都不静默当成成功。
 *
 * 多笔记提交（docs/08 §9）：一个 Bundle 默认产出 Source 与 Digest 两篇，
 * 每篇独立哈希与冲突检测；任一失败不标记已全部完成，下次同步续做。
 */

import {
  assertSafeRelativePath,
  digestKbId,
  digestNotePath,
  documentsIndexPath,
  joinUnder,
  noteTitleFromPath,
  resolveAvailableNotePath,
  sourceAssetsDir,
  sourceKbId,
  sourceNotePath,
} from "../vault/paths";
import {
  CLOUD_DIGEST_END,
  CLOUD_DIGEST_START,
  LOCAL_ORGANIZE_END,
  LOCAL_ORGANIZE_START,
  extractPartition,
  mergeKbFrontmatter,
  mergeManagedTags,
  managedTags,
  readFrontmatterValue,
  renderCloudPending,
  renderDigestNote,
  renderInboxIndex,
  renderSourceNote,
  replacePartition,
  sha256Hex,
  SOURCE_BODY_END,
  SOURCE_BODY_START,
  stripSegmentIds,
} from "../vault/template";
import { renderContentMarkdown } from "../vault/content";
import { DocumentIndex, type DocumentKind } from "../vault/documents";
import { CommitStore, FormatGate, Suppression } from "../vault/records";
import type { VaultFs } from "../vault/vaultfs";
import type { KbClient } from "../api";
import { ApiError } from "../api";
import type {
  CommitNoteRecord,
  CommitRecord,
  ContentDocumentV3,
  ContentRefV3,
  EngineStatus,
  KbFileEntry,
  KbManifest,
  KbSettings,
  PendingEntry,
} from "../types";
import { CONTENT_FORMAT_VERSION, LAYOUT_VERSION } from "../types";
import { parseContentDocument } from "../vault/content";

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
  /** 新 Digest 入库后自动准备整理候选（docs/08 §8.1）；默认不启用。 */
  onDigestWritten?: (itemId: string, digestPath: string) => Promise<void>;
}

const SUPPORTED_SCHEMA = "1.0";
const MAX_EVENT_PAGES = 50;
/** 格式暂停条目的重查间隔：升级插件后最迟一轮即可续做，不每轮重复下载。 */
const PAUSED_RETRY_MS = 6 * 60 * 60_000;
/** Bundle 里的机器内容文件（docs/24 §5）。 */
export const CONTENT_FILE_NAME = "content.json";

export class EpochConflictError extends Error {
  constructor() {
    super("本设备已不是主要写入设备（consumer_epoch 过期）");
    this.name = "EpochConflictError";
  }
}

/** 内容格式不受支持：暂停该项导入，等待插件升级（docs/24 §8）。 */
export class ContentFormatError extends Error {
  constructor(
    readonly code: "content_format_unsupported" | "content_file_missing" | "content_file_unparsable",
    message: string,
    readonly formatVersion: string | null,
  ) {
    super(message);
    this.name = "ContentFormatError";
  }
}

function backoffMs(attempts: number): number {
  return Math.min(30_000 * 2 ** Math.max(0, attempts - 1), 10 * 60_000);
}

async function sha256HexOfBinary(data: ArrayBuffer): Promise<string> {
  const buf = await crypto.subtle.digest("SHA-256", new Uint8Array(data));
  return Array.from(new Uint8Array(buf), (b) => b.toString(16).padStart(2, "0")).join("");
}

/** 笔记写入结果：每篇独立记录状态，失败不影响另一篇的已写入事实。 */
interface NoteWriteResult {
  role: string;
  note_path: string;
  managed_digest: string | null;
  state: string;
  conflicts: string[];
}

export class SyncEngine {
  private running = false;
  private epochConflict = false;
  private lastError: string | null = null;
  private suppressedCount = 0;
  private pausedForUpgradeCount = 0;
  /** 最近一次 loadState 的真实状态缓存：公开 status 不再返回占位值（审查 C-02）。 */
  private lastState: SyncState | null = null;

  constructor(private deps: EngineDeps) {
    this.deps.fs = deps.fs;
  }

  get status(): EngineStatus {
    const st = this.lastState;
    return {
      running: this.running,
      cursor: st?.cursor ?? 0,
      pendingCount: st ? Object.keys(st.pending).length : 0,
      lastRunAt: st?.lastRunAt ?? null,
      lastError: this.lastError,
      epochConflict: this.epochConflict,
      suppressedCount: this.suppressedCount,
      pausedForUpgrade: this.pausedForUpgradeCount,
    };
  }

  resetEpochConflict(): void {
    this.epochConflict = false;
  }

  private formatGate(): FormatGate {
    const s = this.deps.settings();
    return new FormatGate(this.deps.fs, `${s.systemFolder}/KnowledgeInbox/format_gate.json`);
  }

  private documentIndex(): DocumentIndex {
    const s = this.deps.settings();
    return new DocumentIndex(this.deps.fs, documentsIndexPath(s.systemFolder));
  }

  private async refreshSuppressedCount(): Promise<void> {
    const s = this.deps.settings();
    const suppression = new Suppression(this.deps.fs, `${s.systemFolder}/KnowledgeInbox/suppression.json`);
    this.suppressedCount = (await suppression.list()).length;
  }

  private async refreshPausedCount(): Promise<void> {
    this.pausedForUpgradeCount = (await this.formatGate().list()).length;
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
        const state = await this.deps.loadState();
        this.lastState = state;
        this.deps.onStatus({ running: false, cursor: state.cursor, pendingCount: Object.keys(state.pending).length, lastRunAt: state.lastRunAt, lastError: "尚未配对", epochConflict: false, suppressedCount: this.suppressedCount });
        return;
      }
      const state = await this.deps.loadState();
      this.lastState = state;
      const moreEvents = await this.pullEvents(client, state);
      const { pendingCount, lastError: processError } = await this.processPending(client, state);
      state.lastRunAt = Date.now();
      await this.deps.saveState(state);
      this.lastError = processError;
      await this.refreshSuppressedCount();
      await this.refreshPausedCount();
      this.deps.onStatus({
        running: false, cursor: state.cursor, pendingCount,
        lastRunAt: state.lastRunAt, lastError: processError, epochConflict: this.epochConflict,
        suppressedCount: this.suppressedCount, pausedForUpgrade: this.pausedForUpgradeCount, moreEvents,
      });
    } catch (err) {
      this.lastError = err instanceof Error ? err.message : String(err);
      this.deps.log(`同步失败：${this.lastError}`);
      const state = await this.deps.loadState();
      this.lastState = state;
      this.deps.onStatus({
        running: false, cursor: state.cursor,
        pendingCount: Object.keys(state.pending).length,
        lastRunAt: Date.now(), lastError: this.lastError, epochConflict: this.epochConflict,
        suppressedCount: this.suppressedCount,
      });
    } finally {
      this.running = false;
    }
  }

  /**
   * 事件拉取：先并入本地待办并持久化，才推进游标（docs/02 §13.1）。
   *
   * 返回是否因达到单轮页数上限而还有剩余事件（审查 C-33：不静默截断）。
   */
  private async pullEvents(client: KbClient, state: SyncState): Promise<boolean> {
    let cursor = state.cursor;
    for (let page = 0; page < MAX_EVENT_PAGES; page++) {
      let res;
      try {
        res = await client.listEvents(cursor);
      } catch (err) {
        if (err instanceof ApiError && (err.code === "CURSOR_EXPIRED" || err.status === 410)) {
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
      if (!res.has_more) return false;
    }
    this.deps.log(`事件较多，本轮达到 ${MAX_EVENT_PAGES} 页上限；下次同步继续拉取`);
    return true;
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
        if (err instanceof ApiError && err.code === "GONE") {
          // 条目已在服务器删除/过期（A21）：放弃待办并记录状态，不无限退避重试；
          // 本地已入库的内容绝不因此删除。
          const s = this.deps.settings();
          const suppression = new Suppression(this.deps.fs, `${s.systemFolder}/KnowledgeInbox/suppression.json`);
          await suppression.suppress(entry.item_id, "条目已在服务器删除或过期（GONE）");
          delete state.pending[entry.item_id];
          changed = true;
          this.deps.log(`条目 ${entry.item_id} 已在服务器删除（GONE），放弃同步并保留本地内容`);
          await this.deps.saveState(state);
          continue;
        }
        if (err instanceof ContentFormatError) {
          // 不认识的内容格式：只暂停这一项并提示升级，不发成功回执、不落空白笔记；
          // 保留待办（长间隔重查），插件升级后无需人工清理即可继续。
          await this.formatGate().hold({
            item_id: entry.item_id,
            bundle_revision: entry.revision,
            code: err.code,
            format_version: err.formatVersion,
            reason: err.message,
            detected_at: new Date().toISOString(),
          });
          entry.attempts += 1;
          entry.next_try_at = Date.now() + PAUSED_RETRY_MS;
          entry.last_error = err.message;
          lastError = `${entry.item_id}: ${err.message}`;
          changed = true;
          this.deps.log(`条目 ${entry.item_id} 已暂停导入：${err.message}`);
          await this.deps.saveState(state);
          continue;
        }
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
    const docs = this.documentIndex();
    await docs.ensure([
      { folder: s.sourcesFolder, kind: "source", skipDirs: ["_assets"] },
      { folder: s.digestsFolder, kind: "digest" },
      { folder: s.knowledgeFolder, kind: "knowledge" },
    ]);

    // 已被用户删除的条目：记录 suppression，停止复建（docs/02 §8.2）
    if (await suppression.isSuppressed(entry.item_id)) return;

    const latest = await commits.latestForItem(entry.item_id);
    if (latest && latest.bundle_revision >= entry.revision) {
      if (!latest.ack_sent) await this.ackRecord(client, commits, latest);
      return; // 已提交更新或相同版本：跳过旧快照（docs/02 §13.1）
    }
    // 只有 Source 笔记被删除才算用户主动删除；Digest 缺失可重建
    const sourceNote = latest ? CommitStore.noteState(latest, "source") : null;
    if (sourceNote && !(await fs.exists(sourceNote.note_path))) {
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
    // 内容格式闸门（先于下载与落盘）：未知版本或缺 content.json 一律暂停
    const contentEntry = assertContentFormat(manifest);
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

    // 闸门第二段：机器可读内容必须真能按 v3 解析（校验放在真实边界，不静默降级）
    let contentDoc: ContentDocumentV3 | null = null;
    if (contentEntry) {
      const stagedContent = joinUnder(stagingBase, CONTENT_FILE_NAME);
      if (!(await fs.exists(stagedContent))) {
        throw new ContentFormatError("content_file_missing",
          `Bundle 清单声明了 ${CONTENT_FILE_NAME}，但下载后未找到该文件；请升级插件或联系服务端检查发布。`,
          manifest.processing.format_version ?? null);
      }
      const parsed = parseContentDocument(JSON.parse(await fs.read(stagedContent)));
      if (!parsed.document) {
        throw new ContentFormatError("content_file_unparsable",
          `内容文件无法按 v3 读取：${parsed.errors.join("；")}`,
          manifest.processing.format_version ?? null);
      }
      contentDoc = parsed.document;
    }

    // 3. 移入最终不可变目录：01 Sources/_assets/<item_id>/source-000001/（docs/08 §2）
    const assetsBase = sourceAssetsDir(s.sourcesFolder, entry.item_id, manifest.source_revision);
    for (let i = 0; i < manifest.files.length; i++) {
      const stagePath = joinUnder(stagingBase, relPaths[i]);
      const finalPath = joinUnder(assetsBase, relPaths[i]);
      if (await fs.exists(finalPath)) {
        await fs.remove(stagePath);
        continue;
      }
      await fs.rename(stagePath, finalPath);
    }
    for (const leftover of await fs.list(stagingBase)) await fs.remove(leftover);

    // 4. 读取已下载内容：正文优先用段落版 readable.md（自然分段），
    //    没有段落版的旧来源版本回退到逐片段 normalized.md
    let normalizedText: string | null = null;
    const readablePath = joinUnder(assetsBase, "readable.md");
    const normalizedPath = joinUnder(assetsBase, "normalized.md");
    if (await fs.exists(readablePath)) normalizedText = await fs.read(readablePath);
    else if (await fs.exists(normalizedPath)) normalizedText = await fs.read(normalizedPath);

    // 云端区与 preview.md 由同一份 v3 文档渲染；本插件不再回读 preview 文本
    const snapshots = new Set<string>();
    if (contentDoc) {
      for (const ref of Object.values(contentDoc.references)) {
        const key = `${ref.item_id}@${ref.source_revision}`;
        if (snapshots.has(key)) continue;
        if (await fs.exists(`${sourceAssetsDir(s.sourcesFolder, ref.item_id, ref.source_revision)}/normalized.md`)) {
          snapshots.add(key);
        }
      }
    }
    const cloudMd = contentDoc
      ? renderContentMarkdown(contentDoc, { sourceLinkOf: (ref) => sourceAnchor(s.sourcesFolder, ref, snapshots) })
      : null;

    const status = manifest.processing.state;
    const sourcePath = await this.resolveNotePath(docs, sourceNote?.note_path ?? null,
      sourceKbId(entry.item_id), "source",
      sourceNotePath(s.sourcesFolder, manifest.source.captured_at, manifest.source.title),
      manifest.source.title, entry.item_id);
    const digestPath = await this.resolveNotePath(docs,
      latest ? CommitStore.noteState(latest, "digest")?.note_path ?? null : null,
      digestKbId(entry.item_id), "digest",
      digestNotePath(s.digestsFolder, manifest.source.captured_at, manifest.source.title),
      manifest.source.title, entry.item_id);
    const sourceLink = `[[${sourcePath}|原始资料]]`;
    const digestLink = `[[${digestPath}|查看提炼]]`;

    // 5. 写 Source 与 Digest：每篇独立冲突检测（docs/08 §3、§7.2）
    const sourceResult = await this.writeSourceNote(manifest, sourcePath, assetsBase, normalizedText, digestLink, status, latest);
    const digestResult = await this.writeDigestNote(manifest, digestPath, sourceLink, cloudMd, status, latest, contentDoc);

    // 6. 写入本地 commit 标记（可恢复完成点）：失败不标记已全部完成
    const notes: CommitNoteRecord[] = [sourceResult, digestResult];
    const allWritten = notes.every((n) => n.state === "written");
    const record: CommitRecord = {
      item_id: entry.item_id,
      bundle_revision: entry.revision,
      manifest_sha256: manifestSha,
      layout_version: LAYOUT_VERSION,
      note_path: sourceResult.note_path,
      generated_digest: sourceResult.managed_digest,
      notes,
      local_commit_id: crypto.randomUUID(),
      committed_at: new Date().toISOString(),
      ack_sent: false,
      conflicts: notes.flatMap((n) => n.conflicts),
    };
    await commits.put(record);

    // 7. 回执；成功后才算 synced。未全部写入时不发回执，下次续做。
    if (allWritten) {
      await this.ackRecord(client, commits, record);
      await this.formatGate().release(entry.item_id);
    } else {
      this.deps.log(`条目 ${entry.item_id} 部分笔记未写入，保留待办下次重试`);
    }

    // 8. 重建 00 Inbox 索引；新 Digest 入库后按开关准备整理候选
    await this.rebuildInboxIndex(s, commits);
    if (digestResult.state === "written" && this.deps.onDigestWritten) {
      await this.deps.onDigestWritten(entry.item_id, digestResult.note_path).catch((err) => {
        this.deps.log(`准备整理候选失败：${err instanceof Error ? err.message : String(err)}`);
      });
    }
  }

  /**
   * 解析一篇笔记应写入的路径：已有 commit 路径 → 文档索引 → 新的可读文件名。
   *
   * 老库沿用旧路径（既有链接仍可打开），新条目用 `YYYY-MM-DD 标题.md`；
   * 同名冲突按 `（2）` 规则让路，且绝不把同一 `kb_id` 写成第二份可写副本。
   */
  private async resolveNotePath(
    docs: DocumentIndex,
    recordedPath: string | null,
    kbId: string,
    kind: DocumentKind,
    desiredPath: string,
    title: string | null,
    itemId: string,
  ): Promise<string> {
    const fs = this.deps.fs;
    if (recordedPath && (await fs.exists(recordedPath))) {
      await docs.register({ kb_id: kbId, path: recordedPath, kind, item_id: itemId, title: title ?? "", updated_at: "" });
      return recordedPath;
    }
    const indexed = await docs.pathOf(kbId);
    if (indexed && (await fs.exists(indexed))) return indexed;
    const path = await resolveAvailableNotePath(desiredPath, async (candidate) =>
      (await fs.exists(candidate)) || (await docs.isTakenByOther(candidate, kbId)));
    await docs.register({ kb_id: kbId, path, kind, item_id: itemId, title: title ?? "", updated_at: "" });
    return path;
  }

  /** Source 笔记：正文只放原始证据；机器可写部分只有 frontmatter 与固定链接。 */
  private async writeSourceNote(
    manifest: KbManifest,
    notePath: string,
    assetsBase: string,
    normalizedText: string | null,
    digestLink: string,
    status: string,
    latest: CommitRecord | null,
  ): Promise<NoteWriteResult> {
    const fs = this.deps.fs;
    const s = this.deps.settings();
    const previous = latest ? CommitStore.noteState(latest, "source") : null;
    if (!(await fs.exists(notePath))) {
      const body = renderSourceNote(manifest, { assetsBase, digestLink, status, normalizedText });
      await fs.write(notePath, body);
      return {
        role: "source", note_path: notePath,
        managed_digest: await sha256Hex(body), state: "written", conflicts: [],
      };
    }
    // 已存在：只更新 frontmatter 的 kb_* 行与系统标签，正文（用户可能编辑过）不动
    const current = await fs.read(notePath);
    const withFm = mergeKbFrontmatter(current, [
      `kb_id: "src-${manifest.item_id}"`,
      `kb_item_id: "${manifest.item_id}"`,
      "kb_type: source",
      `kb_bundle_revision: ${manifest.bundle_revision}`,
      `kb_source_revision: ${manifest.source_revision}`,
      `kb_source_type: "${manifest.source.platform}"`,
      `kb_status: "${status}"`,
      `kb_coverage: "${manifest.source.coverage}"`,
      `kb_captured_at: ${manifest.source.captured_at ? `"${manifest.source.captured_at}"` : ""}`.trimEnd(),
      `kb_source_url: ${manifest.source.original_url ? `"${manifest.source.original_url}"` : ""}`.trimEnd(),
    ].filter((l) => !l.endsWith(":")));
    const updated0 = mergeManagedTags(withFm, managedTags("source", status));
    // 正文区随来源版本更新：用户没改过正文时才替换（首次会补上分区标记）
    const updated = await this.refreshSourceBody(updated0, assetsBase, normalizedText);
    if (updated !== current) await fs.write(notePath, updated);
    return {
      role: "source", note_path: notePath,
      managed_digest: previous?.managed_digest ?? null, state: "written", conflicts: [],
    };
  }

  /**
   * Source 正文区：有分区标记就整块替换；旧笔记没有标记时，只有正文仍等于
   * 本地旧逐片段正文（说明用户没改过）才迁移一次，用户改过的一律保留。
   */
  private async refreshSourceBody(current: string, assetsBase: string,
                                  bodyText: string | null): Promise<string> {
    if (!bodyText || !bodyText.trim()) return current;
    const inner = stripSegmentIds(bodyText).trim();
    if (extractPartition(current, SOURCE_BODY_START, SOURCE_BODY_END) !== null) {
      return replacePartition(current, SOURCE_BODY_START, SOURCE_BODY_END, inner);
    }
    const m = /## 完整文字稿[ \t]*\n+([\s\S]*)$/.exec(current);
    if (!m) return current;
    const fs = this.deps.fs;
    const legacyPath = joinUnder(assetsBase, "normalized.md");
    if (!(await fs.exists(legacyPath))) return current;
    if (m[1].trim() !== stripSegmentIds(await fs.read(legacyPath)).trim()) return current;
    return current.slice(0, m.index) +
      `## 完整文字稿\n\n${SOURCE_BODY_START}\n${inner}\n${SOURCE_BODY_END}\n`;
  }

  /** Digest 笔记：只替换 kb:cloud-digest 区，本地整理区与人工区保留（docs/08 §3.2）。 */
  private async writeDigestNote(
    manifest: KbManifest,
    notePath: string,
    sourceLink: string,
    cloudMd: string | null,
    status: string,
    latest: CommitRecord | null,
    contentDoc: ContentDocumentV3 | null,
  ): Promise<NoteWriteResult> {
    const fs = this.deps.fs;
    const s = this.deps.settings();
    const previous = latest ? CommitStore.noteState(latest, "digest") : null;
    // 内容身份写进 frontmatter：机器回读以 content.json 为准，不靠文件名或正文猜
    const contentLines = contentDoc ? [
      `kb_format_version: "${CONTENT_FORMAT_VERSION}"`,
      `kb_content_revision: ${contentDoc.revision}`,
      `kb_completeness: "${contentDoc.completeness.state}"`,
    ] : [];

    if (!(await fs.exists(notePath))) {
      const body = renderDigestNote(manifest, { sourceLink, cloudMd, status, contentLines });
      await fs.write(notePath, body);
      const cloudInner = extractPartition(body, CLOUD_DIGEST_START, CLOUD_DIGEST_END) ?? "";
      return {
        role: "digest", note_path: notePath,
        managed_digest: await sha256Hex(cloudInner), state: "written", conflicts: [],
      };
    }

    const current = await fs.read(notePath);
    const currentInner = extractPartition(current, CLOUD_DIGEST_START, CLOUD_DIGEST_END);
    if (currentInner === null) {
      // 没有分区标记：可能是旧库笔记或用户重写过结构；不猜测，交冲突文件处理
      const conflictPath = `${s.systemFolder}/KnowledgeInbox/conflicts/${manifest.item_id}--${String(manifest.bundle_revision).padStart(6, "0")}--digest.md`;
      await fs.write(conflictPath, [
        `# 待合并：${manifest.source.title ?? manifest.item_id}（Digest，bundle r${manifest.bundle_revision}）`,
        "",
        "该 Digest 笔记缺少 kb:cloud-digest 分区标记（可能被重写过结构）；请手动加入标记。",
        "",
        CLOUD_DIGEST_START,
        cloudMd ?? "",
        CLOUD_DIGEST_END,
        "",
      ].join("\n"));
      return {
        role: "digest", note_path: notePath,
        managed_digest: previous?.managed_digest ?? null, state: "merge_needed", conflicts: [conflictPath],
      };
    }

    // 用户改过云端区：不静默覆盖，新结果进冲突文件（docs/08 §3.2）
    const currentHash = await sha256Hex(currentInner);
    const userEdited = previous?.managed_digest != null && currentHash !== previous.managed_digest;
    if (userEdited) {
      const conflictPath = `${s.systemFolder}/KnowledgeInbox/conflicts/${manifest.item_id}--${String(manifest.bundle_revision).padStart(6, "0")}--digest.md`;
      await fs.write(conflictPath, [
        `# 待合并：${manifest.source.title ?? manifest.item_id}（Digest，bundle r${manifest.bundle_revision}）`,
        "",
        "检测到你在云端提炼区有编辑。新版本结果如下，请手动合并；本插件不会覆盖你的修改。",
        "",
        CLOUD_DIGEST_START,
        cloudMd ?? "",
        CLOUD_DIGEST_END,
        "",
      ].join("\n"));
      return {
        role: "digest", note_path: notePath,
        managed_digest: previous?.managed_digest ?? null,
        state: "merge_needed", conflicts: [conflictPath],
      };
    }

    // 更新 frontmatter 与云端区，保留本地整理区与人工区
    const newInner = cloudMd?.trim() || currentInner;
    // 本地整理结论是文档级字段：逐观点晋升账本已随 v3 退出（docs/23 §6.1）
    const organize = readFrontmatterValue(current, "kb_organize") ?? "not_evaluated";
    const withFm = mergeKbFrontmatter(current, [
      `kb_id: "dig-${manifest.item_id}"`,
      `kb_item_id: "${manifest.item_id}"`,
      "kb_type: digest",
      `kb_bundle_revision: ${manifest.bundle_revision}`,
      `kb_source_revision: ${manifest.source_revision}`,
      `kb_digest_revision: ${manifest.bundle_revision}`,
      `kb_status: "${status}"`,
      `kb_organize: ${organize}`,
      `kb_source_url: ${manifest.source.original_url ? `"${manifest.source.original_url}"` : ""}`.trimEnd(),
      ...contentLines,
    ].filter((l) => !l.endsWith(":")));
    const rebuilt = replacePartition(withFm, CLOUD_DIGEST_START, CLOUD_DIGEST_END, newInner);
    // `status/*` 由 `kb_organize` 派生（云端更新不改本地整理结论，docs/08 §5）
    const tagged = mergeManagedTags(rebuilt, managedTags("digest", organize));
    await fs.write(notePath, tagged);
    return {
      role: "digest", note_path: notePath,
      managed_digest: await sha256Hex(newInner), state: "written", conflicts: [],
    };
  }

  private async ackRecord(client: KbClient, commits: CommitStore, record: CommitRecord): Promise<void> {
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

  /** 重建 00 Inbox 索引（公开：恢复命令删除 commit 后调用，避免索引残留死条目）。 */
  async rebuildIndex(): Promise<void> {
    const s = this.deps.settings();
    const commits = new CommitStore(this.deps.fs, `${s.systemFolder}/KnowledgeInbox/commits`);
    await this.rebuildInboxIndex(s, commits);
  }

  private async rebuildInboxIndex(s: KbSettings, commits: CommitStore): Promise<void> {
    const records = await commits.all();
    const entries = [] as Array<{ notePath: string; title: string; status: string; capturedAt: string | null }>;
    for (const r of records) {
      for (const note of r.notes) {
        if (note.role !== "source") continue;
        // 文件名不再带 item_id；标题从可读文件名还原，日期用提交时间
        entries.push({
          notePath: note.note_path,
          title: noteTitleFromPath(note.note_path),
          status: note.state === "merge_needed" ? "merge_needed" : "synced",
          capturedAt: r.committed_at,
        });
      }
    }
    await this.deps.fs.write(`${s.inboxFolder}/待回顾.md`, renderInboxIndex(entries));
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
      // 部分笔记未写入时不补发回执：下次同步会续做（docs/08 §7.2 第 6 条）
      if (record.notes.some((x) => x.state !== "written")) continue;
      try {
        await this.ackRecord(client, commits, record);
        n += 1;
      } catch (err) {
        if (err instanceof EpochConflictError) {
          this.epochConflict = true;
          this.deps.onStatus({ running: false, cursor: 0, pendingCount: 0, lastRunAt: null, lastError: err.message, epochConflict: true, suppressedCount: this.suppressedCount });
          break;
        }
        this.deps.log(`补发回执失败（${record.item_id} r${record.bundle_revision}）：${err instanceof Error ? err.message : String(err)}`);
      }
    }
    return n;
  }
}

/**
 * 内容格式闸门·第一段：看清单声明（docs/24 §5、§8）。
 *
 * 返回需要读取的 `content.json` 条目；`null` 表示本轮 Bundle 没有生成内容
 * （原始资料照旧入库）。未知版本、缺内容文件一律抛错暂停，不当成空内容导入。
 */
export function assertContentFormat(manifest: KbManifest): KbFileEntry | null {
  const state = manifest.processing.state;
  const declared = manifest.processing.format_version ?? null;
  const contentFile = manifest.files.find((f) => f.relative_path === CONTENT_FILE_NAME) ?? null;
  const hasResult = Boolean(manifest.processing.result_file_id) || state === "ready";

  if (!hasResult && !contentFile) return null; // 尚未提炼或仅原始资料
  if (declared && declared !== CONTENT_FORMAT_VERSION) {
    throw new ContentFormatError("content_format_unsupported",
      `内容格式版本 ${declared} 不受支持，请升级插件后再同步（当前插件支持 ${CONTENT_FORMAT_VERSION}）`,
      declared);
  }
  if (!contentFile) {
    const legacy = manifest.files.some((f) => f.relative_path === "analysis.json");
    throw new ContentFormatError("content_file_missing",
      legacy
        ? `该条目仍是旧版内容协议（analysis.json，无 format_version），需要服务端完成 v3 迁移或重新整理后再同步`
        : `清单声明已生成整理结果，但缺少 ${CONTENT_FILE_NAME}；本插件不会写入空白摘要，也不会发送成功回执`,
      declared ?? (legacy ? "2.0 或更早" : null));
  }
  if (manifest.processing.content_file_id && contentFile.file_id !== manifest.processing.content_file_id) {
    throw new ContentFormatError("content_file_missing",
      `${CONTENT_FILE_NAME} 的 file_id 与清单 processing.content_file_id 不一致`, declared);
  }
  return contentFile;
}

/**
 * 一条引用 → 固定原文锚点链接（docs/24 §8）。
 *
 * 只指向该 item + source_revision 的不可变 `normalized.md`；本地没有该版快照时
 * 返回 null，由渲染器如实写明「本地原文快照缺失」，不用标题或摘要补位。
 */
export function sourceAnchor(sourcesFolder: string, ref: ContentRefV3, available: Set<string>): string | null {
  if (!ref.item_id || !ref.source_revision || !ref.segment_ids.length) return null;
  if (!available.has(`${ref.item_id}@${ref.source_revision}`)) return null;
  return `${sourceAssetsDir(sourcesFolder, ref.item_id, ref.source_revision)}/normalized#^${ref.segment_ids[0]}`;
}
