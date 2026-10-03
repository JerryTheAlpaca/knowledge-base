/**
 * 纯逻辑冒烟测试（docs/24 §8、§10；docs/23 §7.2、§8.3；docs/08 §2、§3、§7.2）。
 *
 * 覆盖：路径安全（A16）、可读文件名与同名冲突规则、文档索引与按 frontmatter 重建、
 * 分区读写与写保护、Source/Digest 两层模板、ContentDocument v3 解析与渲染、
 * 格式闸门、引用直连原文、文件重命名迁移、多笔记 commit 记录、抑制与回执。
 *
 * 主题整理层（本地模型直连、目标选择与融合、03 Knowledge 写入、旧知识迁移）已随插件精简
 * 一并移除，对应用例不再保留；云端已组装好的 content.json 仍是唯一正文来源。
 *
 * v3 样本取自共享夹具目录 `tests/fixtures/content_v3/`（docs/24 §10，与服务端 pytest 同一批
 * JSON）；夹具没覆盖的组合才内联补数据。
 *
 * 运行（不依赖 Obsidian 界面，package.json 有意不提供 test 脚本）：
 *   node_modules/.bin/esbuild scripts/smoke.ts --bundle --platform=node --format=cjs \
 *     --alias:obsidian=./scripts/obsidian-stub.ts --outfile=.smoke/smoke.cjs && node .smoke/smoke.cjs
 */
import { strict as assert } from "node:assert";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
  assertSafeRelativePath,
  digestKbId,
  digestNotePath,
  documentsIndexPath,
  joinUnder,
  noteDateFromPath,
  noteTitleFromPath,
  resolveAvailableNotePath,
  sanitizeTitle,
  sourceAssetsDir,
  sourceKbId,
  sourceNotePath,
  withConflictSuffix,
  UnsafePathError,
} from "../src/vault/paths";
import {
  CLOUD_DIGEST_END,
  CLOUD_DIGEST_START,
  extractPartition,
  managedTags,
  mergeKbFrontmatter,
  mergeManagedTags,
  partitionHashes,
  readFrontmatterValue,
  renderCloudPending,
  renderDigestNote,
  renderSourceNote,
  replacePartition,
  sha256Hex,
  stripSegmentIds,
} from "../src/vault/template";
import {
  parseContentDocument,
  renderCompletenessNotice,
  renderContentMarkdown,
} from "../src/vault/content";
import { DocumentIndex, documentEntryFromFrontmatter } from "../src/vault/documents";
import { CommitStore, FormatGate, JsonStore, RevisionStore, Suppression } from "../src/vault/records";
import {
  planRenames,
  renderRedirectNote,
  revertRenameMigration,
  rewriteManagedLinks,
  runRenameMigration,
} from "../src/vault/rename";
import { ContentFormatError, SyncEngine, assertContentFormat, sourceAnchor } from "../src/sync/engine";
import type { ContentRefV3, KbManifest, KbSettings } from "../src/types";
import { CONTENT_FORMAT_VERSION, LAYOUT_VERSION } from "../src/types";

/** 与 settings.ts 的默认值保持一致；此处内联以避免冒烟测试依赖 obsidian 模块。 */
const DEFAULT_SETTINGS: KbSettings = {
  serverUrl: "",
  tokenRef: "kb-service-token",
  deviceName: "Obsidian 桌面",
  inboxFolder: "00 Inbox",
  sourcesFolder: "01 Sources",
  digestsFolder: "02 Digests",
  knowledgeFolder: "03 Knowledge",
  assetsFolder: "01 Sources/_assets",
  systemFolder: "99 System",
  autoSync: true,
  deviceId: "",
  userId: "",
  cloudProfileId: "",
  contentFormatVersion: CONTENT_FORMAT_VERSION,
  layoutVersion: LAYOUT_VERSION,
};

let passed = 0;
const asyncChecks: Array<Promise<void>> = [];
function ok(name: string, fn: () => void | Promise<void>) {
  const result = fn();
  if (result instanceof Promise) asyncChecks.push(result);
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
  assert.equal(joinUnder("01 Sources/_assets/x", "normalized.md"),
    "01 Sources/_assets/x/normalized.md");
  assert.throws(() => joinUnder("01 Sources", "..\\escape"), UnsafePathError);
});
ok("接受正常相对路径并规范化反斜杠", () => {
  assert.equal(assertSafeRelativePath("uploads\\u1\\a.png"), "uploads/u1/a.png");
});

// ---- 可读文件名与同名冲突（docs/23 §7.2；docs/24 §8）----
ok("清理 Windows 保留字符与尾部点/空格", () => {
  assert.equal(sanitizeTitle('demo<>:"/\\|?*title. '), "demo title");
  assert.equal(sanitizeTitle("CON"), "_CON");
  assert.equal(sanitizeTitle("      "), "未命名");
  assert.equal(sanitizeTitle("很长的标题".repeat(20)).length <= 60, true);
});
ok("Source/Digest 用可读文件名，不再带 --item_id 后缀", () => {
  const at = "2026-09-21T09:20:00+08:00";
  assert.equal(sourceNotePath("01 Sources", at, "文章标题"),
    "01 Sources/2026/09/2026-09-21 文章标题.md");
  assert.equal(digestNotePath("02 Digests", at, "文章标题"),
    "02 Digests/2026/09/2026-09-21 文章标题.md");
  assert.ok(!sourceNotePath("01 Sources", at, "标题").includes("--"));
  assert.equal(sourceAssetsDir("01 Sources", "item-demo", 2), "01 Sources/_assets/item-demo/source-000002");
  assert.equal(documentsIndexPath("99 System"), "99 System/KnowledgeInbox/index/documents.json");
});
ok("同目录同名按明确规则追加（2）（3），文件名可还原标题与日期", () => {
  assert.equal(withConflictSuffix("01 Sources/2026/09/2026-09-21 标题.md", 2),
    "01 Sources/2026/09/2026-09-21 标题（2）.md");
  assert.equal(noteTitleFromPath("02 Digests/2026/09/2026-09-21 标题（3）.md"), "标题");
  assert.equal(noteDateFromPath("01 Sources/2026/09/2026-09-21 标题--item.md"), "2026-09-21");
});
ok("resolveAvailableNotePath 跳过文件与索引已占用的名字", async () => {
  const takenByFs = new Set(["01 Sources/2026/09/2026-09-21 标题.md"]);
  const takenByIndex = new Set(["01 Sources/2026/09/2026-09-21 标题（2）.md"]);
  const path = await resolveAvailableNotePath(
    "01 Sources/2026/09/2026-09-21 标题.md",
    async (p) => takenByFs.has(p) || takenByIndex.has(p));
  assert.equal(path, "01 Sources/2026/09/2026-09-21 标题（3）.md");
});
ok("kb_id 稳定：Source/Digest 由 item_id 派生", () => {
  assert.equal(sourceKbId("item-demo"), "src-item-demo");
  assert.equal(digestKbId("item-demo"), "dig-item-demo");
});

