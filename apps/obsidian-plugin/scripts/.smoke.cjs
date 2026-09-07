"use strict";

// scripts/smoke.ts
var import_node_assert = require("node:assert");

// src/vault/paths.ts
var RESERVED_NAMES = /* @__PURE__ */ new Set(
  [
    "CON",
    "PRN",
    "AUX",
    "NUL",
    ...Array.from({ length: 9 }, (_, i) => `COM${i + 1}`),
    ...Array.from({ length: 9 }, (_, i) => `LPT${i + 1}`)
  ]
);
var ILLEGAL_CHARS = /[<>:"/\\|?*\u0000-\u001f]/g;
var UnsafePathError = class extends Error {
};
function assertSafeRelativePath(raw) {
  if (typeof raw !== "string" || raw.length === 0) {
    throw new UnsafePathError("\u76F8\u5BF9\u8DEF\u5F84\u4E3A\u7A7A");
  }
  if (/[\u0000-\u001f]/.test(raw)) {
    throw new UnsafePathError(`\u76F8\u5BF9\u8DEF\u5F84\u542B\u63A7\u5236\u5B57\u7B26\uFF1A${JSON.stringify(raw.slice(0, 40))}`);
  }
  const normalized = raw.replace(/\\/g, "/");
  if (normalized.startsWith("/") || /^[a-zA-Z]:/.test(normalized)) {
    throw new UnsafePathError(`\u62D2\u7EDD\u7EDD\u5BF9\u8DEF\u5F84\uFF1A${normalized}`);
  }
  const segments = normalized.split("/");
  for (const seg of segments) {
    if (seg.length === 0 || seg === "." || seg === "..") {
      throw new UnsafePathError(`\u62D2\u7EDD\u975E\u6CD5\u8DEF\u5F84\u6BB5\uFF1A${normalized}`);
    }
  }
  return normalized;
}
function joinUnder(base, relative) {
  const rel = assertSafeRelativePath(relative);
  const cleanBase = base.replace(/\\/g, "/").replace(/\/+$/, "");
  const joined = `${cleanBase}/${rel}`;
  if (!joined.startsWith(`${cleanBase}/`)) {
    throw new UnsafePathError(`\u8DEF\u5F84\u9003\u9038\uFF1A${joined}`);
  }
  return joined;
}
function sanitizeTitle(raw, maxLen = 60) {
  let t = (raw ?? "").normalize("NFC").replace(ILLEGAL_CHARS, " ").replace(/\s+/g, " ").trim();
  if (t.length > maxLen) {
    t = t.slice(0, maxLen).trimEnd();
  }
  t = t.replace(/[.\s]+$/, "");
  const stem = t.split(".")[0].toUpperCase();
  if (RESERVED_NAMES.has(stem)) {
    t = `_${t}`;
  }
  return t.length > 0 ? t : "\u672A\u547D\u540D";
}
function datePart(iso) {
  if (iso) {
    const m = /^(\d{4}-\d{2}-\d{2})/.exec(iso);
    if (m) return m[1];
  }
  const now = /* @__PURE__ */ new Date();
  const mm = String(now.getMonth() + 1).padStart(2, "0");
  const dd = String(now.getDate()).padStart(2, "0");
  return `${now.getFullYear()}-${mm}-${dd}`;
}
function sourceNotePath(sourcesFolder, capturedAt, title, itemId) {
  const d = datePart(capturedAt);
  const [y, m] = d.split("-");
  const name = `${d} ${sanitizeTitle(title ?? "")}--${itemId}`;
  return `${sourcesFolder}/${y}/${m}/${name}.md`.replace(/\\/g, "/");
}
function bundleDirName(revision) {
  return `bundle-${String(revision).padStart(6, "0")}`;
}

// src/vault/template.ts
var GEN_START = "<!-- kb:generated:start -->";
var GEN_END = "<!-- kb:generated:end -->";
var COVERAGE_NOTES = {
  full_text: "\u5DF2\u53D6\u5F97\u672C\u6B21\u6B63\u6587\u8303\u56F4\u5168\u6587",
  partial_text: "\u4EC5\u53D6\u5F97\u90E8\u5206\u6B63\u6587",
  user_excerpt: "\u4EC5\u7528\u6237\u6458\u5F55",
  screenshots_only: "\u4EC5\u6709\u622A\u56FE\uFF08\u539F\u6587\u672A\u53D6\u5F97\uFF09",
  transcript_only: "\u4EC5\u5B57\u5E55/\u6587\u5B57\u7A3F\uFF0C\u4E0D\u542B\u89C6\u9891\u753B\u9762",
  metadata_only: "\u4EC5\u5143\u6570\u636E\uFF0C\u6B63\u6587\u672A\u53D6\u5F97"
};
function yamlString(v) {
  return `"${v.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;
}
function yamlValue(v) {
  if (v === null || v === void 0) return "";
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  if (Array.isArray(v)) {
    return `[${v.map((x) => typeof x === "string" ? yamlString(x) : String(x)).join(", ")}]`;
  }
  return yamlString(String(v));
}
function kbFrontmatterLines(manifest2, status) {
  const s = manifest2.source;
  const lines = [
    ["kb_id", manifest2.item_id],
    ["kb_bundle_revision", manifest2.bundle_revision],
    ["kb_source_revision", manifest2.source_revision],
    ["kb_source_type", s.platform],
    ["kb_status", status],
    ["kb_coverage", s.coverage],
    ["kb_captured_at", s.captured_at ?? null],
    ["kb_source_url", s.original_url ?? s.canonical_url ?? null],
    ["kb_topics", []]
  ];
  return lines.filter(([, v]) => v !== null && v !== void 0).map(([k, v]) => `${k}: ${yamlValue(v)}`);
}
function rewritePreviewLinks(previewMd, assetsBase) {
  const withoutNote = previewMd.replace(/\n## 用户备注\n[\s\S]*?(?=\n## |\s*$)/, "");
  return withoutNote.replace(
    /\[\[normalized#([A-Za-z0-9_-]+)(\|([^\]]*))?\]\]/g,
    (_m, sid, _lab, label) => `[[${assetsBase}/normalized#^${sid}|${label ?? sid}]]`
  );
}
function fileLabel(f) {
  const p = f.relative_path;
  if (p === "normalized.md") return "\u5B8C\u6574\u6587\u5B57\u7A3F";
  if (p === "capture.json") return "\u539F\u59CB\u63D0\u4EA4\u8BB0\u5F55";
  if (p === "analysis.json") return "AI \u7ED3\u6784\u5316\u7ED3\u679C";
  if (p === "preview.md") return "";
  if (p.startsWith("uploads/")) return `\u539F\u59CB\u9644\u4EF6\uFF1A${p.split("/").pop() ?? p}`;
  if (/\.srt$|\.vtt$|subtitle/.test(p)) return "\u539F\u59CB\u5B57\u5E55";
  return p;
}
function renderSourceNote(manifest2, generatedMd, userNote, assetsBase, status) {
  const s = manifest2.source;
  const title = s.title?.trim() || "\u672A\u547D\u540D";
  const fm = kbFrontmatterLines(manifest2, status).join("\n");
  const date = (s.captured_at ?? "").slice(0, 10) || "\u672A\u77E5\u65E5\u671F";
  const author = s.author?.trim() || "\u672A\u53D6\u5F97";
  const coverageNote = COVERAGE_NOTES[s.coverage] ?? s.coverage;
  const noteLines = [
    "---",
    fm,
    "---",
    "",
    `# ${title}`,
    "",
    `\u6765\u6E90\uFF1A${s.platform} \xB7 \u4F5C\u8005\uFF1A${author} \xB7 \u91C7\u96C6\u4E8E ${date}`,
    "",
    `> [!info] \u5B8C\u6574\u6027\uFF1A${coverageNote}\u3002`,
    "",
    "## \u6211\u7684\u5907\u6CE8",
    "",
    userNote?.trim() ? userNote.trim() : "\uFF08\u65E0\uFF09",
    "",
    GEN_START,
    generatedMd,
    GEN_END,
    "",
    "## \u539F\u59CB\u6750\u6599",
    ""
  ];
  const links = [];
  for (const f of manifest2.files) {
    const label = fileLabel(f);
    if (!label) continue;
    links.push(`- [[${assetsBase}/${f.relative_path}|${label}]]`);
  }
  if (links.length === 0) links.push("\uFF08\u65E0\uFF09");
  noteLines.push(...links, "", "## \u6211\u7684\u540E\u7EED\u601D\u8003", "");
  return noteLines.join("\n");
}
function extractGenerated(text) {
  const start = text.indexOf(GEN_START);
  const end = text.indexOf(GEN_END);
  if (start === -1 || end === -1 || end < start) return null;
  let inner = text.slice(start + GEN_START.length, end);
  if (inner.startsWith("\n")) inner = inner.slice(1);
  if (inner.endsWith("\n")) inner = inner.slice(0, -1);
  return inner;
}
function mergeKbFrontmatter(existing, manifest2, status) {
  const newKb = kbFrontmatterLines(manifest2, status);
  const trimmed = existing.replace(/^\uFEFF/, "");
  if (trimmed.startsWith("---\n")) {
    const endIdx = trimmed.indexOf("\n---", 4);
    if (endIdx !== -1) {
      const fmBody = trimmed.slice(4, endIdx);
      const rest = trimmed.slice(endIdx + 4);
      const kept = fmBody.split("\n").filter((line) => !/^kb_[a-z_]+:/.test(line));
      return `---
${[...newKb, ...kept].join("\n")}${rest}`;
    }
  }
  return `---
${newKb.join("\n")}
---
${trimmed}`;
}

// scripts/smoke.ts
var passed = 0;
function ok(name, fn) {
  fn();
  passed += 1;
  console.log(`  \u2713 ${name}`);
}
ok("\u62D2\u7EDD ../ \u9003\u9038", () => {
  import_node_assert.strict.throws(() => assertSafeRelativePath("../escape.md"), UnsafePathError);
  import_node_assert.strict.throws(() => assertSafeRelativePath("a/../../b.md"), UnsafePathError);
});
ok("\u62D2\u7EDD\u7EDD\u5BF9\u8DEF\u5F84\u4E0E\u76D8\u7B26", () => {
  import_node_assert.strict.throws(() => assertSafeRelativePath("/etc/passwd"), UnsafePathError);
  import_node_assert.strict.throws(() => assertSafeRelativePath("C:/Windows/system32"), UnsafePathError);
  import_node_assert.strict.throws(() => assertSafeRelativePath("C:\\Windows\\system32"), UnsafePathError);
});
ok("joinUnder \u6821\u9A8C\u7ED3\u679C\u4ECD\u5728 base \u4E0B", () => {
  import_node_assert.strict.equal(
    joinUnder("90 Assets/KnowledgeInbox/x", "bundle-000001/normalized.md"),
    "90 Assets/KnowledgeInbox/x/bundle-000001/normalized.md"
  );
  import_node_assert.strict.throws(() => joinUnder("90 Assets", "..\\escape"), UnsafePathError);
});
ok("\u63A5\u53D7\u6B63\u5E38\u76F8\u5BF9\u8DEF\u5F84\u5E76\u89C4\u8303\u5316\u53CD\u659C\u6760", () => {
  import_node_assert.strict.equal(assertSafeRelativePath("uploads\\u1\\a.png"), "uploads/u1/a.png");
});
ok("\u6E05\u7406 Windows \u4FDD\u7559\u5B57\u7B26\u4E0E\u5C3E\u90E8\u70B9/\u7A7A\u683C", () => {
  import_node_assert.strict.equal(sanitizeTitle('demo<>:"/\\|?*title. '), "demo title");
  import_node_assert.strict.equal(sanitizeTitle("CON"), "_CON");
  import_node_assert.strict.equal(sanitizeTitle("      "), "\u672A\u547D\u540D");
  import_node_assert.strict.equal(sanitizeTitle("\u5F88\u957F\u7684\u6807\u9898".repeat(20)).length <= 60, true);
});
ok("Source \u7B14\u8BB0\u8DEF\u5F84\u5E26\u65E5\u671F\u4E0E item_id", () => {
  const p = sourceNotePath("10 Sources", "2026-09-07T09:20:00+08:00", "\u6F14\u793A\u6807\u9898", "item-demo");
  import_node_assert.strict.equal(p, "10 Sources/2026/09/2026-09-07 \u6F14\u793A\u6807\u9898--item-demo.md");
  import_node_assert.strict.equal(bundleDirName(3), "bundle-000003");
});
var manifest = {
  schema_version: "1.0",
  item_id: "item-demo",
  source_revision: 2,
  bundle_revision: 3,
  created_at: "2026-09-07T01:23:00Z",
  source: {
    platform: "bilibili",
    title: "\u6F14\u793A\u6807\u9898",
    author: null,
    original_url: "https://example.com/video/demo",
    canonical_url: null,
    published_at: null,
    captured_at: "2026-09-07T09:20:00+08:00",
    source_locator: {},
    coverage: "transcript_only",
    content_scope: "unknown",
    original_media_retained: false
  },
  processing: { state: "ready", recipe_version: "source-light-v1", result_file_id: "result-json", source_revision: 2 },
  files: [
    { file_id: "f1", relative_path: "normalized.md", role: "source_material", mime: "text/markdown", bytes: 10, sha256: "aa" },
    { file_id: "f2", relative_path: "uploads/u1/\u56FE.png", role: "original_submission", mime: "image/png", bytes: 10, sha256: "bb" }
  ],
  missing_materials: ["original_video"],
  warnings: [],
  expires_at: "2026-10-07T01:23:00Z"
};
ok("\u65B0\u5EFA Source \u7B14\u8BB0\u5305\u542B\u6211\u7684\u5907\u6CE8/\u751F\u6210\u533A\u6807\u8BB0/\u539F\u59CB\u6750\u6599\u94FE\u63A5", () => {
  const text = renderSourceNote(manifest, "## \u4E00\u53E5\u8BDD\u6458\u8981\n\n\u6F14\u793A\u3002", "\u6211\u7684\u5907\u6CE8\u5185\u5BB9", "90 Assets/KnowledgeInbox/item-demo/bundle-000003", "ready");
  import_node_assert.strict.ok(text.includes("## \u6211\u7684\u5907\u6CE8\n\n\u6211\u7684\u5907\u6CE8\u5185\u5BB9"));
  import_node_assert.strict.ok(text.includes(GEN_START) && text.includes(GEN_END));
  import_node_assert.strict.ok(text.includes("[[90 Assets/KnowledgeInbox/item-demo/bundle-000003/normalized.md|\u5B8C\u6574\u6587\u5B57\u7A3F]]"));
  import_node_assert.strict.ok(text.includes('kb_status: "ready"'));
});
ok("mergeKbFrontmatter \u66F4\u65B0 kb_* \u4E14\u4FDD\u7559\u672A\u77E5\u5B57\u6BB5", () => {
  const existing = [
    "---",
    'kb_id: "item-demo"',
    'kb_status: "ready"',
    'custom_field: "\u4FDD\u7559\u6211"',
    "---",
    "",
    "# \u6B63\u6587"
  ].join("\n");
  const merged = mergeKbFrontmatter(existing, manifest, "merge_needed");
  import_node_assert.strict.ok(merged.includes('custom_field: "\u4FDD\u7559\u6211"'));
  import_node_assert.strict.ok(merged.includes('kb_status: "merge_needed"'));
  import_node_assert.strict.ok(merged.includes("kb_bundle_revision: 3"));
  import_node_assert.strict.ok(merged.endsWith("# \u6B63\u6587"));
});
ok("extractGenerated \u63D0\u53D6\u6807\u8BB0\u95F4\u5185\u5BB9", () => {
  const text = `\u5934\u90E8
${GEN_START}
\u65E7\u751F\u6210\u5185\u5BB9
${GEN_END}
\u5C3E\u90E8`;
  import_node_assert.strict.equal(extractGenerated(text), "\u65E7\u751F\u6210\u5185\u5BB9");
  import_node_assert.strict.equal(extractGenerated("\u65E0\u6807\u8BB0"), null);
});
ok("preview.md \u94FE\u63A5\u6539\u5199\u4E3A\u672C\u5730\u8D44\u4EA7\u8DEF\u5F84\u5E76\u5265\u7528\u6237\u5907\u6CE8\u6BB5", () => {
  const md = "## \u6838\u5FC3\u89C2\u70B9\n\n- \u89C2\u70B9\uFF08[[normalized#s0001|s0001]]\uFF09\n\n## \u7528\u6237\u5907\u6CE8\n\n> \u5907\u6CE8";
  const out = rewritePreviewLinks(md, "90 Assets/KnowledgeInbox/i/bundle-000001");
  import_node_assert.strict.ok(out.includes("[[90 Assets/KnowledgeInbox/i/bundle-000001/normalized#^s0001|s0001]]"));
  import_node_assert.strict.ok(!out.includes("## \u7528\u6237\u5907\u6CE8"));
});
console.log(`
\u5168\u90E8 ${passed} \u9879\u901A\u8FC7`);
