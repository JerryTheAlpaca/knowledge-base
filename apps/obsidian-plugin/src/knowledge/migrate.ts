/**
 * 本地迁移（docs/23 §8.2、§8.3）：两件独立验收的事。
 *
 * 1. 旧知识与旧证据 → 直连原文：把旧 `evidence_map` 每条来源展开成直接 Source 引用，
 *    多条来源分别保留（两份材料的 `s0001` 不合并）；不调用模型、不重写人工内容；
 *    正文与旧映射无法可靠对应的主题**不自动改写**，进可读的「迁移待处理」报告。
 * 2. 文件重命名：旧路径→新路径映射、冲突检测、经 Obsidian 的重命名能力改名、
 *    更新受管理链接与路径索引、留可反查的恢复记录；不确定的旧路径保留带跳转说明的
 *    兼容入口（同一 `kb_id` 绝不出现第二份可写副本）。
 *
 * 两步互不依赖：内容协议迁移只改正文与引用表，重命名只改路径与链接，分别可验收。
 *
 * 纯逻辑模块：文件访问与改名回调都通过注入，可在 Node 下测试。
 */

import type { FsLike } from "../vault/records";
import { JsonStore } from "../vault/records";
import {
  KNOWLEDGE_END,
  KNOWLEDGE_HISTORY_END,
  KNOWLEDGE_HISTORY_START,
  KNOWLEDGE_START,
  extractPartition,
  readFrontmatterValue,
  replacePartition,
  sha256Hex,
} from "../vault/template";
import {
  digestNotePath,
  documentsIndexPath,
  knowledgeNotePath,
  migrationDir,
  noteDateFromPath,
  renameRecordPath,
  revisionDir,
  sanitizeTitle,
  sourceAssetsDir,
  sourceNotePath,
  withConflictSuffix,
} from "../vault/paths";
import { DocumentIndex, listMarkdown, type DocumentIndexEntry } from "../vault/documents";
import {
  digestSnapshotPath,
  expandLegacyEvidenceMap,
  refAnchor,
  sourceAnchor,
  TopicReferenceStore,
  type LegacySourceRef,
} from "./citations";
import { ORGANIZE_RULE_VERSION, type ContentRefV3, type KbSettings } from "../types";

/** 旧正文里的 `[c0001]` 文本标记、`^c0001` 块锚点与两跳依据后缀。 */
const LEGACY_CITATION_SUFFIX = /（依据：[^）]*）/g;
const CLAIM_MARKER = /\[(c\d{4})\]/g;
const CLAIM_ANCHOR = /\s+\^(c\d{4})\s*$/;

/** 一篇旧 Knowledge 笔记的转换结果；`text === null` 表示不自动改写。 */
export interface LegacyNoteMigration {
  text: string | null;
  refs: LegacySourceRef[];
  errors: string[];
  history_lines: string[];
}

/** 旧证据条目 → v3 引用表条目（哈希由调用方按本地固定原文补算）。 */
export function legacyToContentRef(ref: LegacySourceRef): ContentRefV3 {
  return {
    item_id: ref.item_id,
    source_revision: ref.source_revision,
    segment_ids: [...ref.segment_ids],
    source_text_hash: "",
    locator: null,
  };
}

/**
 * 把旧两跳证据转成直连原文引用。
 *
 * 只在管理区内改写：`[c0001]` 文本标记与旧快照依据换成直接原文链接；旧观点原文与
 * 旧 `^c0001` 锚点移入折叠的「历史引用」区并标注所属历史版本。管理区缺失、正文观点
 * 没有映射、或映射缺 `digest_claim_id`（不能拿 knowledge claim_id 顶替）时不改写。
 */
