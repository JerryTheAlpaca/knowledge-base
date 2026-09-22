/**
 * 三层模板、分区管理、frontmatter 外科式更新与冲突检测
 * （docs/02 §12.2、§12.3；docs/08 §3、§5、§6、§9）。
 *
 * 分区（docs/08 §3.2、§3.3）：
 * - Source：无机器生成区；正文只放原始证据与链接。
 * - Digest：`kb:cloud-digest`（云端更新）+ `kb:local-organize`（本地整理更新）
 *   + 人工区（用户管理）；三者单独记录哈希。
 * - Knowledge：`kb:knowledge`（AI 融合更新）+ 人工区（自动融合不改写）。
 *
 * 纯逻辑模块：不依赖 obsidian，可独立测试。
 *
 * v3 起：`kb:cloud-digest` 区内容由同版本 `content.json` 渲染（见 `vault/content.ts`），
 * 不再从 preview.md 的固定中文标题反解字段；preview.md 只作可阅读产物。
 */

import type {
  KbFileEntry,
  KbManifest,
  KnowledgeIndexEntry,
} from "../types";

/** 分区标记：Source 不再有生成区；保留旧标记用于旧库识别与迁移。 */
export const LEGACY_GEN_START = "<!-- kb:generated:start -->";
export const LEGACY_GEN_END = "<!-- kb:generated:end -->";
export const CLOUD_DIGEST_START = "<!-- kb:cloud-digest:start -->";
export const CLOUD_DIGEST_END = "<!-- kb:cloud-digest:end -->";
export const LOCAL_ORGANIZE_START = "<!-- kb:local-organize:start -->";
export const LOCAL_ORGANIZE_END = "<!-- kb:local-organize:end -->";
export const KNOWLEDGE_START = "<!-- kb:knowledge:start -->";
export const KNOWLEDGE_END = "<!-- kb:knowledge:end -->";
/** 旧 `^c0001` 观点原文与锚点的折叠历史区：第一次转新结构时写入，之后不被融合覆盖。 */
export const KNOWLEDGE_HISTORY_START = "<!-- kb:knowledge-history:start -->";
export const KNOWLEDGE_HISTORY_END = "<!-- kb:knowledge-history:end -->";
/** Source 正文区：可随来源版本更新（提取器改进后旧条目也能用上新正文）。 */
export const SOURCE_BODY_START = "<!-- kb:source-body:start -->";
export const SOURCE_BODY_END = "<!-- kb:source-body:end -->";

export const COVERAGE_NOTES: Record<string, string> = {
  full_text: "已取得本次正文范围全文",
  partial_text: "仅取得部分正文",
  user_excerpt: "仅用户摘录",
  screenshots_only: "仅有截图（原文未取得）",
  transcript_only: "已取得该分 P 字幕，不含视频画面",
  metadata_only: "仅元数据，正文未取得",
};

export function sha256Hex(text: string): Promise<string> {
  const data = new TextEncoder().encode(text);
  return crypto.subtle.digest("SHA-256", data).then((buf) => {
    const arr = new Uint8Array(buf);
    let hex = "";
    for (const b of arr) hex += b.toString(16).padStart(2, "0");
    return hex;
  });
}