// ---- 分区读写与哈希（docs/08 §3.2）----
ok("extractPartition/replacePartition 只动标记之间", () => {
  const text = `头部\n${CLOUD_DIGEST_START}\n旧内容\n${CLOUD_DIGEST_END}\n人工区\n`;
  assert.equal(extractPartition(text, CLOUD_DIGEST_START, CLOUD_DIGEST_END), "旧内容");
  const out = replacePartition(text, CLOUD_DIGEST_START, CLOUD_DIGEST_END, "新内容");
  assert.ok(out.includes("新内容"));
  assert.ok(out.includes("头部"));
  assert.ok(out.includes("人工区"));
  assert.equal(extractPartition("无标记", CLOUD_DIGEST_START, CLOUD_DIGEST_END), null);
});
ok("分区标记缺失时追加而不是静默丢弃内容", () => {
  const out = replacePartition("只有人工区", CLOUD_DIGEST_START, CLOUD_DIGEST_END, "新内容");
  assert.ok(out.includes("只有人工区"));
  assert.ok(out.includes("新内容"));
  assert.equal(extractPartition(out, CLOUD_DIGEST_START, CLOUD_DIGEST_END), "新内容");
});
ok("云端区记录哈希；标记缺失为 null", async () => {
  const withCloud = await partitionHashes(`${CLOUD_DIGEST_START}\n云端\n${CLOUD_DIGEST_END}`);
  assert.ok(withCloud.cloud_digest);
  assert.equal((await partitionHashes("无标记")).cloud_digest, null);
});

// ---- frontmatter：只替换 kb_*，保留未知字段 ----
ok("mergeKbFrontmatter 更新 kb_* 且保留未知字段与 aliases", () => {
  const existing = [
    "---",
    'kb_id: "kn-x"',
    "kb_revision: 1",
    'aliases: ["别名"]',
    'custom_field: "保留我"',
    "---",
    "",
    "# 正文",
  ].join("\n");
  const merged = mergeKbFrontmatter(existing, ['kb_revision: 2', 'kb_reviewed_at: "2026-09-09"']);
  assert.ok(merged.includes('custom_field: "保留我"'));
  assert.ok(merged.includes('aliases: ["别名"]'));
  assert.ok(merged.includes("kb_revision: 2"));
  assert.ok(!merged.includes("kb_revision: 1"));
  assert.ok(merged.endsWith("# 正文"));
  const fmEnd = merged.indexOf("\n---", 4);
  assert.ok(fmEnd !== -1, "frontmatter 必须有结尾 ---");
  const merged2 = mergeKbFrontmatter(merged, ["kb_revision: 3"]);
  assert.ok(!merged2.includes("\n---\n---"), "重复合并不应叠加 frontmatter");
});

// ---- Source / Digest 两层模板（docs/08 §3）----
const manifest = {
  schema_version: "1.0",
  item_id: "item-demo",
  source_revision: 2,
  bundle_revision: 3,
  created_at: "2026-09-09T01:23:00Z",
  source: {
    platform: "bilibili",
    title: "Agent实践",
    author: "作者",
    original_url: "https://example.com/video/demo",
    canonical_url: null,
    published_at: null,
    captured_at: "2026-09-09T09:20:00+08:00",
    source_locator: { bvid: "BV1xx", cid: 12345, part: 1 },
    coverage: "transcript_only",
    content_scope: "unknown",
    original_media_retained: false,
  },
  processing: {
    state: "ready", recipe_version: "content-v3-1", result_file_id: "content-json",
    source_revision: 2, format_version: CONTENT_FORMAT_VERSION,
    completeness: "complete", content_file_id: "content-json",
  },
  files: [
    { file_id: "f1", relative_path: "normalized.md", role: "source_material", mime: "text/markdown", bytes: 10, sha256: "aa" },
    { file_id: "f2", relative_path: "original-subtitle.srt", role: "source_material", mime: "text/plain", bytes: 10, sha256: "bb" },
    { file_id: "content-json", relative_path: "content.json", role: "generated", mime: "application/json", bytes: 20, sha256: "cc" },
  ],
  missing_materials: [],
  warnings: [],
  expires_at: "2026-10-09T01:23:00Z",
} as unknown as KbManifest;

