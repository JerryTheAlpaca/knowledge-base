/**
 * 本地主题索引与匹配（docs/08 §5）。
 *
 * - 只扫描 `03 Knowledge`，提取标题、`kb_id`、aliases、范围说明和检索关键词，
 *   建立可重建的本地索引（`99 System/KnowledgeInbox/knowledge-index.json`）。
 * - 用户改名／移动笔记时按 `kb_id` 更新映射，索引丢失可重新扫描。
 * - 匹配顺序：精确标题和别名 → 关键词及范围检索 → 取最多约 5 个候选在本地判断相关性。
 *   数量是「可调整的实现参数」，不是质量保证；匹配模糊时列候选，不强行归类。
 * - 检索不需要把所有主题发给模型。
 *
 * 纯逻辑模块：文件访问通过 `FsLike` 注入，可独立测试。
 */

import type { FsLike } from "../vault/records";
import { JsonStore } from "../vault/records";
import {
  KNOWLEDGE_END,
  KNOWLEDGE_START,
  extractKnowledgeScope,
  extractPartition,
  indexEntrySearchText,
  readFrontmatterList,
  readFrontmatterValue,
  sha256Hex,
} from "../vault/template";
import { knowledgeIndexPath } from "../vault/paths";
import { listMarkdown } from "../vault/documents";
import type { KnowledgeIndex, KnowledgeIndexEntry } from "../types";

export const INDEX_SCHEMA_VERSION = "1.0";

/** 匹配候选数量上限（docs/08 §5「最多约 5 个」，实现参数可调）。 */
export const MAX_MATCH_CANDIDATES = 5;

/** 正文中的检索关键词：范围说明里 `关键词：` 之后的分隔列表。 */
export function extractKeywords(scope: string): string[] {
  const m = /关键词[：:]\s*([^\n]+)/.exec(scope);
  if (!m) return [];
  return m[1].split(/[，,、;；|]/).map((s) => s.trim()).filter(Boolean);
}

/** 从一篇 Knowledge 笔记解析索引条目；没有 kb_id 的普通笔记不进入索引。 */
export function parseKnowledgeEntry(text: string, path: string): KnowledgeIndexEntry | null {
  const kbId = readFrontmatterValue(text, "kb_id");
  if (!kbId || readFrontmatterValue(text, "kb_type") !== "knowledge") return null;
  const title = /^#\s+(.+)$/m.exec(text)?.[1]?.trim() ?? path.split("/").pop()?.replace(/\.md$/, "") ?? kbId;
  const scope = extractKnowledgeScope(text);
  const managed = extractPartition(text, KNOWLEDGE_START, KNOWLEDGE_END) ?? "";
  return {
    kb_id: kbId,
    title,
    path,
    aliases: readFrontmatterList(text, "aliases"),
    scope,
    keywords: extractKeywords(scope),
    revision: Number(readFrontmatterValue(text, "kb_revision") ?? "0") || 0,
    reviewed_at: readFrontmatterValue(text, "kb_reviewed_at"),
    body_hash: "", // 由调用方异步补齐
  };
}

export interface KnowledgeMatch {
  entry: KnowledgeIndexEntry;
  /** exact（标题/别名精确命中）| keyword（关键词或范围检索命中） */
  via: "exact" | "keyword";
  score: number;
}

/** 分词：中文按 2 字滑窗，拉丁与数字按词；用于本地关键词检索。 */
export function tokenize(text: string): string[] {
  const lower = text.toLowerCase();
  const out: string[] = [];
  for (const word of lower.match(/[a-z0-9][a-z0-9_+-]*/g) ?? []) out.push(word);
  for (const run of lower.match(/[\u4e00-\u9fff]+/g) ?? []) {
    if (run.length <= 2) { out.push(run); continue; }
    for (let i = 0; i < run.length - 1; i++) out.push(run.slice(i, i + 2));
  }
  return out;
}

/**
 * 匹配顺序（docs/08 §5）：精确标题和别名 → 关键词及范围检索。
 *
 * 返回最多 `MAX_MATCH_CANDIDATES` 个候选；空数组表示没有匹配，需要新建主题建议。
 */
