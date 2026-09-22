/**
 * 路径安全、三层目录与文件命名（docs/02 §12.3；docs/08 §2、§9）。
 * 纯逻辑模块：不依赖 obsidian，可独立测试。
 *
 * 三层：01 Sources 保存原始证据，02 Digests 提炼单个来源，
 * 03 Knowledge 按主题持续维护；附件放 Sources 下以便一起归档。
 */

const RESERVED_NAMES = new Set(
  ["CON", "PRN", "AUX", "NUL",
    ...Array.from({ length: 9 }, (_, i) => `COM${i + 1}`),
    ...Array.from({ length: 9 }, (_, i) => `LPT${i + 1}`)],
);

const ILLEGAL_CHARS = /[<>:"/\\|?*\u0000-\u001f]/g;

export class UnsafePathError extends Error {}

/** 校验并规范化服务器给出的 bundle 相对路径；拒绝绝对路径、`..`、盘符与控制字符。 */
export function assertSafeRelativePath(raw: string): string {
  if (typeof raw !== "string" || raw.length === 0) {
    throw new UnsafePathError("相对路径为空");
  }
  if (/[\u0000-\u001f]/.test(raw)) {
    throw new UnsafePathError(`相对路径含控制字符：${JSON.stringify(raw.slice(0, 40))}`);
  }
  const normalized = raw.replace(/\\/g, "/");
  if (normalized.startsWith("/") || /^[a-zA-Z]:/.test(normalized)) {
    throw new UnsafePathError(`拒绝绝对路径：${normalized}`);
  }
  const segments = normalized.split("/");
  for (const seg of segments) {
    if (seg.length === 0 || seg === "." || seg === "..") {
      throw new UnsafePathError(`拒绝非法路径段：${normalized}`);
    }
  }
  return normalized;
}

/** base + 相对路径，再次确认结果仍在 base 之下（双重防护）。 */
export function joinUnder(base: string, relative: string): string {
  const rel = assertSafeRelativePath(relative);
  const cleanBase = base.replace(/\\/g, "/").replace(/\/+$/, "");
  const joined = `${cleanBase}/${rel}`;
  if (!joined.startsWith(`${cleanBase}/`)) {
    throw new UnsafePathError(`路径逃逸：${joined}`);
  }
  return joined;
}

/** 清理标题：去 Windows 保留字符、尾部点/空格，限长，保留中文。 */
export function sanitizeTitle(raw: string, maxLen = 60): string {
  let t = (raw ?? "").normalize("NFC").replace(ILLEGAL_CHARS, " ").replace(/\s+/g, " ").trim();
  if (t.length > maxLen) {
    t = t.slice(0, maxLen).trimEnd();
  }
  t = t.replace(/[.\s]+$/, "");
  const stem = t.split(".")[0].toUpperCase();
  if (RESERVED_NAMES.has(stem)) {
    t = `_${t}`;
  }
  return t.length > 0 ? t : "未命名";
}

function datePart(iso: string | null | undefined): string {
  if (iso) {
    const m = /^(\d{4}-\d{2}-\d{2})/.exec(iso);
    if (m) return m[1];
  }
  const now = new Date();
  const mm = String(now.getMonth() + 1).padStart(2, "0");
  const dd = String(now.getDate()).padStart(2, "0");
  return `${now.getFullYear()}-${mm}-${dd}`;
}

function dateFolder(folder: string, iso: string | null | undefined): { dir: string; date: string } {
  const d = datePart(iso);
  const [y, m] = d.split("-");
  return { dir: `${folder}/${y}/${m}`, date: d };
}

/** Source 笔记的目标路径：`01 Sources/YYYY/MM/YYYY-MM-DD 标题.md`（docs/24 §8；无 item_id 后缀）。
 *
 * 这里只给**期望名**；同目录同名冲突由 `resolveAvailableNotePath` 追加 `（2）`，
 * 真实路径登记在文档索引里，不按目录重新猜。
 */
export function sourceNotePath(sourcesFolder: string, capturedAt: string | null | undefined, title: string | null): string {
  const { dir, date } = dateFolder(sourcesFolder, capturedAt);
  return `${dir}/${noteStem(date, title)}.md`.replace(/\\/g, "/");
}

/** Digest 笔记目标路径：`02 Digests/YYYY/MM/YYYY-MM-DD 标题.md`。
 *
 * 与 Source 同日期同标题，仅目录不同；`kb_id` 为 `dig-<item_id>` 以区分。
 */
export function digestNotePath(digestsFolder: string, capturedAt: string | null | undefined, title: string | null): string {
  const { dir, date } = dateFolder(digestsFolder, capturedAt);
  return `${dir}/${noteStem(date, title)}.md`.replace(/\\/g, "/");
}

function noteStem(date: string, title: string | null): string {
  return `${date} ${sanitizeTitle(title ?? "")}`;
}

/** 同目录同名冲突规则：追加全角括号序号 `（2）`、`（3）`（docs/23 §7.2）。 */
export function withConflictSuffix(path: string, n: number): string {
  if (n < 2) return path;
  const idx = path.lastIndexOf("/");
  const dir = idx === -1 ? "" : path.slice(0, idx + 1);
  const base = idx === -1 ? path : path.slice(idx + 1);
  const dot = base.lastIndexOf(".md");
  const stem = dot > 0 ? base.slice(0, dot) : base;
  const ext = dot > 0 ? base.slice(dot) : ".md";
  return `${dir}${stem}（${n}）${ext}`;
}

/**
 * 为一个新笔记挑选未被占用的路径：期望名被占（文件或索引里已登记给别的文档）时
 * 依次尝试 `（2）`、`（3）`。`taken` 必须同时检查文件系统与文档索引，避免为同一
 * `kb_id` 造出第二份可写副本（docs/23 §7.2、§8.3 第 5 条）。
 */
export async function resolveAvailableNotePath(
  desired: string,
  taken: (path: string) => Promise<boolean>,
  maxAttempts = 50,
): Promise<string> {
  if (!(await taken(desired))) return desired;
  for (let n = 2; n <= maxAttempts + 1; n++) {
    const candidate = withConflictSuffix(desired, n);
    if (!(await taken(candidate))) return candidate;
  }
  // 极端同名堆积：退回带时间戳的确定唯一名，仍按同一文档身份登记
  return withConflictSuffix(desired, maxAttempts + 1);
}

/** 从可读文件名还原展示标题：去掉 `YYYY-MM-DD ` 前缀与 `（n）` 冲突后缀。 */
export function noteTitleFromPath(path: string): string {
  const base = (path.split("/").pop() ?? path).replace(/\.md$/, "");
  return base.replace(/^\d{4}-\d{2}-\d{2}\s+/, "").replace(/（\d+）$/, "");
}

/** 文件名里的采集日期（旧库迁移时用来推断目录；没有则 null）。 */
export function noteDateFromPath(path: string): string | null {
  const base = path.split("/").pop() ?? path;
  return /^\d{4}-\d{2}-\d{2}/.exec(base)?.[0] ?? null;
}

/** Source 附件目录：01 Sources/_assets/<item_id>/source-000001/（docs/08 §2）。 */
export function sourceAssetsDir(sourcesFolder: string, itemId: string, sourceRevision: number): string {
  return `${sourcesFolder}/_assets/${itemId}/source-${String(sourceRevision).padStart(6, "0")}`;
}

/** Knowledge 笔记路径：03 Knowledge/<主题名>.md，用稳定主题名，不加日期（docs/08 §2）。 */
export function knowledgeNotePath(knowledgeFolder: string, title: string): string {
  return `${knowledgeFolder}/${sanitizeTitle(title)}.md`.replace(/\\/g, "/");
}

/** bundle 目录名：bundle-000003 */
export function bundleDirName(revision: number): string {
  return `bundle-${String(revision).padStart(6, "0")}`;
}

/** commit 标记文件名：<item_id>--000003.json */
export function commitMarkerName(itemId: string, revision: number): string {
  return `${itemId}--${String(revision).padStart(6, "0")}.json`;
}

/** 历史快照目录：99 System/KnowledgeInbox/revisions/<kind>/<id>/（docs/08 §2、§6.1）。 */
export function revisionDir(systemFolder: string, kind: "digests" | "knowledge", id: string): string {
  return `${systemFolder}/KnowledgeInbox/revisions/${kind}/${sanitizeTitle(id, 80)}`;
}

/** 快照文件名：r000003.md（零填充，便于排序与引用）。 */
export function revisionFileName(revision: number): string {
  return `r${String(revision).padStart(6, "0")}.md`;
}

/** 迁移清单目录：99 System/KnowledgeInbox/migrations/（docs/08 §2、§10）。 */
export function migrationDir(systemFolder: string): string {
  return `${systemFolder}/KnowledgeInbox/migrations`;
}

/** 整理任务目录：99 System/KnowledgeInbox/organize/（docs/08 §8.1）。 */
export function organizeDir(systemFolder: string): string {
  return `${systemFolder}/KnowledgeInbox/organize`;
}

/** 候选目录：99 System/KnowledgeInbox/proposals/（docs/08 §2）。 */
export function proposalsDir(systemFolder: string): string {
  return `${systemFolder}/KnowledgeInbox/proposals`;
}

/** 本地知识索引：99 System/KnowledgeInbox/knowledge-index.json（docs/08 §2、§5）。 */
export function knowledgeIndexPath(systemFolder: string): string {
  return `${systemFolder}/KnowledgeInbox/knowledge-index.json`;
}

/** 文档索引（ID→路径）：99 System/KnowledgeInbox/index/documents.json（docs/24 §8）。 */
export function documentsIndexPath(systemFolder: string): string {
  return `${systemFolder}/KnowledgeInbox/index/documents.json`;
}

/** 旧版内容迁移与文件重命名记录：99 System/KnowledgeInbox/migrations/（docs/23 §8.2、§8.3）。 */
export function renameRecordPath(systemFolder: string, stamp: string): string {
  return `${migrationDir(systemFolder)}/rename-${stamp}.json`;
}

/** 主题引用表快照：随正文版本一起保存，回滚时成对恢复（docs/23 §6.4 第 7 条）。 */
export function knowledgeRefsFileName(revision: number): string {
  return `refs-r${String(revision).padStart(6, "0")}.json`;
}

/** 生成稳定的 Knowledge `kb_id`：kn-<slug>；含非 ASCII 时附稳定短哈希。
 *
 * 纯 ASCII 标题直接转 slug（`Agent Operations` → `kn-agent-operations`）；
 * 含中文等非 ASCII 字符时附上标题哈希，避免「Agent 操作技巧」与
 * 「Agent 上下文管理」都退化成 `kn-agent` 而撞同一个 ID（docs/08 §2）。
 */
export function knowledgeIdFromTitle(title: string): string {
  const ascii = title.normalize("NFKD").replace(/[\u0300-\u036f]/g, "")
    .toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  if (ascii && !/[^\x00-\x7F]/.test(title)) return `kn-${ascii.slice(0, 40)}`;
  // 非 ASCII：用稳定短哈希，保证同名得同 ID、不同名不撞
  let h = 0x811c9dc5;
  for (let i = 0; i < title.length; i++) {
    h ^= title.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  const suffix = h.toString(16).padStart(8, "0");
  return ascii ? `kn-${ascii.slice(0, 24)}-${suffix}` : `kn-zh-${suffix}`;
}

/** 生成稳定的 Source/Digest `kb_id`（docs/08 §3.1、§3.2）。 */
export function sourceKbId(itemId: string): string {
  return `src-${itemId}`;
}

export function digestKbId(itemId: string): string {
  return `dig-${itemId}`;
}