ok("Source 笔记：无 AI 生成区，含完整性/定位/原件链接/可读原文", () => {
  const text = renderSourceNote(manifest, {
    assetsBase: "01 Sources/_assets/item-demo/source-000002",
    digestLink: "[[02 Digests/2026/09/2026-09-09 Agent实践|查看提炼]]",
    status: "ready",
    normalizedText: "第一段 ^s0001\n第二段 ^s0002\n",
  });
  assert.ok(text.includes("kb_type: source"));
  assert.ok(text.includes('kb_id: "src-item-demo"'));
  assert.ok(text.includes("完整性：已取得该分 P 字幕，不含视频画面。"));
  assert.ok(text.includes("定位：分 P 1 · cid 12345 · BV1xx"));
  assert.ok(text.includes("[[02 Digests/2026/09/2026-09-09 Agent实践|查看提炼]]"));
  assert.ok(text.includes("[[01 Sources/_assets/item-demo/source-000002/original-subtitle.srt|原字幕]]"));
  assert.ok(text.includes("## 完整文字稿"));
  assert.ok(!text.includes("^s0001"), "Source 正文不应残留块 ID");
  assert.ok(!/## (一句话总结|核心观点)/.test(text), "Source 正文不含 AI 总结");
});
ok("stripSegmentIds 只去块 ID 不去正文", () => {
  assert.equal(stripSegmentIds("段落一 ^s0001\n段落二 ^s0002"), "段落一\n段落二");
});
ok("Digest 笔记：来源链接 + 云端区 + 人工区，不再有本地整理区", () => {
  const text = renderDigestNote(manifest, {
    sourceLink: "[[01 Sources/2026/09/2026-09-09 Agent实践|原始资料]]",
    cloudMd: "先可靠保存，再进行提炼。",
    status: "ready",
    contentLines: [`kb_content_revision: 3`, `kb_format_version: "${CONTENT_FORMAT_VERSION}"`, 'kb_completeness: "complete"'],
  });
  assert.ok(text.includes("kb_type: digest"));
  assert.ok(text.includes("kb_content_revision: 3"));
  assert.ok(text.includes(CLOUD_DIGEST_START) && text.includes(CLOUD_DIGEST_END));
  assert.ok(text.includes("## 我的备注与判断"));
  assert.ok(!text.includes("kb:local-organize"), "本地整理区已退出，不再写分区标记");
  assert.ok(!text.includes("kb_organize"), "整理结论字段已退出");
  assert.ok(!text.includes("status/not_evaluated"));
});
ok("云端无结果时诚实占位，不生成看似有效的空摘要", () => {
  const waiting = { ...manifest, processing: { ...manifest.processing, state: "waiting_key", result_file_id: null } } as KbManifest;
  const text = renderDigestNote(waiting, { sourceLink: null, cloudMd: null, status: "waiting_key" });
  assert.ok(text.includes("云端整理尚未完成（状态：waiting_key）"));
  assert.ok(text.includes("（原始资料尚未入库）"));
  const failed = renderCloudPending({ ...manifest, processing: { ...manifest.processing, state: "failed" } } as KbManifest);
  assert.ok(failed.includes("云端整理失败"));
  assert.ok(!failed.includes("尚未完成"), "失败不能写成尚未完成");
});
ok("标签：只增删系统管理的 type/status，保留用户其他标签", () => {
  const existing = [
    "---",
    'kb_id: "src-x"',
    "tags: [type/source, 我的标签, status/review]",
    "---",
    "",
    "# 正文",
  ].join("\n");
  const merged = mergeManagedTags(existing, managedTags("source", "ready"));
  assert.ok(merged.includes("type/source"));
  assert.ok(merged.includes("status/ready"));
  assert.ok(!merged.includes("status/review"), "旧状态标签应被移除");
  assert.ok(merged.includes("我的标签"), "用户标签必须保留");
  assert.ok(!merged.includes("type/digest"));
  const block = ["---", "tags:", "  - 用户标签", "  - type/source", "---", "", "# x"].join("\n");
  const mergedBlock = mergeManagedTags(block, managedTags("digest", null));
  assert.ok(mergedBlock.includes("type/digest"));
  assert.ok(mergedBlock.includes("用户标签"));
  assert.ok(!mergedBlock.includes("type/source"));
  const noTags = ["---", 'kb_id: "x"', "---", "", "# x"].join("\n");
  assert.ok(mergeManagedTags(noTags, managedTags("digest", null)).includes("tags: [type/digest]"));
});
// ---- 多笔记 commit 记录（docs/08 §7.2、§9）----
class MemFs {
  files = new Map<string, string>();
  async exists(p: string) { return this.files.has(p); }
  async read(p: string) { return this.files.get(p) as string; }
  async write(p: string, d: string) { this.files.set(p, d); }
  async remove(p: string) { this.files.delete(p); }
  async list(dir: string) {
    const prefix = `${dir}/`;
    return [...this.files.keys()].filter((k) => k.startsWith(prefix));
  }
  async listDirs() { return [] as string[]; }
}
function commitOf(itemId: string, rev: number): Record<string, unknown> {
  return {
    item_id: itemId, bundle_revision: rev, manifest_sha256: "x", layout_version: 2,
    note_path: `01 Sources/n/${itemId}.md`, generated_digest: null,
    notes: [
      { role: "source", note_path: `01 Sources/n/${itemId}.md`, managed_digest: "a", state: "written", conflicts: [] },
      { role: "digest", note_path: `02 Digests/n/${itemId}.md`, managed_digest: "b", state: "written", conflicts: [] },
    ],
    local_commit_id: `c-${rev}`, committed_at: "2026-09-09T00:00:00Z",
    ack_sent: true, conflicts: [],
  };
}
ok("多笔记 commit：每篇独立记录路径与哈希", async () => {
  const fs = new MemFs();
  const commits = new CommitStore(fs as never, "99 System/KnowledgeInbox/commits");
  await commits.put(commitOf("item-a", 3) as never);
  const rec = await commits.latestForItem("item-a");
  assert.equal(rec?.layout_version, LAYOUT_VERSION);
  assert.equal(rec?.notes.length, 2);
  assert.equal(CommitStore.noteState(rec!, "digest")?.managed_digest, "b");
  assert.equal(CommitStore.noteState(rec!, "source")?.state, "written");
});
ok("旧版单篇记录升级为 notes 列表（读取兼容）", async () => {
  const fs = new MemFs();
  await fs.write("99 System/KnowledgeInbox/commits/item-b--000001.json", JSON.stringify({
    item_id: "item-b", bundle_revision: 1, manifest_sha256: "x",
    note_path: "10 Sources/2026/item-b.md", generated_digest: "d",
    local_commit_id: "c1", committed_at: "2026-09-08T00:00:00Z", ack_sent: true, conflicts: [],
  }));
  const commits = new CommitStore(fs as never, "99 System/KnowledgeInbox/commits");
  const rec = await commits.latestForItem("item-b");
  assert.equal(rec?.layout_version, 1);
  assert.equal(rec?.notes.length, 1);
  assert.equal(rec?.notes[0].role, "source");
  assert.equal(rec?.notes[0].managed_digest, "d");
});
ok("部分笔记未写入时可被识别（失败不标记已全部完成）", async () => {
  const fs = new MemFs();
  const commits = new CommitStore(fs as never, "99 System/KnowledgeInbox/commits");
  const rec = commitOf("item-c", 1) as Record<string, unknown>;
  (rec.notes as Array<Record<string, unknown>>)[1].state = "merge_needed";
  await commits.put(rec as never);
  const got = await commits.latestForItem("item-c");
  assert.ok(got!.notes.some((n) => n.state !== "written"));
});
ok("removeForItem 只删该条目的 commit 标记", async () => {
  const fs = new MemFs();
  const commits = new CommitStore(fs as never, "99 System/KnowledgeInbox/commits");
  await commits.put(commitOf("item-a", 3) as never);
  await commits.put(commitOf("item-a", 4) as never);
  await commits.put(commitOf("item-b", 1) as never);
  assert.equal(await commits.removeForItem("item-a"), 2);
  assert.equal(await commits.latestForItem("item-a"), null);
  assert.equal((await commits.latestForItem("item-b"))?.bundle_revision, 1);
});
ok("unsuppressAll 返回被清除条目且记录消失", async () => {
  const fs = new MemFs();
  const sup = new Suppression(fs as never, "99 System/KnowledgeInbox/suppression.json");
  await sup.suppress("item-a", "用户删除了 Source 笔记");
  await sup.suppress("item-b", "条目已在服务器删除或过期（GONE）");
  assert.equal((await sup.list()).length, 2);
  const removed = await sup.unsuppressAll();
  assert.deepEqual(removed.sort(), ["item-a", "item-b"]);
  assert.equal(await sup.isSuppressed("item-a"), false);
  assert.deepEqual(await sup.unsuppressAll(), []);
});
ok("RevisionStore 历史快照幂等且可列版本", async () => {
  const fs = new MemFs();
  const rev = new RevisionStore(fs as never, "99 System");
  await rev.save("digests", "dig-a", 1, "v1");
  await rev.save("digests", "dig-a", 1, "v1");
  await rev.save("digests", "dig-a", 2, "v2");
  assert.equal(await rev.read("digests", "dig-a", 1), "v1");
  assert.deepEqual(await rev.revisions("digests", "dig-a"), [1, 2]);
});
ok("JsonStore 损坏时回退默认值", async () => {
  const fs = new MemFs();
  await fs.write("x.json", "{坏 JSON");
  const store = new JsonStore<{ n: number }>(fs as never, "x.json", () => ({ n: 0 }));
  assert.deepEqual(await store.read(), { n: 0 });
});





// ---- ContentDocument v3：解析与渲染（docs/24 §1–§5）----
// 插件不再本地组装文档，这里直接消费云端给好的 content.json。
const SNAPSHOT_REVISIONS = new Set(["item-a@2", "item-b@1"]);
const inlineDoc = {
  format_version: CONTENT_FORMAT_VERSION,
  document_id: "dig-item-a",
  kind: "digest",
  revision: 3,
  created_at: "2026-09-22T00:00:00Z",
  title: "内容接收与提炼",
  summary: "先可靠保存，再进行提炼。",
  sections: [
    {
      heading: "主要判断",
      blocks: [
        { kind: "text", text: "下面区分来源主张与逐字摘录。", refs: [] },
        { kind: "claim", text: "采集与总结应分开处理。", refs: ["e1"] },
        { kind: "quote", text: "采集与总结应该分开处理，避免混在一起。", refs: ["e1"] },
        { kind: "suggestion", text: "可以分别统计两个阶段的失败原因。", refs: [] },
      ],
    },
    {
      heading: "另一来源",
      blocks: [{ kind: "claim", text: "回执也要分开处理。", refs: ["e2"] }],
    },
  ],
  references: {
    e1: {
      item_id: "item-a", source_revision: 2, segment_ids: ["s0001"],
      source_text_hash: "0".repeat(64), locator: { kind: "time", start_ms: 74000, end_ms: 82500 },
    },
    e2: {
      item_id: "item-b", source_revision: 1, segment_ids: ["s0001"],
      source_text_hash: "1".repeat(64), locator: null,
    },
  },
  limitations: ["未取得视频画面上的文字"],
  completeness: { state: "complete", missing_stages: [], gaps: [], dropped_blocks: 0, repair_calls: 0 },
  provenance: {
    recipe_version: "content-v3-1", task: "digest", input_documents: [],
    source_revisions: [{ item_id: "item-a", source_revision: 2 }],
  },
};

ok("解析 content.json：未知 format_version 与缺字段都拒绝，不静默降级", () => {
  const roundTrip = parseContentDocument(inlineDoc);
  assert.equal(roundTrip.errors.length, 0, roundTrip.errors.join("；"));
  assert.equal(roundTrip.document?.sections[0].blocks[1].kind, "claim");
  const tooNew = parseContentDocument({ ...inlineDoc, format_version: "4.0" });
  assert.equal(tooNew.document, null);
  assert.ok(tooNew.errors[0].includes("4.0"));
  assert.equal(parseContentDocument({ kind: "digest" }).document, null);
  const dangling = parseContentDocument({
    ...inlineDoc,
    sections: [{ heading: "x", blocks: [{ kind: "claim", text: "y", refs: ["e404"] }] }],
  });
  assert.equal(dangling.document, null);
  assert.ok(dangling.errors.some((e) => e.includes("e404")));
});

ok("渲染：四种角色各有展示区别，界面不出现裸内部编号", () => {
  const doc = parseContentDocument(inlineDoc).document!;
  const md = renderContentMarkdown(doc, {
    sourceLinkOf: (ref) => sourceAnchor("01 Sources", ref, SNAPSHOT_REVISIONS),
  });
  assert.ok(md.includes("先可靠保存，再进行提炼。"));
  assert.ok(md.includes("## 主要判断"));
  assert.ok(md.includes("- 采集与总结应分开处理。"));
  assert.ok(md.includes("> 采集与总结应该分开处理，避免混在一起。"), "quote 用引用样式");
  assert.ok(md.includes("[!question] AI 建议"), "suggestion 标明 AI 建议/待验证");
  assert.ok(md.includes("下面区分来源主张与逐字摘录。"), "text 作为导语直接成段");
  assert.ok(md.includes("[[01 Sources/_assets/item-a/source-000002/normalized#^s0001|01:14–01:23 查看原文]]"));
  assert.ok(!/\bR\d+\b/.test(md) && !/\be\d+\b/.test(md), "正文不显示 R1/e1");
  assert.ok(!/\bs\d{4}\b(?!\])/.test(md.replace(/\[\[[^\]]*\]\]/g, "")), "链接文字里不出现片段编号");
  assert.ok(md.includes("## 限制与未解决的问题"));
  assert.equal(renderCompletenessNotice(doc.completeness).length, 0, "complete 不加警示");
  const ghost: ContentRefV3 = { item_id: "no-such", source_revision: 9, segment_ids: ["s0001"], source_text_hash: "" };
  assert.equal(sourceAnchor("01 Sources", ghost, SNAPSHOT_REVISIONS), null, "本地没有该版原文时不给链接");
});