export function matchKnowledge(
  entries: KnowledgeIndexEntry[],
  query: { title: string; text: string },
  limit = MAX_MATCH_CANDIDATES,
): KnowledgeMatch[] {
  const title = query.title.trim().toLowerCase();
  const exact: KnowledgeMatch[] = [];
  for (const e of entries) {
    const names = [e.title, ...e.aliases].map((n) => n.trim().toLowerCase()).filter(Boolean);
    if (names.some((n) => n === title)) exact.push({ entry: e, via: "exact", score: 1000 });
  }
  if (exact.length) return exact.slice(0, limit);

  const queryTokens = new Set(tokenize(`${query.title}\n${query.text}`));
  const scored: KnowledgeMatch[] = [];
  for (const e of entries) {
    const haystack = tokenize(indexEntrySearchText(e));
    let score = 0;
    for (const t of haystack) if (queryTokens.has(t)) score += 1;
    if (score > 0) scored.push({ entry: e, via: "keyword", score });
  }
  return scored.sort((a, b) => b.score - a.score).slice(0, limit);
}

/** 索引存储：可重建，不随 Vault 同步；损坏或缺失时重新扫描。 */
export class KnowledgeIndexStore {
  private store: JsonStore<KnowledgeIndex>;
  constructor(private fs: FsLike, private systemFolder: string) {
    this.store = new JsonStore<KnowledgeIndex>(fs, knowledgeIndexPath(systemFolder), () => ({
      schema_version: INDEX_SCHEMA_VERSION,
      built_at: new Date().toISOString(),
      entries: [],
    }));
  }

  async read(): Promise<KnowledgeIndex> {
    const doc = await this.store.read();
    if (doc.schema_version !== INDEX_SCHEMA_VERSION || !Array.isArray(doc.entries)) {
      return { schema_version: INDEX_SCHEMA_VERSION, built_at: new Date().toISOString(), entries: [] };
    }
    return doc;
  }

  /** 重新扫描 `03 Knowledge`，整表重建（用户改名／移动后按 kb_id 收敛）。
   *
   * 同一 `kb_id` 出现在多个文件时两份都保留在磁盘上，第一个进入检索索引；
   * 身份冲突由文档索引 `index/documents.json` 的 conflicts 记录并对外报告。
   */
  async rebuild(knowledgeFolder: string): Promise<KnowledgeIndex> {
    const entries: KnowledgeIndexEntry[] = [];
    const seen = new Set<string>();
    for (const path of await listMarkdown(this.fs, knowledgeFolder)) {
      let text: string;
      try {
        text = await this.fs.read(path);
      } catch {
        continue;
      }
      const entry = parseKnowledgeEntry(text, path);
      if (!entry || seen.has(entry.kb_id)) continue;
      seen.add(entry.kb_id);
      entry.body_hash = await this.bodyHash(text);
      entries.push(entry);
    }
    entries.sort((a, b) => a.kb_id.localeCompare(b.kb_id));
    const doc: KnowledgeIndex = {
      schema_version: INDEX_SCHEMA_VERSION,
      built_at: new Date().toISOString(),
      entries,
    };
    await this.store.write(doc);
    return doc;
  }

  /** 单篇更新（融合落盘后调用，避免整表重扫）。 */
  async upsert(knowledgeFolder: string, path: string): Promise<void> {
    const doc = await this.read();
    let text: string;
    try {
      text = await this.fs.read(path);
    } catch {
      return;
    }
    const entry = parseKnowledgeEntry(text, path);
    if (!entry) return;
    entry.body_hash = await this.bodyHash(text);
    const idx = doc.entries.findIndex((e) => e.kb_id === entry.kb_id);
    if (idx >= 0) doc.entries[idx] = entry;
    else doc.entries.push(entry);
    doc.built_at = new Date().toISOString();
    await this.store.write(doc);
    void knowledgeFolder;
  }

  async remove(kbId: string): Promise<void> {
    const doc = await this.read();
    doc.entries = doc.entries.filter((e) => e.kb_id !== kbId);
    doc.built_at = new Date().toISOString();
    await this.store.write(doc);
  }

  private async bodyHash(text: string): Promise<string> {
    const managed = extractPartition(text, KNOWLEDGE_START, KNOWLEDGE_END) ?? "";
    return sha256Hex(managed);
  }
}
