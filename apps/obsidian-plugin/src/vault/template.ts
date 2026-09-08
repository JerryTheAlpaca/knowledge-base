/**
 * Source 模板、kb_* frontmatter 外科式更新与生成区冲突检测（docs/02 §12.2、§12.3）。
 * 纯逻辑模块：不依赖 obsidian，可独立测试。
 */

import type { KbFileEntry, KbManifest } from "../types";

export const GEN_START = "<!-- kb:generated:start -->";
export const GEN_END = "<!-- kb:generated:end -->";

const COVERAGE_NOTES: Record<string, string> = {
  full_text: "已取得本次正文范围全文",
  partial_text: "仅取得部分正文",
  user_excerpt: "仅用户摘录",
  screenshots_only: "仅有截图（原文未取得）",
  transcript_only: "仅字幕/文字稿，不含视频画面",
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

/** kb_* frontmatter 行；只包含本插件管理的键（docs/02 §12.3）。 */
export function kbFrontmatterLines(manifest: KbManifest, status: string): string[] {
  const s = manifest.source;
  const lines: Array<[string, unknown]> = [
    ["kb_id", manifest.item_id],
    ["kb_bundle_revision", manifest.bundle_revision],
    ["kb_source_revision", manifest.source_revision],
    ["kb_source_type", s.platform],
    ["kb_status", status],
    ["kb_coverage", s.coverage],
    ["kb_captured_at", s.captured_at ?? null],
    ["kb_source_url", s.original_url ?? s.canonical_url ?? null],
    ["kb_topics", []],
  ];
  return lines
    .filter(([, v]) => v !== null && v !== undefined)
    .map(([k, v]) => `${k}: ${yamlValue(v)}`);
}

/** 把 preview.md 中的相对链接改写为本地资产路径，并剥掉重复的「用户备注」段。 */
export function rewritePreviewLinks(previewMd: string, assetsBase: string): string {
  const withoutNote = previewMd.replace(/\n## 用户备注\n[\s\S]*?(?=\n## |\s*$)/, "");
  return withoutNote.replace(
    /\[\[normalized#([A-Za-z0-9_-]+)(\|([^\]]*))?\]\]/g,
    (_m, sid: string, _lab, label: string | undefined) =>
      `[[${assetsBase}/normalized#^${sid}|${label ?? sid}]]`,
  );
}

function fileLabel(f: KbFileEntry): string {
  const p = f.relative_path;
  if (p === "normalized.md") return "完整文字稿";
  if (p === "capture.json") return "原始提交记录";
  if (p === "analysis.json") return "AI 结构化结果";
  if (p === "preview.md") return "";
  if (p.startsWith("uploads/")) return `原始附件：${p.split("/").pop() ?? p}`;
  if (/\.srt$|\.vtt$|subtitle/.test(p)) return "原始字幕";
  return p;
}

/** 未完成加工时的生成区占位（诚实标注状态，不伪造结果）。 */
export function renderGeneratedPending(manifest: KbManifest): string {
  const lines: string[] = ["## AI 加工", "", `服务器尚未完成 AI 加工（状态：${manifest.processing.state}）。`, ""];
  lines.push("原始材料已入库；成品发布后同步时会合并到此区域。");
  for (const w of manifest.warnings ?? []) lines.push(`> [!warning] ${w}`);
  const missing = manifest.missing_materials ?? [];
  if (missing.length > 0) {
    lines.push("", "缺失材料：" + missing.join("、"));
  }
  return lines.join("\n");
}

export function renderGenerated(manifest: KbManifest, previewMd: string | null, assetsBase: string): string {
  if (!previewMd) return renderGeneratedPending(manifest);
  let md = rewritePreviewLinks(previewMd, assetsBase).trim();
  const missing = manifest.missing_materials ?? [];
  if (missing.length > 0) {
    md += `\n\n> [!warning] 缺失材料：${missing.join("、")}`;
  }
  return md;
}

/** 完整 Source 笔记（新建场景，docs/02 §12.2）。 */
export function renderSourceNote(
  manifest: KbManifest,
  generatedMd: string,
  userNote: string | null,
  assetsBase: string,
  status: string,
): string {
  const s = manifest.source;
  const title = s.title?.trim() || "未命名";
  const fm = kbFrontmatterLines(manifest, status).join("\n");
  const date = (s.captured_at ?? "").slice(0, 10) || "未知日期";
  const author = s.author?.trim() || "未取得";
  const coverageNote = COVERAGE_NOTES[s.coverage] ?? s.coverage;
  const noteLines = [
    "---",
    fm,
    "---",
    "",
    `# ${title}`,
    "",
    `来源：${s.platform} · 作者：${author} · 采集于 ${date}`,
    "",
    `> [!info] 完整性：${coverageNote}。`,
    "",
    "## 我的备注",
    "",
    userNote?.trim() ? userNote.trim() : "（无）",
    "",
    GEN_START,
    generatedMd,
    GEN_END,
    "",
    "## 原始材料",
    "",
  ];
  const links: string[] = [];
  for (const f of manifest.files) {
    const label = fileLabel(f);
    if (!label) continue;
    links.push(`- [[${assetsBase}/${f.relative_path}|${label}]]`);
  }
  if (links.length === 0) links.push("（无）");
  noteLines.push(...links, "", "## 我的后续思考", "");
  return noteLines.join("\n");
}

/** 取出生成区内容（不含标记本身）；没有标记时返回 null。 */
export function extractGenerated(text: string): string | null {
  const start = text.indexOf(GEN_START);
  const end = text.indexOf(GEN_END);
  if (start === -1 || end === -1 || end < start) return null;
  let inner = text.slice(start + GEN_START.length, end);
  if (inner.startsWith("\n")) inner = inner.slice(1);
  if (inner.endsWith("\n")) inner = inner.slice(0, -1);
  return inner;
}

/**
 * 只替换 kb_* frontmatter 行，未知字段与注释原样保留（docs/02 §12.3）。
 * 不改动正文；生成区由调用方单独处理。
 */
export function mergeKbFrontmatter(existing: string, manifest: KbManifest, status: string): string {
  const newKb = kbFrontmatterLines(manifest, status);
  const trimmed = existing.replace(/^\uFEFF/, "");
  if (trimmed.startsWith("---\n")) {
    const endIdx = trimmed.indexOf("\n---", 4);
    if (endIdx !== -1) {
      const fmBody = trimmed.slice(4, endIdx);
      const rest = trimmed.slice(endIdx + 4);
      const kept = fmBody.split("\n").filter((line) => !/^kb_[a-z_]+:/.test(line));
      return `---\n${[...newKb, ...kept].join("\n")}\n---${rest}`;
    }
  }
  return `---\n${newKb.join("\n")}\n---\n${trimmed}`;
}

/** 00 Inbox 索引页（整页由插件管理）。 */
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