// ---- 格式闸门：暂停导入而不是写空白笔记（docs/24 §8；docs/23 §10 发布要求）----
ok("format_version 不认识或缺 content.json → 暂停该项并提示升级", () => {
  const onlySource = {
    ...manifest,
    processing: { state: "original_only", recipe_version: "content-v3-1", result_file_id: null, source_revision: 2 },
    files: manifest.files.filter((f) => f.relative_path !== "content.json"),
  } as unknown as KbManifest;
  assert.equal(assertContentFormat(onlySource), null, "仅原始资料的 Bundle 不要求内容文件");
  const tooNew = { ...manifest, processing: { ...manifest.processing, format_version: "4.0" } } as KbManifest;
  assert.throws(() => assertContentFormat(tooNew), (err: unknown) =>
    err instanceof ContentFormatError && err.code === "content_format_unsupported" && err.formatVersion === "4.0");
  const legacy = {
    ...manifest,
    processing: { state: "ready", recipe_version: "source-light-v1", result_file_id: "analysis", source_revision: 2 },
    files: [{ file_id: "a", relative_path: "analysis.json", role: "generated", mime: "application/json", bytes: 1, sha256: "x" }],
  } as unknown as KbManifest;
  assert.throws(() => assertContentFormat(legacy), (err: unknown) =>
    err instanceof ContentFormatError && err.code === "content_file_missing");
  const noFile = { ...manifest, files: manifest.files.filter((f) => f.relative_path !== "content.json") } as KbManifest;
  assert.throws(() => assertContentFormat(noFile), ContentFormatError);
  const mismatch = { ...manifest, processing: { ...manifest.processing, content_file_id: "other" } } as KbManifest;
  assert.throws(() => assertContentFormat(mismatch), ContentFormatError);
  assert.equal(assertContentFormat(manifest)?.relative_path, "content.json");
});


// ---- 共享夹具（docs/24 §10）：与服务端 pytest 读同一批 JSON，跨实现互校 ----
// 从打包产物目录回溯到仓库根：`.smoke/smoke.cjs` → `../../../tests/fixtures/content_v3`。
const FIXTURE_DIR = join(__dirname, "..", "..", "..", "tests", "fixtures", "content_v3");
function fixture(name: string): any {
  return JSON.parse(readFileSync(join(FIXTURE_DIR, name), "utf-8"));
}

ok("共享夹具：content.json 解析进 v3 类型，引用身份在引用表里都能对上", () => {
  const pairs = [
    ["document_short_article.json", "ref_table_short_article.json"],
    ["document_conversation.json", "ref_table_conversation.json"],
  ] as const;
  for (const [docFile, tableFile] of pairs) {
    const parsed = parseContentDocument(fixture(docFile));
    assert.equal(parsed.errors.length, 0, `${docFile}：${parsed.errors.join("；")}`);
    const doc = parsed.document!;
    assert.equal(doc.format_version, CONTENT_FORMAT_VERSION);
    assert.ok(doc.sections.length > 0, `${docFile}：章节为空`);
    const byIdentity = new Map<string, any>(Object.values(fixture(tableFile) as Record<string, any>)
      .map((e: any) => [`${e.item_id}@${e.source_revision}|${e.segment_ids.join(",")}`, e]));
    for (const [key, ref] of Object.entries(doc.references)) {
      const entry = byIdentity.get(`${ref.item_id}@${ref.source_revision}|${ref.segment_ids.join(",")}`);
      assert.ok(entry, `${docFile} 的 ${key} 在夹具引用表里找不到对应原文`);
      assert.match(ref.source_text_hash, /^[0-9a-f]{64}$/, `${docFile} ${key}：哈希必须是 sha256`);
    }
    const roundTrip = parseContentDocument(JSON.parse(JSON.stringify(doc)));
    assert.deepEqual(roundTrip.errors, [], `${docFile} 往返解析应稳定`);
  }
});