function yamlString(v: string): string {
  return `"${v.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;
}

function yamlValue(v: unknown): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  if (Array.isArray(v)) {
    return `[${v.map((x) => (typeof x === "string" ? yamlString(x) : String(x))).join(", ")}]`;
  }
  return yamlString(String(v));
}

// ---- 分区读写 ----

export interface Partition {
  inner: string;
  hash: string;
}

/** 取出标记之间的内容（不含标记本身）；没有标记时返回 null。 */
export function extractPartition(text: string, startMark: string, endMark: string): string | null {
  const start = text.indexOf(startMark);
  const end = text.indexOf(endMark);
  if (start === -1 || end === -1 || end < start) return null;
  let inner = text.slice(start + startMark.length, end);
  if (inner.startsWith("\n")) inner = inner.slice(1);
  if (inner.endsWith("\n")) inner = inner.slice(0, -1);
  return inner;
}

/** 替换分区内容，保留标记之外的原文（人工区与未知内容不动）。
 *
 * 标记缺失时**追加**一个完整分区而不是丢弃内容：旧库笔记或用户重写过结构时，
 * 静默丢掉模型结果会让用户以为整理成功（docs/08 §3.2、§7.2）。
 */
export function replacePartition(text: string, startMark: string, endMark: string, inner: string): string {
  const start = text.indexOf(startMark);
  const end = text.indexOf(endMark);
  if (start === -1 || end === -1 || end < start) {
    const trimmed = text.replace(/\s+$/, "");
    return `${trimmed}\n\n${startMark}\n${inner}\n${endMark}\n`;
  }
  return `${text.slice(0, start + startMark.length)}\n${inner}\n${text.slice(end)}`;
}

/** 三个分区各自的哈希；缺失的分区为 null（docs/08 §3.2「三者单独记录哈希」）。 */
export async function partitionHashes(text: string): Promise<{
  cloud_digest: string | null;
  local_organize: string | null;
  knowledge: string | null;
}> {
  const cloud = extractPartition(text, CLOUD_DIGEST_START, CLOUD_DIGEST_END);
  const local = extractPartition(text, LOCAL_ORGANIZE_START, LOCAL_ORGANIZE_END);
  const kn = extractPartition(text, KNOWLEDGE_START, KNOWLEDGE_END);
  return {
    cloud_digest: cloud === null ? null : await sha256Hex(cloud),
    local_organize: local === null ? null : await sha256Hex(local),
    knowledge: kn === null ? null : await sha256Hex(kn),
  };
}

// ---- frontmatter ----

function commonKbLines(manifest: KbManifest, status: string): string[] {
  const s = manifest.source;
  const lines: Array<[string, unknown]> = [
    ["kb_bundle_revision", manifest.bundle_revision],
    ["kb_source_revision", manifest.source_revision],
    ["kb_source_type", s.platform],
    ["kb_status", status],
    ["kb_coverage", s.coverage],
    ["kb_captured_at", s.captured_at ?? null],
    ["kb_source_url", s.original_url ?? s.canonical_url ?? null],
  ];
  return lines
    .filter(([, v]) => v !== null && v !== undefined)
    .map(([k, v]) => `${k}: ${yamlValue(v)}`);
}

/** 只替换 kb_* frontmatter 行，未知字段与注释原样保留（docs/02 §12.3；docs/08 §3.3）。
 *
 * 不整块重写 frontmatter：用户自定义字段、aliases、tags 必须保留。
 */
export function mergeKbFrontmatter(existing: string, kbLines: string[]): string {
  const trimmed = existing.replace(/^\uFEFF/, "");
  if (trimmed.startsWith("---\n")) {
    const endIdx = trimmed.indexOf("\n---", 4);
    if (endIdx !== -1) {
      const fmBody = trimmed.slice(4, endIdx);
      const rest = trimmed.slice(endIdx + 4);
      const kept = fmBody.split("\n").filter((line) => !/^kb_[a-z_]+:/.test(line));
      return `---\n${[...kbLines, ...kept].join("\n")}\n---${rest}`;
    }
  }
  return `---\n${kbLines.join("\n")}\n---\n${trimmed}`;
}

/** 读取 frontmatter 中某个键的原始文本值（不解析完整 YAML）。 */
export function readFrontmatterValue(text: string, key: string): string | null {
  const m = new RegExp(`^${key}:\\s*(.*)$`, "m").exec(text);
  if (!m) return null;
  let v = m[1].trim();
  if (v.startsWith('"') && v.endsWith('"') && v.length >= 2) v = v.slice(1, -1);
  return v || null;
}

/** 读取 frontmatter 数组值（支持 [a, b] 与 - a 两种写法）。 */
export function readFrontmatterList(text: string, key: string): string[] {
  const inline = new RegExp(`^${key}:\\s*\\[(.*)\\]\\s*$`, "m").exec(text);
  if (inline) {
    return inline[1].split(",").map((s) => s.trim().replace(/^"|"$/g, "")).filter(Boolean);
  }
  const block = new RegExp(`^${key}:\\s*\\n((?:[ \\t]*-\\s*.*\\n?)+)`, "m").exec(text);
  if (block) {
    return block[1].split("\n").map((l) => l.replace(/^[ \t]*-[ \t]*/, "").trim().replace(/^"|"$/g, "")).filter(Boolean);
  }
  return [];
}

// ---- 标签（docs/08 §5） ----

/** 系统管理的标签命名空间：`type/*` 与 `status/*`。 */
const MANAGED_TAG_PREFIXES = ["type/", "status/"];

export function isManagedTag(tag: string): boolean {
  return MANAGED_TAG_PREFIXES.some((p) => tag.startsWith(p));
}

/** 系统管理标签：类型 + 处理状态展示（`status/*` 由 `kb_*` 派生，不独立维护）。 */
export function managedTags(kind: "source" | "digest" | "knowledge", state: string | null): string[] {
  const tags = [`type/${kind}`];
  if (state) tags.push(`status/${state}`);
  return tags;
}

/**
 * 只增删系统管理的标签，保留用户其他标签（docs/08 §5）。
 *
 * 写入 `tags: [...]` 单行；用户自定义标签原样保留，系统标签按当前状态重建，
 * 避免 `kb_*` 与 `status/*` 双向独立维护而逐渐不一致。
 */
export function mergeManagedTags(existing: string, managed: string[]): string {
  const userTags = readFrontmatterList(existing, "tags").filter((t) => !isManagedTag(t));
  const all = [...new Set([...managed, ...userTags])];
  const line = `tags: [${all.join(", ")}]`;
  const inline = /^tags:\s*\[.*\]\s*$/m;
  if (inline.test(existing)) return existing.replace(inline, line);
  // 块状写法或无 tags：在 frontmatter 内替换/插入
  const block = /^tags:\s*\n(?:[ \t]*-[ \t]*.*\n?)+/m;
  if (block.test(existing)) return existing.replace(block, `${line}\n`);
  const endIdx = existing.startsWith("---\n") ? existing.indexOf("\n---", 4) : -1;
  if (endIdx !== -1) return `${existing.slice(0, endIdx)}\n${line}${existing.slice(endIdx)}`;
  return existing;
}

// ---- Source 模板 ----

function fileLabel(f: KbFileEntry): string {
  const p = f.relative_path;
  if (p === "normalized.md") return "完整文字稿";
  if (p === "readable.md") return "";  // 段落版正文已嵌入笔记，不重复列入原件
  if (p === "capture.json") return "原始提交记录";
  if (p === "analysis.json") return "云端结构化结果";
  if (p === "preview.md") return "";
  if (p === "segments.json") return "原文片段索引";
  if (p.startsWith("uploads/")) return `原始附件：${p.split("/").pop() ?? p}`;
  if (/\.srt$|\.vtt$|subtitle/.test(p)) return "原字幕";
  return p;
}

/** 嵌入 Source 的正文：去掉块 ID（段落 ^p0001 / 片段 ^s0001），只留可读文字。 */
export function stripSegmentIds(normalizedMd: string): string {
  return normalizedMd.replace(/\s+\^[sp]\d{4}\s*$/gm, "");
}

/** Source 笔记（docs/08 §3.1）：来源元数据、完整性说明、对应 Digest 链接、原件链接、可读原文。
 *
 * Source 正文不含 AI 总结；长字幕嵌入本地 normalized.md，不把摘要当正文。
 */
export function renderSourceNote(
  manifest: KbManifest,
  opts: {
    assetsBase: string;
    digestLink: string | null;
    status: string;
    normalizedText: string | null;
  },
): string {
  const s = manifest.source;
  const title = s.title?.trim() || "未命名";
  const date = (s.captured_at ?? "").slice(0, 10) || "未知日期";
  const author = s.author?.trim() || "未取得";
  const coverageNote = COVERAGE_NOTES[s.coverage] ?? s.coverage;
  const fm = mergeKbFrontmatter("", [
    `kb_id: ${yamlValue(`src-${manifest.item_id}`)}`,
    `kb_item_id: ${yamlValue(manifest.item_id)}`,
    "kb_type: source",
    ...commonKbLines(manifest, opts.status),
  ]);
  const fmWithTags = mergeManagedTags(fm, managedTags("source", opts.status));

  const lines: string[] = [
    fmWithTags,
    "",
    `# ${title}`,
    "",
    `来源：${s.platform}；作者：${author}；采集于 ${date}。`,
  ];
  const locator = s.source_locator ?? {};
  const locParts: string[] = [];
  if (locator.part) locParts.push(`分 P ${String(locator.part)}`);
  if (locator.cid) locParts.push(`cid ${String(locator.cid)}`);
  if (locator.bvid) locParts.push(String(locator.bvid));
  if (s.original_url) locParts.push(s.original_url);
  if (locParts.length) lines.push(`定位：${locParts.join(" · ")}。`);
  lines.push("", `完整性：${coverageNote}。`);
  const missing = manifest.missing_materials ?? [];
  if (missing.length) lines.push(`缺失材料：${missing.join("、")}。`);
  lines.push(
    "",
    `提炼：${opts.digestLink ? opts.digestLink : "（尚未生成 Digest）"}`,
    "",
    "## 原始材料",
    "",
  );

  const links: string[] = [];
  for (const f of manifest.files) {
    const label = fileLabel(f);
    if (!label) continue;
    links.push(`- [[${opts.assetsBase}/${f.relative_path}|${label}]]`);
  }
  if (links.length === 0) links.push("（无）");
  lines.push(...links, "", "## 完整文字稿", "");
  lines.push(SOURCE_BODY_START);
  if (opts.normalizedText && opts.normalizedText.trim()) {
    lines.push(stripSegmentIds(opts.normalizedText).trim());
  } else {
    lines.push("（未取得可读正文；覆盖说明如实反映缺失，不用标题补写。）");
  }
  lines.push(SOURCE_BODY_END, "");
  return lines.join("\n");
}

// ---- Digest 模板 ----

/** 云端尚无可用提炼时的诚实占位（docs/24 §8：绝不写看似有效的空白摘要）。 */
export function renderCloudPending(manifest: KbManifest): string {
  const state = manifest.processing.state;
  const lines = ["## 云端整理结果", ""];
  if (state === "failed") {
    lines.push("云端整理失败，原始资料已完整保存；可在网页端重新发起整理，成功后同步会替换本区域。");
  } else if (state === "ready") {
    lines.push("云端整理结果未能读取（内容文件缺失或格式不受支持）；原始资料已保存，请升级插件后重新同步。");
  } else {
    lines.push(`云端整理尚未完成（状态：${state || "未知"}）；原始资料已保存，完成后同步会替换本区域。`);
  }
  for (const w of manifest.warnings ?? []) lines.push(``, `> [!warning] ${w}`);
  const missing = manifest.missing_materials ?? [];
  if (missing.length) lines.push("", "缺失材料：" + missing.join("、"));
  return lines.join("\n");
}

/** 本地整理区初始内容（docs/08 §3.2）：首次投递显示「尚未本地整理」。 */
export function renderLocalOrganizeInitial(): string {
  return [
    "## 与已有知识的关系",
    "",
    "尚未本地整理。",
  ].join("\n");
}

/** 本地整理区（主题候选结论，docs/23 §6.1）：不再逐观点列晋升建议。
 *
 * `resolveLink` 只把已解析到真实笔记的主题渲染成链接；未落地的新主题用普通文字
 * 写建议，不生成指向不存在文件的链接。
 */
export function renderLocalOrganize(input: {
  relationNote: string | null;
  targetTitle: string | null;
  targetPath: string | null;
  reason: string | null;
  outcome: "candidate" | "keep_digest" | "no_change" | "none";
  proposalTitle?: string | null;
}): string {
  const lines = ["## 与已有知识的关系", ""];
  lines.push(input.relationNote?.trim() || "尚未本地整理。", "");
  lines.push("## 本地整理结论", "");
  const target = input.targetPath
    ? `[[${input.targetPath}|${input.targetTitle ?? "已有主题"}]]`
    : (input.targetTitle ? `「${input.targetTitle}」（尚未创建）` : null);
  if (input.outcome === "keep_digest") {
    lines.push("本次材料对长期主题没有可靠增量，结论保留在本 Digest。");
  } else if (input.outcome === "no_change") {
    lines.push("模型判断现有主题无需修改；未写入 Knowledge。");
  } else if (input.outcome === "candidate") {
    lines.push(`已生成主题修改候选${target ? `：${target}` : ""}，采纳前不会写入 Knowledge。`);
  } else {
    lines.push("尚未本地整理。");
  }
  if (input.reason) lines.push("", `原因：${input.reason}`);
  return lines.join("\n");
}

/** Digest 笔记（docs/08 §3.2）：来源链接 + 云端区 + 本地整理区 + 人工区。
 *
 * 云端区内容由同版本 `content.json` 渲染（见 `vault/content.ts`），Markdown 只是可阅读产物。
 */
export function renderDigestNote(
  manifest: KbManifest,
  opts: {
    sourceLink: string | null;
    cloudMd: string | null;
    status: string;
    /** v3 内容身份与完整度，写进 frontmatter 供回读定位。 */
    contentLines?: string[];
  },
): string {
  const s = manifest.source;
  const title = s.title?.trim() || "未命名";
  const kbId = `dig-${manifest.item_id}`;
  const fm = mergeKbFrontmatter("", [
    `kb_id: ${yamlValue(kbId)}`,
    `kb_item_id: ${yamlValue(manifest.item_id)}`,
    "kb_type: digest",
    `kb_source_revision: ${manifest.source_revision}`,
    `kb_digest_revision: ${manifest.bundle_revision}`,
    "kb_organize: not_evaluated",
    ...(opts.contentLines ?? []),
  ]);
  // `status/*` 是 `kb_organize`（本地整理结论）的展示，不独立维护（docs/08 §5）
  const fmWithTags = mergeManagedTags(fm, managedTags("digest", "not_evaluated"));
  return [
    fmWithTags,
    "",
    `# ${title}：提炼`,
    "",
    `来源：${opts.sourceLink ? opts.sourceLink : "（原始资料尚未入库）"}`,
    "",
    CLOUD_DIGEST_START,
    opts.cloudMd?.trim() || renderCloudPending(manifest),
    CLOUD_DIGEST_END,
    "",
    LOCAL_ORGANIZE_START,
    renderLocalOrganizeInitial(),
    LOCAL_ORGANIZE_END,
    "",
    "## 我的备注与判断",
    "",
    "此区域归用户管理。",
    "",
  ].join("\n");
}

// ---- Knowledge 模板 ----

/** Knowledge 笔记（docs/08 §3.3）：主题说明 + 机器区 + 人工区。 */
export function renderKnowledgeNote(opts: {
  kbId: string;
  title: string;
  aliases: string[];
  scope: string;
  managedBody: string;
  revision: number;
  reviewedAt: string | null;
}): string {
  const fm = [
    "---",
    `kb_id: ${yamlValue(opts.kbId)}`,
    "kb_type: knowledge",
    `kb_revision: ${opts.revision}`,
    `kb_reviewed_at: ${opts.reviewedAt ?? new Date().toISOString().slice(0, 10)}`,
    `aliases: ${yamlValue(opts.aliases)}`,
    "---",
  ].join("\n");
  const fmWithTags = mergeManagedTags(fm, managedTags("knowledge", null));
  return [
    fmWithTags,
    "",
    `# ${opts.title}`,
    "",
    "## 这个主题解决什么问题",
    "",
    opts.scope?.trim() || "边界、适用对象，以及不在本篇展开的问题。",
    "",
    KNOWLEDGE_START,
    opts.managedBody.trim(),
    KNOWLEDGE_END,
    "",
    "## 我的实践与补充",
    "",
    "用户经验、偏好和手动记录，自动融合不改写。",
    "",
  ].join("\n");
}

/** 主题范围说明（frontmatter 之外的「这个主题解决什么问题」小节）。 */
export function extractKnowledgeScope(text: string): string {
  const m = /## 这个主题解决什么问题\s*\n+([\s\S]*?)(?=\n<!-- kb:knowledge:start|\n## |\s*$)/.exec(text);
  return (m?.[1] ?? "").trim();
}

// ---- 00 Inbox 索引 ----

/** 00 Inbox/待回顾.md（docs/08 §2、§5：插件输出普通 Markdown，无需 Dataview）。 */
export function renderInboxIndex(
  entries: Array<{ notePath: string; title: string; status: string; capturedAt: string | null }>,
): string {
  const rows = [...entries]
    .sort((a, b) => (b.capturedAt ?? "").localeCompare(a.capturedAt ?? ""))
    .map((e) => `| ${(e.capturedAt ?? "").slice(0, 10) || "—"} | [[${e.notePath}|${e.title}]] | ${e.status} |`)
    .join("\n");
  return [
    "# Knowledge Inbox",
    "",
    "待回顾条目（由 Knowledge Inbox 插件维护；下方表格在每次同步后重建）。",
    "",
    "| 采集日期 | 条目 | 状态 |",
    "| --- | --- | --- |",
    rows || "| — | （暂无条目） | — |",
    "",
  ].join("\n");
}

/** 00 Inbox/知识更新候选.md（docs/23 §6.1；取代旧逐观点晋升候选索引）。 */
export function renderProposalIndex(
  entries: Array<{
    proposalId: string;
    knowledgeTitle: string | null;
    state: string;
    changeSummary: string;
    createdAt: string;
    noOp: boolean;
  }>,
): string {
  const rows = [...entries]
    .sort((a, b) => b.createdAt.localeCompare(a.createdAt))
    .map((e) =>
      `| ${e.createdAt.slice(0, 10)} | ${e.knowledgeTitle ?? "（新建主题）"} | ${e.noOp ? "无变化" : (e.changeSummary || "—")} | ${e.state} |`,
    )
    .join("\n");
  return [
    "# 知识更新候选",
    "",
    "由本地整理生成；采纳前不会写入 Knowledge。使用命令「查看知识更新候选」或整理面板逐条查看差异与原文依据。",
    "",
    "| 生成日期 | 目标主题 | 变化摘要 | 状态 |",
    "| --- | --- | --- | --- |",
    rows || "| — | （暂无候选） | — | — |",
    "",
  ].join("\n");
}

/** 索引条目 -> 检索用纯文本（docs/08 §5：标题、kb_id、aliases、范围说明、检索关键词）。 */
export function indexEntrySearchText(e: KnowledgeIndexEntry): string {
  return [e.kb_id, e.title, ...e.aliases, e.scope, ...e.keywords].join("\n").toLowerCase();
}
