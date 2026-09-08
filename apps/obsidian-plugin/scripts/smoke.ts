/**
 * 纯逻辑冒烟测试：路径安全（A16）、文件名清理、frontmatter 合并与生成区冲突判定。
 * 运行：node scripts/smoke.mjs（先 tsc 编译到 .smoke/，或直接由测试脚本内嵌断言）。
 * 由 esbuild 打包 src/smoke-entry.ts 后执行，不依赖 obsidian。
 */
import { strict as assert } from "node:assert";
import {
  assertSafeRelativePath,
  joinUnder,
  sanitizeTitle,
  sourceNotePath,
  bundleDirName,
  UnsafePathError,
} from "../src/vault/paths";
import {
  GEN_START,
  GEN_END,
  extractGenerated,
  mergeKbFrontmatter,
  renderSourceNote,
  rewritePreviewLinks,
} from "../src/vault/template";
import type { KbManifest } from "../src/types";

let passed = 0;
function ok(name: string, fn: () => void) {
  fn();
  passed += 1;
  console.log(`  ✓ ${name}`);
}

// ---- A16：路径穿越拒绝 ----
ok("拒绝 ../ 逃逸", () => {
  assert.throws(() => assertSafeRelativePath("../escape.md"), UnsafePathError);
  assert.throws(() => assertSafeRelativePath("a/../../b.md"), UnsafePathError);
});
ok("拒绝绝对路径与盘符", () => {
  assert.throws(() => assertSafeRelativePath("/etc/passwd"), UnsafePathError);
  assert.throws(() => assertSafeRelativePath("C:/Windows/system32"), UnsafePathError);
  assert.throws(() => assertSafeRelativePath("C:\\Windows\\system32"), UnsafePathError);
});
ok("joinUnder 校验结果仍在 base 下", () => {
  assert.equal(joinUnder("90 Assets/KnowledgeInbox/x", "bundle-000001/normalized.md"),
    "90 Assets/KnowledgeInbox/x/bundle-000001/normalized.md");
  assert.throws(() => joinUnder("90 Assets", "..\\escape"), UnsafePathError);
});
ok("接受正常相对路径并规范化反斜杠", () => {
  assert.equal(assertSafeRelativePath("uploads\\u1\\a.png"), "uploads/u1/a.png");
});

// ---- 文件名清理（docs/02 §12.3）----
ok("清理 Windows 保留字符与尾部点/空格", () => {
  assert.equal(sanitizeTitle('demo<>:"/\\|?*title. '), "demo title");
  assert.equal(sanitizeTitle("CON"), "_CON");
  assert.equal(sanitizeTitle("      "), "未命名");
  assert.equal(sanitizeTitle("很长的标题".repeat(20)).length <= 60, true);
});
ok("Source 笔记路径带日期与 item_id", () => {
  const p = sourceNotePath("10 Sources", "2026-09-07T09:20:00+08:00", "演示标题", "item-demo");
  assert.equal(p, "10 Sources/2026/09/2026-09-07 演示标题--item-demo.md");
  assert.equal(bundleDirName(3), "bundle-000003");
});

// ---- frontmatter 合并：kb_* 更新、未知字段保留 ----
const manifest = {
  schema_version: "1.0",
  item_id: "item-demo",
  source_revision: 2,
  bundle_revision: 3,
  created_at: "2026-09-07T01:23:00Z",
  source: {
    platform: "bilibili",
    title: "演示标题",
    author: null,
    original_url: "https://example.com/video/demo",
    canonical_url: null,
    published_at: null,
    captured_at: "2026-09-07T09:20:00+08:00",
    source_locator: {},
    coverage: "transcript_only",
    content_scope: "unknown",
    original_media_retained: false,
  },
  processing: { state: "ready", recipe_version: "source-light-v1", result_file_id: "result-json", source_revision: 2 },
  files: [
    { file_id: "f1", relative_path: "normalized.md", role: "source_material", mime: "text/markdown", bytes: 10, sha256: "aa" },
    { file_id: "f2", relative_path: "uploads/u1/图.png", role: "original_submission", mime: "image/png", bytes: 10, sha256: "bb" },
  ],
  missing_materials: ["original_video"],
  warnings: [],
  expires_at: "2026-10-07T01:23:00Z",
} as unknown as KbManifest;

ok("新建 Source 笔记包含我的备注/生成区标记/原始材料链接", () => {
  const text = renderSourceNote(manifest, "## 一句话摘要\n\n演示。", "我的备注内容", "90 Assets/KnowledgeInbox/item-demo/bundle-000003", "ready");
  assert.ok(text.includes("## 我的备注\n\n我的备注内容"));
  assert.ok(text.includes(GEN_START) && text.includes(GEN_END));
  assert.ok(text.includes("[[90 Assets/KnowledgeInbox/item-demo/bundle-000003/normalized.md|完整文字稿]]"));
  assert.ok(text.includes("kb_status: \"ready\""));
});
ok("mergeKbFrontmatter 更新 kb_* 且保留未知字段", () => {
  const existing = [
    "---",
    'kb_id: "item-demo"',
    "kb_status: \"ready\"",
    'custom_field: "保留我"',
    "---",
    "",
    "# 正文",
  ].join("\n");
  const merged = mergeKbFrontmatter(existing, manifest, "merge_needed");
  assert.ok(merged.includes('custom_field: "保留我"'));
  assert.ok(merged.includes('kb_status: "merge_needed"'));
  assert.ok(merged.includes("kb_bundle_revision: 3"));
  assert.ok(merged.endsWith("# 正文"));
  // 结束线必须保留，否则下次合并会叠加出双重 frontmatter（真机验收发现的 bug）
  const fmEnd = merged.indexOf("\n---", 4);
  assert.ok(fmEnd !== -1, "frontmatter 必须有结尾 ---");
  const merged2 = mergeKbFrontmatter(merged, manifest, "ready");
  assert.ok(!merged2.includes("\n---\n---"), "重复合并不应叠加 frontmatter");
  assert.ok(merged2.includes('kb_status: "ready"'));
});

// ---- 生成区冲突判定（A11/A12 基础）----
ok("extractGenerated 提取标记间内容", () => {
  const text = `头部\n${GEN_START}\n旧生成内容\n${GEN_END}\n尾部`;
  assert.equal(extractGenerated(text), "旧生成内容");
  assert.equal(extractGenerated("无标记"), null);
});
ok("preview.md 链接改写为本地资产路径并剥用户备注段", () => {
  const md = "## 核心观点\n\n- 观点（[[normalized#s0001|s0001]]）\n\n## 用户备注\n\n> 备注";
  const out = rewritePreviewLinks(md, "90 Assets/KnowledgeInbox/i/bundle-000001");
  assert.ok(out.includes("[[90 Assets/KnowledgeInbox/i/bundle-000001/normalized#^s0001|s0001]]"));
  assert.ok(!out.includes("## 用户备注"));
});

console.log(`\n全部 ${passed} 项通过`);