ok("共享夹具：旧 analysis.json 与更高格式版本都不作正文，渲染直连固定原文", () => {
  const legacy = parseContentDocument(fixture("legacy_analysis_v2.json"));
  assert.equal(legacy.document, null, "旧 analysis.json 不是 v3 内容文档");
  assert.ok(legacy.errors[0].includes(CONTENT_FORMAT_VERSION), legacy.errors.join("；"));
  const v1 = parseContentDocument(fixture("legacy_analysis_v1.json"));
  assert.equal(v1.document, null);
  const tooNew = parseContentDocument({ ...fixture("document_short_article.json"), format_version: "4.0" });
  assert.equal(tooNew.document, null, "更高的格式版本必须拒绝，不静默降级");
  assert.ok(tooNew.errors[0].includes("4.0"));

  const doc = parseContentDocument(fixture("document_short_article.json")).document!;
  const available = new Set(Object.values(doc.references).map((r) => `${r.item_id}@${r.source_revision}`));
  const md = renderContentMarkdown(doc, { sourceLinkOf: (ref) => sourceAnchor("01 Sources", ref, available) });
  const item = doc.references.e1.item_id;
  assert.ok(md.includes(`[[01 Sources/_assets/${item}/source-000001/normalized#^s0001|第 1 段 查看原文]]`),
    `依据要直连该版本原文锚点、标签只说自然位置，实际：${md}`);
  assert.ok(!md.includes("第 p0001 段"), "段落编号 p0001 不得出现在给用户看的文字里");
  assert.ok(!/\be\d+\b/.test(md) && !/\bR\d+\b/.test(md), "界面不显示裸内部编号");
  assert.ok(md.includes("样本只覆盖图文来源"), "限制照实展示");
  const conv = parseContentDocument(fixture("document_conversation.json")).document!;
  const convAvail = new Set(Object.values(conv.references).map((r) => `${r.item_id}@${r.source_revision}`));
  const convMd = renderContentMarkdown(conv, { sourceLinkOf: (ref) => sourceAnchor("01 Sources", ref, convAvail) });
  assert.ok(conv.sections.length >= 2, "对话按自然章节呈现");
  assert.ok(!/\bc\d{4}\b/.test(convMd) && !/\bR\d+\b/.test(convMd), "对话产物也不得出现观点编号");
});



// ---- 文件重命名迁移（docs/23 §8.3）----
ok("改名映射：可读目标名、同名冲突、无法确定的保持原样", () => {
  const { plan, blocked } = planRenames([
    { kb_id: "src-a", path: "01 Sources/2026/09/2026-09-21 甲--item-a.md", kind: "source", title: "甲", captured_at: "2026-09-21T00:00:00Z" },
    { kb_id: "src-b", path: "01 Sources/2026/09/2026-09-21 甲--item-b.md", kind: "source", title: "甲", captured_at: "2026-09-21T00:00:00Z" },
    { kb_id: "dig-a", path: "02 Digests/2026/09/2026-09-21 甲--item-a.md", kind: "digest", title: "甲", captured_at: null },
    { kb_id: "src-c", path: "01 Sources/2026/09/无日期--item-c.md", kind: "source", title: "丙", captured_at: null },
  ], { sources: "01 Sources", digests: "02 Digests" });
  assert.deepEqual(plan.map((p) => [p.kb_id, p.new_path]), [
    ["src-a", "01 Sources/2026/09/2026-09-21 甲.md"],
    ["src-b", "01 Sources/2026/09/2026-09-21 甲（2）.md"],
    ["dig-a", "02 Digests/2026/09/2026-09-21 甲.md"],
  ], "映射按路径确定排序；同名让路只在同一目录内发生");
  assert.equal(blocked[0].kb_id, "src-c");
  assert.ok(blocked[0].reason.includes("采集日期"));
});
ok("受管理链接按映射更新；兼容入口不带 kb_id", () => {
  const map = new Map([["01 Sources/2026/09/2026-09-21 甲--item-a.md", "01 Sources/2026/09/2026-09-21 甲.md"]]);
  const text = [
    "提炼：[[01 Sources/2026/09/2026-09-21 甲--item-a.md|原始资料]]",
    "机器引用：[[01 Sources/2026/09/2026-09-21 甲--item-a.md#^s0001|查看原文]]",
    "用户自己写的：[[甲--item-a|我的链接]]",
  ].join("\n");
  const out = rewriteManagedLinks(text, map);
  assert.ok(out.includes("[[01 Sources/2026/09/2026-09-21 甲.md|原始资料]]"));
  assert.ok(out.includes("[[01 Sources/2026/09/2026-09-21 甲.md#^s0001"));
  assert.ok(out.includes("[[甲--item-a|我的链接]]"), "只改插件生成的完整路径链接");
  const redirect = renderRedirectNote("01 Sources/2026/09/2026-09-21 甲.md");
  assert.ok(redirect.includes("kb_type: redirect") && !redirect.includes("kb_id:"));
});


// ---- 文档索引：ID→路径，丢失时按 frontmatter 重建（docs/24 §8）----
class BundleFs extends MemFs {
  binary = new Map<string, ArrayBuffer>();
  async exists(p: string) { return this.files.has(p) || this.binary.has(p); }
  async read(p: string) {
    if (this.files.has(p)) return this.files.get(p) as string;
    const data = this.binary.get(p);
    if (data) return new TextDecoder().decode(data);
    throw new Error(`缺文件：${p}`);
  }
  async writeBinary(p: string, data: ArrayBuffer) { this.binary.set(p, data); this.files.delete(p); }
  async readBinary(p: string) {
    const data = this.binary.get(p);
    if (!data) throw new Error(`缺文件：${p}`);
    return data;
  }
  async remove(p: string) { this.files.delete(p); this.binary.delete(p); }
  async rename(from: string, to: string) {
    if (this.binary.has(from)) { this.binary.set(to, this.binary.get(from) as ArrayBuffer); this.binary.delete(from); }
    else if (this.files.has(from)) { this.files.set(to, this.files.get(from) as string); this.files.delete(from); }
    else throw new Error(`改名源不存在：${from}`);
  }
  async list(dir: string) {
    const prefix = `${dir}/`;
    return [...this.files.keys(), ...this.binary.keys()].filter((k) => k.startsWith(prefix));
  }
}

function plainNote(kbId: string, kind: string, title: string, itemId?: string): string {
  return [
    "---",
    `kb_id: "${kbId}"`,
    `kb_type: ${kind}`,
    itemId ? `kb_item_id: "${itemId}"` : "",
    "---",
    "",
    `# ${title}`,
    "",
  ].filter(Boolean).join("\n");
}

const INDEX_ROOTS = [
  { folder: "01 Sources", kind: "source" as const, skipDirs: ["_assets"] },
  { folder: "02 Digests", kind: "digest" as const },
];