export function migrateLegacyKnowledgeNote(input: {
  kbId: string;
  revision: number;
  text: string;
  evidenceMap: Record<string, unknown>;
  sourcesFolder: string;
  systemFolder: string;
}): LegacyNoteMigration {
  const { revision, text, evidenceMap } = input;
  const managed = extractPartition(text, KNOWLEDGE_START, KNOWLEDGE_END);
  if (managed === null) {
    return { text: null, refs: [], errors: ["缺少 kb:knowledge 管理区标记，不能确定可写范围"], history_lines: [] };
  }
  const { refs, errors } = expandLegacyEvidenceMap(evidenceMap);
  const byClaim = new Map<string, LegacySourceRef[]>();
  for (const ref of refs) byClaim.set(ref.claim_id, [...(byClaim.get(ref.claim_id) ?? []), ref]);
  const bodyClaims = [...new Set([
    ...[...managed.matchAll(CLAIM_MARKER)].map((m) => m[1]),
    ...[...managed.matchAll(/\^\(?(c\d{4})\)?\s*$/gm)].map((m) => m[1]),
  ])];
  if (!bodyClaims.length && !refs.length) {
    return { text: null, refs, errors: ["正文没有旧观点标记，也没有证据映射：结构未知，不自动处理"], history_lines: [] };
  }
  for (const claim of bodyClaims) {
    if (!byClaim.has(claim)) errors.push(`${claim}：正文出现该观点，但证据映射里没有对应条目`);
  }
  if (errors.length) return { text: null, refs, errors, history_lines: [] };

  const history: string[] = [];
  const rewritten = managed.split("\n").map((line) => {
    const anchor = /\s+\^(c\d{4})\s*$/.exec(line)?.[1] ?? null;
    const markers = [...[...line.matchAll(CLAIM_MARKER)].map((m) => m[1]), ...(anchor ? [anchor] : [])];
    if (!markers.length) return line.replace(LEGACY_CITATION_SUFFIX, "");
    const plain = line
      .replace(LEGACY_CITATION_SUFFIX, "")
      .replace(CLAIM_MARKER, "")
      .replace(CLAIM_ANCHOR, "")
      .replace(/\s{2,}/g, " ")
      .trim();
    // 旧观点原文与旧锚点保留在折叠历史区，标明所属历史版本（docs/23 §8.2）
    for (const claim of markers) {
      const entries = byClaim.get(claim) ?? [];
      const oldAnchors = [...new Set(entries
        .filter((e) => e.digest_id && e.digest_revision)
        .map((e) => `[[${digestSnapshotPath(input.systemFolder, e.digest_id, e.digest_revision)}#^${e.digest_claim_id}|旧摘要观点]]`))];
      history.push(`- 主题 rev ${revision} · ${claim}：${plain}${oldAnchors.length ? `（旧锚点：${oldAnchors.join("、")}）` : "（无旧快照锚点）"}`);
    }
    // 多个来源分别成链，不按相同 segment_id 合并
    const links = [...new Set(markers.flatMap((claim) => (byClaim.get(claim) ?? []).map((e) =>
      `[[${sourceAnchor(input.sourcesFolder, e.item_id, e.source_revision, e.segment_ids[0])}|查看原文]]`)))];
    return `${plain}${links.length ? ` （依据：${links.join("、")}）` : ""}`;
  }).join("\n");

  let next = replacePartition(text, KNOWLEDGE_START, KNOWLEDGE_END, rewritten.trim());
  const existingHistory = extractPartition(next, KNOWLEDGE_HISTORY_START, KNOWLEDGE_HISTORY_END) ?? "";
  next = replacePartition(next, KNOWLEDGE_HISTORY_START, KNOWLEDGE_HISTORY_END,
    [...existingHistory.split("\n").filter(Boolean), ...history].join("\n").trim());
  return { text: next, refs, errors: [], history_lines: history };
}

/** 旧 evidence-map.json 路径（迁移期只读；新流程不再写这种结构）。 */
export function legacyEvidenceMapPath(systemFolder: string, kbId: string): string {
  return `${revisionDir(systemFolder, "knowledge", kbId)}/evidence-map.json`;
}

export interface LegacyMigrationOutcome {
  migrated: Array<{ kb_id: string; path: string; refs: number; history_lines: number }>;
  pending: Array<{ kb_id: string; path: string; reason: string }>;
  unchanged: number;
  conflicts: Array<{ kb_id: string; paths: string[] }>;
}

/**
 * 一次可重入的旧知识转换：正文能可靠对应才改，改前先存转换前正文。
 *
 * 转换后的主题引用表按「正文版本 + 1」保存，供之后融合复用同一依据；
 * 旧 Digest 快照留在原路径不删，既有两跳链接仍能打开。
 */
