/**
 * 路径安全与文件命名（docs/02 §12.3、验收 A16）。
 * 纯逻辑模块：不依赖 obsidian，可独立测试。
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

/** Source 笔记路径：10 Sources/YYYY/MM/YYYY-MM-DD 标题--<item_id>.md */
export function sourceNotePath(sourcesFolder: string, capturedAt: string | null | undefined, title: string | null, itemId: string): string {
  const d = datePart(capturedAt);
  const [y, m] = d.split("-");
  const name = `${d} ${sanitizeTitle(title ?? "")}--${itemId}`;
  return `${sourcesFolder}/${y}/${m}/${name}.md`.replace(/\\/g, "/");
}

/** bundle 目录名：bundle-000003 */
export function bundleDirName(revision: number): string {
  return `bundle-${String(revision).padStart(6, "0")}`;
}

/** commit 标记文件名：<item_id>--000003.json */
export function commitMarkerName(itemId: string, revision: number): string {
  return `${itemId}--${String(revision).padStart(6, "0")}.json`;
}