ok("文档索引登记与重建：改名后仍按 kb_id 识别，重复 kb_id 报冲突并保留两份", async () => {
  const fs = new BundleFs();
  await fs.write("01 Sources/2026/09/2026-09-21 甲.md", plainNote("src-item-a", "source", "甲", "item-a"));
  await fs.write("02 Digests/2026/09/2026-09-21 甲.md", plainNote("dig-item-a", "digest", "甲", "item-a"));
  const docs = new DocumentIndex(fs as never, documentsIndexPath("99 System"));
  await docs.ensure(INDEX_ROOTS);
  assert.equal(await docs.pathOf("src-item-a"), "01 Sources/2026/09/2026-09-21 甲.md");
  assert.ok(await fs.exists(documentsIndexPath("99 System")));
  // 用户移动文件后索引丢失：扫 frontmatter 重建，身份不变
  await fs.rename("01 Sources/2026/09/2026-09-21 甲.md", "01 Sources/我搬走了.md");
  await fs.remove(documentsIndexPath("99 System"));
  const rebuilt = new DocumentIndex(fs as never, documentsIndexPath("99 System"));
  await rebuilt.ensure(INDEX_ROOTS);
  assert.equal(await rebuilt.pathOf("src-item-a"), "01 Sources/我搬走了.md");
  // 重复 kb_id：报冲突、两份都留、不任选覆盖
  await fs.write("01 Sources/副本.md", plainNote("src-item-a", "source", "甲", "item-a"));
  await fs.remove(documentsIndexPath("99 System"));
  const withConflict = new DocumentIndex(fs as never, documentsIndexPath("99 System"));
  const doc = await withConflict.rebuild(INDEX_ROOTS);
  assert.equal(doc.conflicts.length, 1);
  assert.equal(doc.conflicts[0].paths.length, 2);
  assert.ok(await fs.exists("01 Sources/我搬走了.md"));
  assert.ok(await fs.exists("01 Sources/副本.md"));
  const registered = await withConflict.register({
    kb_id: "src-item-a", path: "01 Sources/第三份.md", kind: "source", item_id: "item-a", title: "甲", updated_at: "",
  });
  assert.equal(registered.conflict, true);
  // 扫描重建时同名冲突按路径排序保留其一（两份都列进 conflicts）；
  // 这里要守的是：register 的第三份不得改写原登记。
  const kept = await withConflict.pathOf("src-item-a");
  assert.ok(["01 Sources/我搬走了.md", "01 Sources/副本.md"].includes(kept ?? ""), `冲突时保留原登记，实际：${kept}`);
  assert.notEqual(kept, "01 Sources/第三份.md");
  assert.equal(await withConflict.isTakenByOther("01 Sources/副本.md", "dig-item-a"), true);
  assert.equal(await withConflict.isTakenByOther("01 Sources/副本.md", "src-item-a"), false);
});
ok("普通笔记与 redirect 兼容入口都不进入文档索引", () => {
  assert.equal(documentEntryFromFrontmatter("# 随手记\n\n内容", "随手记.md"), null);
  assert.equal(documentEntryFromFrontmatter("---\nkb_type: redirect\n---\n\n> 见 [[新路径]]", "旧路径.md"), null);
  assert.equal(documentEntryFromFrontmatter(plainNote("src-a", "source", "甲", "a"), "01 Sources/甲.md")?.kb_id, "src-a");
});

// ---- 同步引擎端到端：格式闸门、v3 消费与可读文件名 ----
const SEGMENT_TEXT = "采集与总结应该分开处理，避免混在一起。";
const BUNDLE_FILES = (itemId: string): Record<string, string> => ({
  "content.json": JSON.stringify(digestDoc(itemId)),
  "normalized.md": `${SEGMENT_TEXT} ^s0001\n`,
  "segments.json": JSON.stringify({ segments: [{ segment_id: "s0001", text: SEGMENT_TEXT }] }),
  "readable.md": `${SEGMENT_TEXT}\n`,
});

function digestDoc(itemId: string): Record<string, unknown> {
  return {
    format_version: CONTENT_FORMAT_VERSION,
    document_id: `dig-${itemId}`,
    kind: "digest",
    revision: 3,
    created_at: "2026-09-22T00:00:00Z",
    title: "文章标题",
    summary: "先可靠保存，再进行提炼。",
    sections: [{
      heading: "主要判断",
      blocks: [
        { kind: "text", text: "下面区分来源主张与逐字摘录。", refs: [] },
        { kind: "claim", text: "采集与总结应分开处理。", refs: ["e1"] },
        { kind: "quote", text: SEGMENT_TEXT, refs: ["e1"] },
        { kind: "suggestion", text: "可以分别统计两个阶段的失败原因。", refs: [] },
      ],
    }],
    references: {
      e1: {
        item_id: itemId, source_revision: 2, segment_ids: ["s0001"],
        source_text_hash: "由服务端程序计算", locator: { kind: "time", start_ms: 74000, end_ms: 82500 },
      },
    },
    limitations: [],
    completeness: { state: "complete", missing_stages: [], gaps: [], dropped_blocks: 0, repair_calls: 0 },
    provenance: {
      recipe_version: "content-v3-1", task: "digest", input_documents: [],
      source_revisions: [{ item_id: itemId, source_revision: 2 }],
    },
  };
}

function bundleManifest(itemId: string): KbManifest {
  const texts = BUNDLE_FILES(itemId);
  return {
    schema_version: "1.0",
    item_id: itemId,
    source_revision: 2,
    bundle_revision: 3,
    created_at: "2026-09-22T00:00:00Z",
    source: {
      platform: "web", title: "文章标题", author: null, original_url: "https://example.com/a",
      canonical_url: null, published_at: null, captured_at: "2026-09-21T09:20:00+08:00",
      source_locator: {}, coverage: "full_text", content_scope: "unknown", original_media_retained: false,
    },
    processing: {
      state: "ready", recipe_version: "content-v3-1", result_file_id: "f1", source_revision: 2,
      format_version: CONTENT_FORMAT_VERSION, completeness: "complete", content_file_id: "f1",
    },
    files: Object.keys(texts).map((path, i) => ({
      file_id: `f${i + 1}`,
      relative_path: path,
      role: path === "content.json" ? "generated" : "source_material",
      mime: path.endsWith(".json") ? "application/json" : "text/markdown",
      bytes: new TextEncoder().encode(texts[path]).length,
      sha256: "",
    })),
    missing_materials: [],
    warnings: [],
    expires_at: "2026-10-22T00:00:00Z",
  } as unknown as KbManifest;
}

async function engineHarness(
  m: KbManifest,
  ctx: { fs: BundleFs; receipts: string[] },
  overrides: Record<string, string> = {},
) {
  const texts = { ...BUNDLE_FILES(m.item_id), ...overrides };
  for (const f of m.files) {
    if (f.relative_path in overrides) f.bytes = new TextEncoder().encode(texts[f.relative_path]).length;
    f.sha256 = await sha256Hex(texts[f.relative_path] ?? "");
  }
  const client = {
    listEvents: async () => ({
      events: [{ seq: 1, event_type: "bundle_published", item_id: m.item_id, bundle_revision: m.bundle_revision, payload: {}, created_at: "2026-09-22T00:00:00Z" }],
      next_cursor: 1, has_more: false,
    }),
    getManifest: async () => ({ manifest: m, sha256: "manifest-sha" }),
    getFile: async (_itemId: string, _rev: number, fileId: string) => {
      const entry = m.files.find((f) => f.file_id === fileId);
      return new TextEncoder().encode(texts[entry?.relative_path ?? ""] ?? "").buffer as ArrayBuffer;
    },
    sendReceipt: async (itemId: string) => { ctx.receipts.push(itemId); return {} as never; },
  };
  let state = {
    cursor: 0,
    pending: { [m.item_id]: { item_id: m.item_id, revision: m.bundle_revision, seq: 1, enqueued_at: "2026-09-22T00:00:00Z", attempts: 0, next_try_at: 0 } },
    lastRunAt: null,
  };
  let status = null as null | { pausedForUpgrade?: number; pendingCount?: number; lastError?: string | null };
  const engine = new SyncEngine({
    fs: ctx.fs as never,
    getClient: () => client as never,
    settings: () => DEFAULT_SETTINGS,
    loadState: async () => state,
    saveState: async (s) => { state = s; },
    onStatus: (s) => { status = s; },
    log: () => undefined,
  });
  await engine.runOnce("test");
  return { state, status };
}