export async function runLegacyKnowledgeMigration(
  fs: FsLike,
  settings: KbSettings,
  opts: { entries?: DocumentIndexEntry[] } = {},
): Promise<LegacyMigrationOutcome> {
  const docs = new DocumentIndex(fs, documentsIndexPath(settings.systemFolder));
  await docs.ensure(indexRoots(settings));
  const refsStore = new TopicReferenceStore(fs, settings.systemFolder);
  const out: LegacyMigrationOutcome = { migrated: [], pending: [], unchanged: 0, conflicts: await docs.conflicts() };
  const entries = opts.entries ?? (await docs.entries()).filter((e) => e.kind === "knowledge");

  for (const entry of entries) {
    if (!(await fs.exists(entry.path))) {
      out.pending.push({ kb_id: entry.kb_id, path: entry.path, reason: "登记的笔记路径已不存在（可能被改名或移动），请重建索引" });
      continue;
    }
    const text = await fs.read(entry.path);
    const mapPath = legacyEvidenceMapPath(settings.systemFolder, entry.kb_id);
    const hasLegacyMarks = CLAIM_MARKER.test(text) || /\^\(?(c\d{4})\)?\s*$/m.test(text);
    CLAIM_MARKER.lastIndex = 0;
    if (!hasLegacyMarks && !(await fs.exists(mapPath))) { out.unchanged += 1; continue; }
    let evidenceMap: Record<string, unknown> = {};
    if (await fs.exists(mapPath)) {
      try {
        evidenceMap = JSON.parse(await fs.read(mapPath)) as Record<string, unknown>;
      } catch {
        out.pending.push({ kb_id: entry.kb_id, path: entry.path, reason: "旧 evidence-map.json 无法解析" });
        continue;
      }
    }
    const revision = Number(readFrontmatterValue(text, "kb_revision") ?? "0") || 0;
    const migrated = migrateLegacyKnowledgeNote({
      kbId: entry.kb_id, revision, text, evidenceMap,
      sourcesFolder: settings.sourcesFolder, systemFolder: settings.systemFolder,
    });
    if (migrated.text === null) {
      out.pending.push({ kb_id: entry.kb_id, path: entry.path, reason: migrated.errors.join("；") });
      continue;
    }
    // 改前留档：转换出问题时按此文件恢复正文，不动人工区
    await fs.write(`${migrationDir(settings.systemFolder)}/legacy-${entry.kb_id}-r${revision}.md`, text);

    const references: Record<string, ContentRefV3> = {};
    let n = 0;
    for (const ref of migrated.refs) {
      const content = legacyToContentRef(ref);
      content.source_text_hash = await sha256Hex(await refOriginalText(fs, settings, ref));
      references[`e${++n}`] = content;
    }
    await fs.write(entry.path, migrated.text);
    const bodyHash = await sha256Hex(
      (extractPartition(migrated.text, KNOWLEDGE_START, KNOWLEDGE_END) ?? "").trim());
    await refsStore.save(entry.kb_id, {
      revision: revision + 1, document_id: entry.kb_id, body_hash: bodyHash, references,
    });
    await new JsonStore<Record<string, unknown>>(fs,
      `${migrationDir(settings.systemFolder)}/legacy-${entry.kb_id}.json`, () => ({})).write({
      kb_id: entry.kb_id,
      path: entry.path,
      rule_version: ORGANIZE_RULE_VERSION,
      from_revision: revision,
      to_revision: revision + 1,
      refs: n,
      history_lines: migrated.history_lines.length,
      migrated_at: new Date().toISOString(),
    });
    out.migrated.push({ kb_id: entry.kb_id, path: entry.path, refs: n, history_lines: migrated.history_lines.length });
  }
  await fs.write(`${migrationDir(settings.systemFolder)}/迁移待处理.md`, renderMigrationReport(out));
  return out;
}