ok("闸门：未知 format_version 不落盘、不发回执，只暂停该项并提示升级", async () => {
  const fs = new BundleFs();
  const receipts: string[] = [];
  const m = bundleManifest("item-x");
  m.processing = { ...m.processing, format_version: "4.0" } as KbManifest["processing"];
  const { state, status } = await engineHarness(m, { fs, receipts });
  assert.equal(receipts.length, 0, "不得发送成功回执");
  assert.equal(fs.binary.size, 0, "闸门在下载前生效：不落任何 Bundle 文件");
  assert.equal([...fs.files.keys()].filter((k) => k.endsWith(".md") && !k.startsWith("00 Inbox")).length, 0, "不得落盘空白笔记");
  assert.ok(state.pending["item-x"], "保留待办，插件升级后可自动续做");
  const gate = await new FormatGate(fs as never, "99 System/KnowledgeInbox/format_gate.json").list();
  assert.equal(gate.length, 1);
  assert.equal(gate[0].code, "content_format_unsupported");
  assert.ok(gate[0].reason.includes("升级"));
  assert.equal(status?.pausedForUpgrade, 1);
  assert.ok(String(status?.lastError).includes("4.0"));
});

ok("闸门：声明 ready 却缺 content.json 也暂停；旧 analysis.json 不再消费", async () => {
  const fs = new BundleFs();
  const receipts: string[] = [];
  const m = bundleManifest("item-x");
  m.files = m.files.filter((f) => f.relative_path !== "content.json");
  await engineHarness(m, { fs, receipts });
  assert.equal(receipts.length, 0);
  const gate = await new FormatGate(fs as never, "99 System/KnowledgeInbox/format_gate.json").list();
  assert.equal(gate[0].code, "content_file_missing");
  const legacy = bundleManifest("item-y");
  legacy.files = [{ file_id: "a1", relative_path: "analysis.json", role: "generated", mime: "application/json", bytes: 2, sha256: "x" }];
  legacy.processing = { state: "ready", recipe_version: "source-light-v1", result_file_id: "a1", source_revision: 2 } as KbManifest["processing"];
  assert.throws(() => assertContentFormat(legacy), (err: unknown) =>
    err instanceof ContentFormatError && err.code === "content_file_missing");
  assert.equal(assertContentFormat({ ...legacy, processing: { ...legacy.processing, state: "original_only", result_file_id: null }, files: [] } as KbManifest), null);
  assert.equal(assertContentFormat(bundleManifest("item-z"))?.relative_path, "content.json");
});

ok("端到端（共享夹具）：content.json 版本不认识就暂停；认识就按夹具渲染并回执", async () => {
  const doc = fixture("document_short_article.json");
  const receipts: string[] = [];
  const fs = new BundleFs();
  await engineHarness(bundleManifest("item-fx"), { fs, receipts },
    { "content.json": JSON.stringify({ ...doc, format_version: "4.0" }) });
  assert.deepEqual(receipts, [], "内容版本不认识时不发成功回执");
  // 清单版本合法、content.json 自身版本不认识：材料仍会下到 Bundle 目录，但不产生任何笔记
  assert.equal([...fs.files.keys()].filter((k) => k.endsWith(".md") && !k.startsWith("00 Inbox")).length, 0, "不落盘空白笔记");
  assert.equal([...fs.files.keys()].filter((k) => k.startsWith("01 Sources/") || k.startsWith("02 Digests/")).length, 0,
    "三层目录里不留半成品");
  const gate = await new FormatGate(fs as never, "99 System/KnowledgeInbox/format_gate.json").list();
  assert.equal(gate[0].code, "content_file_unparsable");
  assert.ok(gate[0].reason.includes("4.0"), gate[0].reason);
  assert.equal((await new CommitStore(fs as never, "99 System/KnowledgeInbox/commits").all()).length, 0, "暂停项不记提交");

  await engineHarness(bundleManifest("item-fx"), { fs, receipts },
    { "content.json": JSON.stringify(doc) });
  assert.deepEqual(receipts, ["item-fx"], "同一条目升级后自动续做并回执");
  const note = await fs.read("02 Digests/2026/09/2026-09-21 文章标题.md");
  const cloud = extractPartition(note, CLOUD_DIGEST_START, CLOUD_DIGEST_END)!;
  assert.ok(cloud.includes("先可靠保存，再提炼；晋升判断留在本地。"), "夹具摘要进入云端区");
  assert.ok(cloud.includes("样本只覆盖图文来源"), "夹具限制照实展示");
  assert.ok(!cloud.includes("_assets"), "夹具来源没有本地快照时不硬造原文链接");
  assert.ok(!/\be\d+\b/.test(cloud) && !/\bR\d+\b/.test(cloud), "界面不显示内部编号");
});

ok("v3 消费端到端：可读文件名、索引登记、云端区由 content.json 渲染、回执发送", async () => {
  const fs = new BundleFs();
  const receipts: string[] = [];
  await engineHarness(bundleManifest("item-x"), { fs, receipts });
  const sourcePath = "01 Sources/2026/09/2026-09-21 文章标题.md";
  const digestPath = "02 Digests/2026/09/2026-09-21 文章标题.md";
  assert.ok(await fs.exists(sourcePath), `应写入 ${sourcePath}`);
  assert.ok(await fs.exists(digestPath));
  assert.equal([...fs.files.keys()].filter((k) => k.includes("--item-x") && k.startsWith("0") && !k.startsWith("99")).length, 0,
    "新笔记文件名不带 item_id 后缀");
  const digest = await fs.read(digestPath);
  const cloud = extractPartition(digest, CLOUD_DIGEST_START, CLOUD_DIGEST_END) ?? "";
  assert.ok(cloud.includes("[[01 Sources/_assets/item-x/source-000002/normalized#^s0001|01:14–01:23 查看原文]]"),
    `依据应直连固定原文并显示自然位置，实际：${cloud}`);
  assert.ok(cloud.includes("[!question] AI 建议"));
  assert.ok(!/\be1\b/.test(cloud) && !/\bR1\b/.test(cloud), "界面不显示内部编号");
  assert.equal(readFrontmatterValue(digest, "kb_content_revision"), "3");
  assert.equal(readFrontmatterValue(digest, "kb_format_version"), CONTENT_FORMAT_VERSION);
  assert.deepEqual(receipts, ["item-x"]);
  const docs = new DocumentIndex(fs as never, documentsIndexPath("99 System"));
  await docs.ensure(INDEX_ROOTS);
  assert.equal(await docs.pathOf(digestKbId("item-x")), digestPath);
  const commit = await new CommitStore(fs as never, "99 System/KnowledgeInbox/commits").latestForItem("item-x");
  assert.equal(commit?.ack_sent, true);
  assert.equal(commit?.layout_version, LAYOUT_VERSION);
  assert.ok(await fs.exists("99 System/KnowledgeInbox/commits/item-x--000003.json"), "提交标记等内部文件仍可含 item_id");
});