/** 「迁移待处理」报告：列出不能自动改写的主题与身份冲突，保留原文和旧链接。 */
export function renderMigrationReport(out: LegacyMigrationOutcome): string {
  const lines = [
    "# 本地知识迁移结果",
    "",
    `已转换 ${out.migrated.length} 个主题；无需处理 ${out.unchanged} 个；待处理 ${out.pending.length} 个。`,
    "",
    "## 迁移待处理",
    "",
    "以下主题不自动改写：保留原正文与旧链接，请人工处理或显式重新整理。",
    "",
  ];
  if (!out.pending.length) lines.push("（无）", "");
  for (const p of out.pending) lines.push(`- [[${p.path}]]（${p.kb_id}）：${p.reason}`, "");
  if (out.migrated.length) {
    lines.push("## 已转换", "");
    for (const m of out.migrated) lines.push(`- [[${m.path}]]：${m.refs} 条直连原文依据，${m.history_lines} 行历史引用归档`, "");
  }
  if (out.conflicts.length) {
    lines.push("## 身份冲突（同一 kb_id 多个文件，两份都保留）", "");
    for (const c of out.conflicts) lines.push(`- ${c.kb_id}：${c.paths.join("、")}`, "");
  }
  return lines.join("\n");
}

async function refOriginalText(fs: FsLike, settings: KbSettings, ref: LegacySourceRef): Promise<string> {
  const path = `${sourceAssetsDir(settings.sourcesFolder, ref.item_id, ref.source_revision)}/segments.json`;
  if (!(await fs.exists(path))) return "";
  try {
    const doc = JSON.parse(await fs.read(path)) as { segments?: Array<{ segment_id?: string; text?: string }> };
    const table = new Map((doc.segments ?? []).map((s) => [String(s.segment_id), s.text ?? ""]));
    return ref.segment_ids.map((sid) => table.get(sid) ?? "").join("\n");
  } catch {
    return "";
  }
}

function indexRoots(settings: KbSettings): Array<{ folder: string; kind: DocumentIndexEntry["kind"]; skipDirs?: string[] }> {
  return [
    { folder: settings.sourcesFolder, kind: "source", skipDirs: ["_assets"] },
    { folder: settings.digestsFolder, kind: "digest" },
    { folder: settings.knowledgeFolder, kind: "knowledge" },
  ];
}

// ---- 文件重命名迁移（docs/23 §8.3） ----

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

/**
 * 生成旧路径→新路径映射。
 *
 * 只处理仍带 `--<item_id>` 后缀或不在日期目录里的 Source/Digest 笔记；同目录同名按
 * 明确规则追加 `（2）`、`（3）`；日期或标题无法确定、以及文件名带 item_id 的主题笔记
 * 列入 blocked 保持原样，不猜。
 */
export function planRenames(
  entries: RenameEntry[],
  folders: { sources: string; digests: string; knowledge: string },
): RenamePlan {
  const plan: RenameItem[] = [];
  const blocked: RenamePlan["blocked"] = [];
  const used = new Set<string>();
  for (const entry of [...entries].sort((a, b) => a.path.localeCompare(b.path))) {
    const base = entry.path.split("/").pop() ?? entry.path;
    const hasIdSuffix = /--[A-Za-z0-9_-]+\.md$/.test(base);
    if (entry.kind === "knowledge") {
      if (hasIdSuffix) blocked.push({ kb_id: entry.kb_id, path: entry.path, reason: "主题文件名带 item_id 后缀，需要人工确认主题名" });
      continue;
    }
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
    `> 这篇笔记已由 Knowledge Inbox 改名为可阅读文件名，内容在 [[${newPath}]]。`,
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
    sources: settings.sourcesFolder, digests: settings.digestsFolder, knowledge: settings.knowledgeFolder,
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

/** 待迁移旧知识数量（供命令与面板提示，不读取无关内容）。 */
export async function countLegacyKnowledgeNotes(fs: FsLike, knowledgeFolder: string): Promise<number> {
  let n = 0;
  for (const path of await listMarkdown(fs, knowledgeFolder)) {
    try {
      const text = await fs.read(path);
      if (/\[(c\d{4})\]/.test(text) || /\^\(?(c\d{4})\)?\s*$/m.test(text)) n += 1;
    } catch {
      // 读不到的文件不计入
    }
  }
  return n;
}

/** 新引用一律直连固定原文；旧两跳快照路径仅用于确认历史链接仍可打开。 */
export function directSourceLinks(sourcesFolder: string, references: Record<string, ContentRefV3>): string[] {
  return [...new Set(Object.values(references)
    .map((ref) => refAnchor(sourcesFolder, ref))
    .filter((x): x is string => Boolean(x)))];
}

/** 供报告与测试：某主题当前的可读文件名。 */
export function readableKnowledgePath(knowledgeFolder: string, title: string): string {
  return knowledgeNotePath(knowledgeFolder, title);
}