ok("同标题新条目按（2）让路；同版本重复事件不重复建笔记、不产生第二份", async () => {
  const fs = new BundleFs();
  const receipts: string[] = [];
  await engineHarness(bundleManifest("item-x"), { fs, receipts });
  const before = await fs.read("02 Digests/2026/09/2026-09-21 文章标题.md");
  await engineHarness(bundleManifest("item-x"), { fs, receipts });
  assert.equal(await fs.read("02 Digests/2026/09/2026-09-21 文章标题.md"), before, "同版本重复事件不重写笔记");
  await engineHarness(bundleManifest("item-y"), { fs, receipts });
  assert.ok(await fs.exists("01 Sources/2026/09/2026-09-21 文章标题（2）.md"), "第二个同名条目追加（2）");
  assert.deepEqual(receipts, ["item-x", "item-y"], "每个条目各自回执一次；同版本重复事件不重发回执");
  const commits = await new CommitStore(fs as never, "99 System/KnowledgeInbox/commits").all();
  assert.equal(commits.filter((c) => c.item_id === "item-x").length, 1, "同版本重复事件不产生第二份提交记录");
  const docs = new DocumentIndex(fs as never, documentsIndexPath("99 System"));
  await docs.rebuild(INDEX_ROOTS);
  assert.equal(await docs.pathOf(sourceKbId("item-y")), "01 Sources/2026/09/2026-09-21 文章标题（2）.md");
  assert.equal((await docs.conflicts()).length, 0, "不同 kb_id 各占一路径，不是身份冲突");
  const paths = (await docs.entries()).map((e) => e.path);
  assert.equal(new Set(paths).size, paths.length, "没有两份登记指向同一路径");
});

ok("写保护：用户改过云端区时不覆盖，冲突文件仍产出", async () => {
  const fs = new BundleFs();
  const receipts: string[] = [];
  await engineHarness(bundleManifest("item-x"), { fs, receipts });
  const digestPath = "02 Digests/2026/09/2026-09-21 文章标题.md";
  const digest = await fs.read(digestPath);
  // 界面红线：链接锚点可以带稳定原文编号，但给用户看的文字里不得出现 R/e/s0001/p0001
  const region = digest.slice(digest.indexOf("kb:cloud-digest:start"), digest.indexOf("kb:cloud-digest:end"));
  const visible = region.replace(/\[\[[^\]]*\]\]/g, "");
  assert.ok(!/R\d+|e\d{1,4}|s\d{4}|p\d{4}/.test(visible),
    "云端区展示文字泄漏了内部编号：" + visible.slice(0, 120));
  await fs.write(digestPath, digest.replace("先可靠保存，再进行提炼。", "我自己改写过这一段。"));
  // 更高版本的事件：新结果进冲突文件，人工改动保留
  const next = bundleManifest("item-x");
  next.bundle_revision = 4;
  await engineHarness(next, { fs, receipts });
  const after = await fs.read(digestPath);
  assert.ok(after.includes("我自己改写过这一段。"), "用户编辑不被覆盖");
  const conflicts = [...fs.files.keys()].filter((k) => k.includes("/conflicts/"));
  assert.equal(conflicts.length, 1);
  assert.ok(conflicts[0].includes("item-x--000004--digest.md"), "冲突文件名可用 item_id（内部文件）");
});


// ---- 本地迁移端到端（docs/23 §8.2、§8.3）----

ok("文件重命名：改名 + 更新受管理链接 + 索引 + 兼容入口与恢复记录", async () => {
  const fs = new BundleFs();
  const oldSource = "01 Sources/2026/09/2026-09-21 甲--item-a.md";
  const newSource = "01 Sources/2026/09/2026-09-21 甲.md";
  await fs.write(oldSource, [
    "---", 'kb_id: "src-item-a"', "kb_type: source", 'kb_item_id: "item-a"',
    'kb_captured_at: "2026-09-21T09:20:00+08:00"', "---", "", "# 甲", "",
    "提炼：[[02 Digests/2026/09/2026-09-21 甲--item-a.md|查看提炼]]", "",
  ].join("\n"));
  await fs.write("02 Digests/2026/09/2026-09-21 甲--item-a.md", [
    "---", 'kb_id: "dig-item-a"', "kb_type: digest", 'kb_item_id: "item-a"', "---", "", "# 甲：提炼", "",
    "来源：[[01 Sources/2026/09/2026-09-21 甲--item-a.md|原始资料]]", "",
  ].join("\n"));
  const result = await runRenameMigration(fs as never, DEFAULT_SETTINGS, {
    rename: async (from, to) => { await fs.rename(from, to); },
  });
  assert.equal(result.completed, true, JSON.stringify(result.failed));
  assert.equal(result.renamed.length, 2);
  // 旧路径不删除，只留带跳转说明的兼容入口（docs/23 §8.3 第 4、5 条），下方单独校验其内容
  assert.ok(await fs.exists(newSource), "正文已在新路径");
  assert.ok((await fs.read(newSource)).includes("[[02 Digests/2026/09/2026-09-21 甲.md|查看提炼]]"), "受管理链接已更新");
  const redirect = await fs.read(oldSource);
  assert.ok(redirect.includes("kb_type: redirect") && redirect.includes("内容在 [[01 Sources/2026/09/2026-09-21 甲.md]]"));
  assert.ok(!redirect.includes('kb_id: "src-item-a"'), "兼容入口不是同一文档的第二份可写副本");
  const docs = new DocumentIndex(fs as never, documentsIndexPath("99 System"));
  assert.equal(await docs.pathOf("src-item-a"), newSource);
  const record = JSON.parse(await fs.read(result.record_path));
  assert.equal(record.reverse.length, 2);
  const restored = await runRenameMigration(fs as never, DEFAULT_SETTINGS, { rename: async () => { throw new Error("Obsidian 改名失败"); } });
  assert.ok(restored);
  assert.equal(record.renamed[0].old_path, oldSource);
});

ok("重命名单项失败：不标完成、不产生新副本，失败原因可查", async () => {
  const fs = new BundleFs();
  const oldSource = "01 Sources/2026/09/2026-09-21 甲--item-a.md";
  await fs.write(oldSource, [
    "---", 'kb_id: "src-item-a"', "kb_type: source", 'kb_captured_at: "2026-09-21T09:20:00+08:00"', "---", "", "# 甲", "",
  ].join("\n"));
  await fs.write("01 Sources/2026/09/2026-09-21 甲.md", "# 别人已经占了这个名字\n");
  const result = await runRenameMigration(fs as never, DEFAULT_SETTINGS, {
    rename: async (from, to) => { await fs.rename(from, to); },
  });
  assert.equal(result.completed, false);
  assert.equal(result.renamed.length, 0);
  assert.equal(result.failed[0].reason, "目标路径已存在");
  assert.ok(await fs.exists(oldSource), "失败项保持原路径与原内容");
  const record = JSON.parse(await fs.read(result.record_path));
  assert.equal(record.completed, false);
  assert.ok(record.failed[0].kb_id === "src-item-a");
});

ok("恢复改名：按恢复记录反向退回原位", async () => {
  const fs = new BundleFs();
  const oldSource = "01 Sources/2026/09/2026-09-21 甲--item-a.md";
  await fs.write(oldSource, [
    "---", 'kb_id: "src-item-a"', "kb_type: source", 'kb_item_id: "item-a"',
    'kb_captured_at: "2026-09-21T09:20:00+08:00"', "---", "", "# 甲", "",
  ].join("\n"));
  const result = await runRenameMigration(fs as never, DEFAULT_SETTINGS, { rename: async (f, t) => { await fs.rename(f, t); } });
  const record = JSON.parse(await fs.read(result.record_path));
  const reverted = await revertRenameMigration(fs as never, DEFAULT_SETTINGS, record, async (f, t) => { await fs.rename(f, t); });
  assert.equal(reverted.restored, 1, JSON.stringify(reverted.failed));
  assert.ok(await fs.exists(oldSource));
  assert.ok(!(await fs.exists("01 Sources/2026/09/2026-09-21 甲.md")));
});

void (async () => {
  await Promise.all(asyncChecks);
  console.log(`
全部 ${passed} 项通过`);
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
