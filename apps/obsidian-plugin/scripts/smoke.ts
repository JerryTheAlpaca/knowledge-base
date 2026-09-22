/**
 * 纯逻辑冒烟测试（docs/24 §8、§10；docs/23 §6、§7.2、§8.2、§8.3；docs/08 §2、§3、§5）。
 *
 * 覆盖：路径安全（A16）、可读文件名与同名冲突规则、文档索引与按 frontmatter 重建、
 * 分区读写与写保护、Source/Digest/Knowledge 模板、ContentDocument v3 解析与格式闸门、
 * 程序组装与逐字摘录校验、引用直连原文、旧 evidence_map 展开（两个 s0001 不合并）、
 * 主题候选流程（差异含删除、no_op 不写、部分采纳重排引用、基线保护、回滚含引用表）、
 * 旧知识迁移与文件重命名、本地模型解析与错误分类、多笔记 commit 记录。
 *
 * v3 样本取自共享夹具目录 `tests/fixtures/content_v3/`（docs/24 §10，与服务端 pytest 同一批
 * JSON）；只有夹具没覆盖的组合（如本地融合引用表构造）才内联补数据。
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
  knowledgeIdFromTitle,
  knowledgeNotePath,
  knowledgeRefsFileName,
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
  KNOWLEDGE_END,
  KNOWLEDGE_HISTORY_END,
  KNOWLEDGE_HISTORY_START,
  KNOWLEDGE_START,
  LOCAL_ORGANIZE_END,
  LOCAL_ORGANIZE_START,
  extractKnowledgeScope,
  extractPartition,
  managedTags,
  mergeKbFrontmatter,
  mergeManagedTags,
  partitionHashes,
  readFrontmatterValue,
  renderCloudPending,
  renderDigestNote,
  renderKnowledgeNote,
  renderLocalOrganize,
  renderProposalIndex,
  renderSourceNote,
  replacePartition,
  sha256Hex,
  stripSegmentIds,
} from "../src/vault/template";
import {
  LIMITS,
  assembleContentDocument,
  buildRefTableFromSegments,
  independentSourceCount,
  normalizeForQuote,
  parseContentDocument,
  quoteVerificationErrors,
  referenceHashErrors,
  renderCompletenessNotice,
  renderContentMarkdown,
  renderReferenceTable,
  refTextHashOf,
  selectBlocksForAdoption,
} from "../src/vault/content";
import { DocumentIndex, documentEntryFromFrontmatter, listMarkdown } from "../src/vault/documents";
import { CommitStore, FormatGate, JsonStore, RevisionStore, Suppression } from "../src/vault/records";
import { KnowledgeIndexStore, matchKnowledge, parseKnowledgeEntry, tokenize } from "../src/knowledge/index";
import {
  FUSION_SYSTEM_PROMPT,
  TARGET_SYSTEM_PROMPT,
  buildFusionPrompt,
  buildTargetSelectionPrompt,
  validateFusionOutput,
  validateTargetSelection,
} from "../src/knowledge/prompts";
import {
  TopicReferenceStore,
  digestSnapshotPath,
  expandLegacyEvidenceMap,
  refAnchor,
  sourceAnchor,
} from "../src/knowledge/citations";
import {
  OrganizeService,
  OrganizeTaskStore,
  ProposalStore,
  idempotencyKey,
  itemIdOfDigestNote,
  looksLegacyProposal,
  readDigestInput,
  renderDiff,
} from "../src/knowledge/organize";
import {
  migrateLegacyKnowledgeNote,
  planRenames,
  renderMigrationReport,
  renderRedirectNote,
  revertRenameMigration,
  rewriteManagedLinks,
  runLegacyKnowledgeMigration,
  runRenameMigration,
} from "../src/knowledge/migrate";
import { ContentFormatError, SyncEngine, assertContentFormat, sourceAnchor as anchorIfSnapshot } from "../src/sync/engine";
import {
  bindingSecretRef,
  generateLocal,
  LocalModelUnavailableError,
  parseModelJson,
  pinConfig,
  resolveLocalModel,
} from "../src/providers/local";
import type {
  ContentDocumentV3,
  ContentRefV3,
  KbManifest,
  KbSettings,
  LocalModelConfig,
  TopicProposal,
} from "../src/types";
import { CONTENT_FORMAT_VERSION, LAYOUT_VERSION } from "../src/types";

/** 与 settings.ts 的默认值保持一致；此处内联以避免冒烟测试依赖 obsidian 模块。 */
const DEFAULT_LOCAL_MODEL: LocalModelConfig = {
  mode: "follow_cloud",
  cloudProfileId: "",
  local: { name: "", baseUrl: "", model: "", secretRef: "kb-local-llm-key" },
  pinnedProfileVersion: null,
  pinnedCredentialVersion: null,
  pinnedEndpoint: "",
  pinnedModel: "",
  awaitingSync: false,
};
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
  localModel: DEFAULT_LOCAL_MODEL,
  localOrganizeEnabled: false,
  autoPrepareOnSync: false,
  organizeDeviceId: "",
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
  assert.equal(knowledgeNotePath("03 Knowledge", "Agent 操作技巧"), "03 Knowledge/Agent 操作技巧.md");
  assert.ok(!sourceNotePath("01 Sources", at, "标题").includes("--"));
  assert.equal(sourceAssetsDir("01 Sources", "item-demo", 2), "01 Sources/_assets/item-demo/source-000002");
  assert.equal(knowledgeRefsFileName(3), "refs-r000003.json");
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
ok("kb_id 稳定：Source/Digest 由 item_id 派生，Knowledge 同名同 ID", () => {
  assert.equal(sourceKbId("item-demo"), "src-item-demo");
  assert.equal(digestKbId("item-demo"), "dig-item-demo");
  assert.equal(knowledgeIdFromTitle("Agent 操作技巧"), knowledgeIdFromTitle("Agent 操作技巧"));
  assert.notEqual(knowledgeIdFromTitle("Agent 操作技巧"), knowledgeIdFromTitle("Agent 上下文管理"));
  assert.ok(knowledgeIdFromTitle("上下文管理").startsWith("kn-"));
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
ok("三个分区各自记录哈希；缺失分区为 null", async () => {
  const text = [
    CLOUD_DIGEST_START, "云端", CLOUD_DIGEST_END,
    LOCAL_ORGANIZE_START, "本地", LOCAL_ORGANIZE_END,
    KNOWLEDGE_START, "主题", KNOWLEDGE_END,
  ].join("\n");
  const h = await partitionHashes(text);
  assert.ok(h.cloud_digest && h.local_organize && h.knowledge);
  const partial = await partitionHashes(`${CLOUD_DIGEST_START}\n云端\n${CLOUD_DIGEST_END}`);
  assert.equal(partial.local_organize, null);
  assert.equal(partial.knowledge, null);
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

// ---- Source / Digest / Knowledge 模板（docs/08 §3）----
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
ok("Digest 笔记：来源链接 + 云端区 + 本地整理区 + 人工区", () => {
  const text = renderDigestNote(manifest, {
    sourceLink: "[[01 Sources/2026/09/2026-09-09 Agent实践|原始资料]]",
    cloudMd: "先可靠保存，再进行提炼。",
    status: "ready",
    contentLines: [`kb_content_revision: 3`, `kb_format_version: "${CONTENT_FORMAT_VERSION}"`, 'kb_completeness: "complete"'],
  });
  assert.ok(text.includes("kb_type: digest"));
  assert.ok(text.includes("kb_content_revision: 3"));
  assert.ok(text.includes(CLOUD_DIGEST_START) && text.includes(CLOUD_DIGEST_END));
  assert.ok(text.includes(LOCAL_ORGANIZE_START) && text.includes(LOCAL_ORGANIZE_END));
  assert.ok(text.includes("尚未本地整理。"));
  assert.ok(text.includes("## 我的备注与判断"));
  assert.ok(text.includes("kb_organize: not_evaluated"), "整理结论是文档级字段");
  assert.ok(!text.includes("kb_promotion"), "逐观点晋升账本已退出，不再写 kb_promotion");
  assert.ok(text.includes("status/not_evaluated"), "status/* 由 kb_organize 派生");
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
ok("本地整理区写主题候选结论，不出现逐观点编号", () => {
  const text = renderLocalOrganize({
    relationNote: "建议修改主题：Agent 操作技巧",
    outcome: "candidate",
    reason: "为现有主题补充一个适用条件",
    targetTitle: "Agent 操作技巧",
    targetPath: "03 Knowledge/Agent 操作技巧.md",
  });
  assert.ok(text.includes("[[03 Knowledge/Agent 操作技巧.md|Agent 操作技巧]]"));
  assert.ok(text.includes("采纳前不会写入 Knowledge"));
  assert.ok(text.includes("为现有主题补充一个适用条件"));
  assert.ok(!/c\d{4}/.test(text), "本地整理区不再有观点编号");
  const kept = renderLocalOrganize({ relationNote: null, outcome: "keep_digest", reason: null, targetTitle: null, targetPath: null });
  assert.ok(kept.includes("结论保留在本 Digest"));
});
ok("Knowledge 笔记：范围说明 + 机器区 + 人工区，aliases 保留", () => {
  const text = renderKnowledgeNote({
    kbId: "kn-agent-operations", title: "Agent 操作技巧", aliases: ["Agent操作技巧"],
    scope: "讨论可复用的 Agent 操作手法。关键词：提示词, 上下文",
    managedBody: "## 当前判断\n\n- 先固定上下文。",
    revision: 1, reviewedAt: "2026-09-09",
  });
  assert.ok(text.includes("kb_type: knowledge"));
  assert.ok(text.includes("kb_revision: 1"));
  assert.ok(text.includes('aliases: ["Agent操作技巧"]'));
  assert.ok(text.includes(KNOWLEDGE_START) && text.includes(KNOWLEDGE_END));
  assert.ok(text.includes("## 我的实践与补充"));
  assert.equal(extractKnowledgeScope(text), "讨论可复用的 Agent 操作手法。关键词：提示词, 上下文");
});
ok("候选索引为普通 Markdown，无候选时给出占位", () => {
  const empty = renderProposalIndex([]);
  assert.ok(empty.includes("（暂无候选）"));
  const withRows = renderProposalIndex([{
    proposalId: "p1", knowledgeTitle: "Agent 操作技巧",
    state: "ready", changeSummary: "增加适用条件", createdAt: "2026-09-09T10:00:00Z", noOp: false,
  }]);
  assert.ok(withRows.includes("| Agent 操作技巧 | 增加适用条件 | ready |"));
  assert.ok(renderProposalIndex([{
    proposalId: "p2", knowledgeTitle: "X", state: "ready", changeSummary: "", createdAt: "2026-09-09T10:00:00Z", noOp: true,
  }]).includes("无变化"));
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
  const block = ["---", "tags:", "  - 用户标签", "  - type/digest", "---", "", "# x"].join("\n");
  const mergedBlock = mergeManagedTags(block, managedTags("knowledge", null));
  assert.ok(mergedBlock.includes("type/knowledge"));
  assert.ok(mergedBlock.includes("用户标签"));
  assert.ok(!mergedBlock.includes("type/digest"));
  const noTags = ["---", 'kb_id: "x"', "---", "", "# x"].join("\n");
  assert.ok(mergeManagedTags(noTags, managedTags("knowledge", null)).includes("tags: [type/knowledge]"));
});
ok("知识更新候选索引为普通 Markdown（无需 Dataview）", () => {
  assert.ok(renderProposalIndex([]).includes("| 生成日期 | 目标主题 | 变化摘要 | 状态 |"));
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
  await rev.save("knowledge", "kn-a", 1, "v1");
  await rev.save("knowledge", "kn-a", 1, "v1");
  await rev.save("knowledge", "kn-a", 2, "v2");
  assert.equal(await rev.read("knowledge", "kn-a", 1), "v1");
  assert.deepEqual(await rev.revisions("knowledge", "kn-a"), [1, 2]);
});
ok("JsonStore 损坏时回退默认值", async () => {
  const fs = new MemFs();
  await fs.write("x.json", "{坏 JSON");
  const store = new JsonStore<{ n: number }>(fs as never, "x.json", () => ({ n: 0 }));
  assert.deepEqual(await store.read(), { n: 0 });
});

// ---- 本地索引与匹配（docs/08 §5）----
const knowledgeNote = renderKnowledgeNote({
  kbId: "kn-agent-operations", title: "Agent 操作技巧", aliases: ["Agent操作技巧", "Agent Ops"],
  scope: "讨论可复用的 Agent 操作手法。关键词：提示词, 上下文",
  managedBody: "## 当前判断\n\n- [c0001] 先固定上下文。", revision: 2, reviewedAt: "2026-09-09",
});
ok("解析 Knowledge 索引条目：标题/kb_id/aliases/范围/关键词", () => {
  const entry = parseKnowledgeEntry(knowledgeNote, "03 Knowledge/Agent 操作技巧.md");
  assert.ok(entry);
  assert.equal(entry!.kb_id, "kn-agent-operations");
  assert.equal(entry!.title, "Agent 操作技巧");
  assert.deepEqual(entry!.aliases, ["Agent操作技巧", "Agent Ops"]);
  assert.deepEqual(entry!.keywords, ["提示词", "上下文"]);
  assert.equal(entry!.revision, 2);
});
ok("普通笔记（无 kb_id）不进入索引", () => {
  assert.equal(parseKnowledgeEntry("# 随手记\n\n内容", "随手记.md"), null);
});
ok("匹配顺序：精确标题/别名优先于关键词检索", () => {
  const entries = [
    { kb_id: "kn-a", title: "Agent 操作技巧", path: "03 Knowledge/Agent 操作技巧.md",
      aliases: ["Agent操作技巧"], scope: "关键词：提示词", keywords: ["提示词"],
      revision: 1, reviewed_at: null, body_hash: "" },
    { kb_id: "kn-b", title: "上下文管理", path: "03 Knowledge/上下文管理.md",
      aliases: [], scope: "关键词：上下文", keywords: ["上下文"],
      revision: 1, reviewed_at: null, body_hash: "" },
  ];
  const exact = matchKnowledge(entries, { title: "Agent操作技巧", text: "上下文管理很重要" });
  assert.equal(exact.length, 1);
  assert.equal(exact[0].via, "exact");
  assert.equal(exact[0].entry.kb_id, "kn-a");
  const keyword = matchKnowledge(entries, { title: "陌生标题", text: "上下文分配" });
  assert.equal(keyword[0].via, "keyword");
  assert.equal(keyword[0].entry.kb_id, "kn-b");
  assert.deepEqual(matchKnowledge(entries, { title: "完全无关", text: "完全无关内容" }), []);
});
ok("候选数量受上限约束（实现参数，非质量保证）", () => {
  const entries = Array.from({ length: 10 }, (_, i) => ({
    kb_id: `kn-${i}`, title: `主题 ${i}`, path: `03 Knowledge/主题${i}.md`,
    aliases: [], scope: "关键词：通用", keywords: ["通用"],
    revision: 1, reviewed_at: null, body_hash: "",
  }));
  assert.equal(matchKnowledge(entries, { title: "通用", text: "通用" }, 5).length, 5);
});
ok("中文分词：2 字滑窗可用于本地检索", () => {
  const tokens = tokenize("Agent 上下文管理");
  assert.ok(tokens.includes("agent"));
  assert.ok(tokens.includes("上下"));
  assert.ok(tokens.includes("文管"));
});
ok("索引重建扫描 03 Knowledge 并写入 knowledge-index.json", async () => {
  const fs = new MemFs();
  await fs.write("03 Knowledge/Agent 操作技巧.md", knowledgeNote);
  await fs.write("03 Knowledge/随手记.md", "# 随手记\n\n内容");
  const store = new KnowledgeIndexStore(fs as never, "99 System");
  const doc = await store.rebuild("03 Knowledge");
  assert.equal(doc.entries.length, 1);
  assert.equal(doc.entries[0].kb_id, "kn-agent-operations");
  assert.ok(await fs.exists("99 System/KnowledgeInbox/knowledge-index.json"));
});
// ---- 本地模型解析（docs/08 §8.2、§8.3）----
function settingsWith(localModel: Partial<KbSettings["localModel"]>): KbSettings {
  return {
    ...DEFAULT_SETTINGS,
    serverUrl: "https://kb.example.com",
    deviceId: "dev-1",
    localModel: { ...DEFAULT_LOCAL_MODEL, ...localModel },
  };
}
const fakeCloud = (over: Partial<{
  defaultId: string | null; profiles: unknown[]; bound: boolean;
}> = {}) => ({
  listProfiles: async () => (over.profiles ?? [{
    id: "p1", kind: "llm", adapter: "openai", endpoint: "https://api.example.com/v1",
    model: "model-a", capabilities: {}, version: 2, configured: true,
    credential_version: 5, created_at: "2026-09-09T00:00:00Z",
  }]) as never,
  getDefaultProfileId: async () => over.defaultId === undefined ? "p1" : over.defaultId,
  bindingStatus: async () => ({
    bound: over.bound ?? true, profile_version: 2, credential_version: 5,
  }),
  bind: async () => ({
    binding_id: "b1", profile_id: "p1", profile_version: 2, credential_version: 5,
    endpoint: "https://api.example.com/v1", model: "model-a", capabilities: {},
    secret: "sk-x", bound_at: "2026-09-09T00:00:00Z", note: "",
  }),
});
const fakeSecrets = (values: Record<string, string> = {}) => ({
  getSecretStrict: async (ref: string) => values[ref] ?? null,
});
ok("local_profile 模式：使用本机独立配置与秘密存储", async () => {
  const s = settingsWith({
    mode: "local_profile",
    local: { name: "本地 A", baseUrl: "https://api.deepseek.com/v1", model: "deepseek-chat", secretRef: "kb-local-llm-key" },
  });
  const resolved = await resolveLocalModel({
    settings: s, secrets: fakeSecrets({ "kb-local-llm-key": "sk-local" }), cloud: null,
  });
  assert.equal(resolved.mode, "local_profile");
  assert.equal(resolved.apiKey, "sk-local");
  assert.equal(resolved.model, "deepseek-chat");
});
ok("local_profile 缺少 Key 时不回退到其他配置", async () => {
  const s = settingsWith({
    mode: "local_profile",
    local: { name: "本地 A", baseUrl: "https://api.deepseek.com/v1", model: "m", secretRef: "kb-local-llm-key" },
  });
  await assert.rejects(
    () => resolveLocalModel({ settings: s, secrets: fakeSecrets(), cloud: fakeCloud() as never }),
    (err: unknown) => err instanceof LocalModelUnavailableError && err.reason === "no_secret",
  );
});
ok("follow_cloud 使用服务器默认配置并要求已绑定", async () => {
  const s = settingsWith({ mode: "follow_cloud" });
  const resolved = await resolveLocalModel({
    settings: s,
    secrets: fakeSecrets({ [bindingSecretRef("p1")]: "sk-bound" }),
    cloud: fakeCloud() as never,
  });
  assert.equal(resolved.cloudProfileId, "p1");
  assert.equal(resolved.profileVersion, 2);
  assert.equal(resolved.credentialVersion, 5);
});
ok("未绑定时提示绑定，不静默换 Key", async () => {
  const s = settingsWith({ mode: "follow_cloud" });
  await assert.rejects(
    () => resolveLocalModel({
      settings: s, secrets: fakeSecrets(), cloud: fakeCloud({ bound: false }) as never,
    }),
    (err: unknown) => err instanceof LocalModelUnavailableError && err.reason === "no_binding",
  );
});
ok("无法确认线上配置时进入等待同步，不换 Key", async () => {
  const s = settingsWith({ mode: "follow_cloud" });
  await assert.rejects(
    () => resolveLocalModel({
      settings: s, secrets: fakeSecrets(), cloud: fakeCloud({ profiles: [] }) as never,
    }),
    (err: unknown) => err instanceof LocalModelUnavailableError && err.reason === "awaiting_sync",
  );
  await assert.rejects(
    () => resolveLocalModel({
      settings: s, secrets: fakeSecrets(), cloud: fakeCloud({ defaultId: null }) as never,
    }),
    (err: unknown) => err instanceof LocalModelUnavailableError && err.reason === "not_configured",
  );
});
ok("线上配置版本变化后重新同步（同一配置以一次已同步版本为准）", async () => {
  const s = settingsWith({
    mode: "cloud_profile", cloudProfileId: "p1",
    pinnedProfileVersion: 1, pinnedCredentialVersion: 4,
    pinnedEndpoint: "https://old.example.com/v1", pinnedModel: "model-old",
  });
  const resolved = await resolveLocalModel({
    settings: s,
    secrets: fakeSecrets({ [bindingSecretRef("p1")]: "sk-bound" }),
    cloud: fakeCloud() as never,
  });
  assert.equal(resolved.profileVersion, 2);
  assert.equal(resolved.baseUrl, "https://api.example.com/v1");
});
ok("pinConfig 固定版本供进行中的任务使用", () => {
  const pinned = pinConfig(DEFAULT_LOCAL_MODEL, {
    mode: "follow_cloud", label: "m", baseUrl: "https://x/v1", model: "m",
    capabilities: {}, apiKey: "sk", profileVersion: 3, credentialVersion: 7, cloudProfileId: "p1",
  });
  assert.equal(pinned.pinnedProfileVersion, 3);
  assert.equal(pinned.pinnedCredentialVersion, 7);
  assert.equal(pinned.pinnedModel, "m");
  assert.equal(pinned.awaitingSync, false);
});
ok("直连调用只发往选定模型服务，不经过本系统云端", async () => {
  const seen: string[] = [];
  const transport = {
    post: async (url: string, headers: Record<string, string>, body: string) => {
      seen.push(url);
      assert.equal(headers.Authorization, "Bearer sk-bound");
      assert.ok(body.includes("\"model\":\"model-a\""));
      return { status: 200, text: JSON.stringify({ choices: [{ message: { content: "{\"ok\":true}" }, finish_reason: "stop" }] }) };
    },
  };
  const res = await generateLocal(transport, {
    mode: "follow_cloud", label: "m", baseUrl: "https://api.example.com/v1", model: "model-a",
    capabilities: {}, apiKey: "sk-bound", profileVersion: 2, credentialVersion: 5, cloudProfileId: "p1",
  }, { system: "s", user: "u", jsonMode: true });
  assert.deepEqual(seen, ["https://api.example.com/v1/chat/completions"]);
  assert.deepEqual(parseModelJson(res.outputText), { ok: true });
});
ok("模型错误分类：401→auth、429→retryable、超时→unknown_outcome", async () => {
  const base = {
    mode: "follow_cloud" as const, label: "m", baseUrl: "https://x/v1", model: "m",
    capabilities: {}, apiKey: "k", profileVersion: 1, credentialVersion: 1, cloudProfileId: "p",
  };
  const call = (post: Parameters<typeof generateLocal>[0]["post"]) =>
    generateLocal({ post }, base, { system: "s", user: "u" });
  await assert.rejects(() => call(async () => ({ status: 401, text: "" })),
    (e: unknown) => e instanceof Error && (e as { kind?: string }).kind === "auth");
  await assert.rejects(() => call(async () => ({ status: 429, text: "" })),
    (e: unknown) => e instanceof Error && (e as { kind?: string }).kind === "retryable");
  await assert.rejects(() => call(async () => ({ status: 0, text: "", timedOut: true })),
    (e: unknown) => e instanceof Error && (e as { kind?: string }).kind === "unknown_outcome");
  await assert.rejects(() => call(async () => ({ status: 0, text: "", connectFailed: true })),
    (e: unknown) => e instanceof Error && (e as { kind?: string }).kind === "retryable");
});

// ---- ContentDocument v3：解析、程序组装与渲染（docs/24 §1–§5）----
const SNAPSHOT: Record<string, Record<string, string>> = {
  "item-a@2": {
    s0001: "采集与总结应该分开处理，避免混在一起。",
    s0002: "本地整理才决定是否晋升为长期知识。",
  },
  "item-b@1": {
    s0001: "另一篇材料里的同一号片段，讲的是投递回执。",
  },
};
const segText = (itemId: string, sourceRevision: number, segmentId: string): string | null =>
  SNAPSHOT[`${itemId}@${sourceRevision}`]?.[segmentId] ?? null;

// 组装依赖 sha256（异步），而 esbuild 产出的 `.smoke/*.cjs` 不支持顶层 await：
// 这一节的共享夹具用 memoized promise 承载，各检查在自己的 async 体内取值。
const v3Base = (async () => {
  const refTable = await buildRefTableFromSegments([
    { item_id: "item-a", source_revision: 2, segment_ids: ["s0001"] },
    { item_id: "item-a", source_revision: 2, segment_ids: ["s0002"] },
    { item_id: "item-b", source_revision: 1, segment_ids: ["s0001"] },
  ], segText);
  const assembleOpts = {
    documentId: "dig-item-a",
    kind: "digest",
    revision: 3,
    task: "digest",
    recipeVersion: "content-v3-1",
    refTable,
    segmentTexts: segText,
    createdAt: "2026-09-22T00:00:00Z",
  };
  return { refTable, assembleOpts };
})();

ok("引用表按材料顺序分配 R 键，两份材料的 s0001 不互相覆盖", async () => {
  const { refTable } = await v3Base;
  assert.deepEqual(Object.keys(refTable), ["R1", "R2", "R3"]);
  assert.equal(refTable.R1.item_id, "item-a");
  assert.equal(refTable.R3.item_id, "item-b");
  assert.equal(refTable.R1.segment_ids.join(","), "s0001");
  assert.equal(refTable.R3.segment_ids.join(","), "s0001");
  assert.notEqual(refTable.R1.source_text_hash, refTable.R3.source_text_hash);
  assert.equal(await refTextHashOf([SNAPSHOT["item-a@2"].s0001]), refTable.R1.source_text_hash);
});

const goodSubject = {
  title: "内容接收与提炼",
  summary: "先可靠保存，再进行提炼。",
  sections: [
    {
      heading: "主要判断",
      blocks: [
        { kind: "text", text: "下面区分来源主张与逐字摘录。", refs: [] },
        { kind: "claim", text: "采集与总结应分开处理。", refs: ["R1"] },
        { kind: "quote", text: "采集与总结应该分开处理， 避免混在一起。", refs: ["R1"] },
        { kind: "suggestion", text: "可以分别统计两个阶段的失败原因。", refs: [] },
      ],
    },
    {
      heading: "另一来源",
      blocks: [{ kind: "claim", text: "回执也要分开处理。", refs: ["R3", "R3 ", " R1"] }],
    },
  ],
  limitations: ["未取得视频画面上的文字"],
  // 模型多返回的程序字段必须被忽略，不请求修复（docs/24 §3）
  format_version: "2.0",
  document_id: "伪造",
  revision: 99,
  references: { e1: { item_id: "伪造" } },
  source_text_hash: "伪造",
};

const v3Doc = (async () => {
  const { assembleOpts } = await v3Base;
  const assembled = await assembleContentDocument(goodSubject, assembleOpts);
  return { assembled, assembleOpts };
})();

ok("组装：程序填身份与引用表，模型回填字段被忽略，refs 去空白精确去重", async () => {
  const { assembled } = await v3Doc;
  assert.equal(assembled.errors.length, 0);
  assert.equal(assembled.completeness.state, "complete");
  const doc = assembled.document!;
  assert.equal(doc.format_version, CONTENT_FORMAT_VERSION);
  assert.equal(doc.document_id, "dig-item-a");
  assert.equal(doc.revision, 3);
  assert.equal(doc.provenance.recipe_version, "content-v3-1");
  assert.deepEqual(Object.keys(doc.references), ["e1", "e2"], "只给实际用到的来源分配 e 键");
  assert.equal(doc.references.e2.item_id, "item-b");
  assert.deepEqual(doc.sections[0].blocks[1].refs, ["e1"]);
  assert.deepEqual(doc.sections[1].blocks[0].refs, ["e2", "e1"]);
  assert.ok(!JSON.stringify(doc).includes("伪造"));
});

ok("quote 逐字校验只忽略空白；非法引用与无依据 claim 丢弃但不作废全文", async () => {
  const { assembleOpts } = await v3Base;
  const bad = await assembleContentDocument({
    title: "部分结果",
    summary: "仍有可用内容。",
    sections: [{
      heading: "混合",
      blocks: [
        { kind: "quote", text: "被改写过的句子", refs: ["R1"] },
        { kind: "claim", text: "指向不存在的引用", refs: ["R99"] },
        { kind: "claim", text: "没有任何依据的断言", refs: [] },
        { kind: "claim", text: "有效主张。", refs: ["R2"] },
        { kind: "text", text: "超出长度".repeat(LIMITS.blockText + 1), refs: [] },
      ],
    }],
    limitations: [],
  }, assembleOpts);
  assert.equal(bad.completeness.state, "partial");
  assert.equal(bad.dropped_blocks, 4);
  const codes = bad.completeness.gaps.map((g) => g.code);
  assert.ok(codes.includes("quote_not_verbatim"), codes.join(","));
  assert.ok(codes.includes("bad_ref"));
  assert.ok(codes.includes("limit_exceeded"));
  assert.equal(bad.document!.sections[0].blocks.length, 1);
  assert.equal(bad.completeness.gaps.every((g) => Boolean(g.message)), true, "缺口要有给用户看的中文说明");
});

ok("只剩空架子时判为失败，不靠一个摘要假装完成", async () => {
  const { assembleOpts } = await v3Base;
  const empty = await assembleContentDocument({
    title: "空",
    summary: "",
    sections: [{ heading: "只有无效引用", blocks: [{ kind: "claim", text: "无依据", refs: [] }] }],
    limitations: [],
  }, assembleOpts);
  assert.equal(empty.document, null);
  assert.equal(empty.completeness.state, "failed");
  assert.ok(empty.errors.includes("empty_document"));
});

ok("解析 content.json：未知 format_version 与缺字段都拒绝，不静默降级", async () => {
  const { assembled } = await v3Doc;
  const roundTrip = parseContentDocument(assembled.document);
  assert.equal(roundTrip.errors.length, 0);
  assert.equal(roundTrip.document?.sections[0].blocks[1].kind, "claim");
  const tooNew = parseContentDocument({ ...(assembled.document as ContentDocumentV3), format_version: "4.0" });
  assert.equal(tooNew.document, null);
  assert.ok(tooNew.errors[0].includes("4.0"));
  assert.equal(parseContentDocument({ kind: "digest" }).document, null);
  const dangling = parseContentDocument({
    ...(assembled.document as ContentDocumentV3),
    sections: [{ heading: "x", blocks: [{ kind: "claim", text: "y", refs: ["e404"] }] }],
  });
  assert.equal(dangling.document, null);
  assert.ok(dangling.errors.some((e) => e.includes("e404")));
});

ok("渲染：四种角色各有展示区别，界面不出现裸内部编号", async () => {
  const { assembled } = await v3Doc;
  const md = renderContentMarkdown(assembled.document!, {
    sourceLinkOf: (ref) => refAnchor("01 Sources", ref),
  });
  assert.ok(md.includes("先可靠保存，再进行提炼。"));
  assert.ok(md.includes("## 主要判断"));
  assert.ok(md.includes("- 采集与总结应分开处理。"));
  assert.ok(md.includes("> 采集与总结应该分开处理， 避免混在一起。"), "quote 用引用样式");
  assert.ok(md.includes("[!question] AI 建议"), "suggestion 标明 AI 建议/待验证");
  assert.ok(md.includes("下面区分来源主张与逐字摘录。"), "text 作为导语直接成段");
  assert.ok(md.includes("[[01 Sources/_assets/item-a/source-000002/normalized#^s0001|查看原文]]"));
  assert.ok(!/\[\[#[^^]/.test(md));
  assert.ok(!/\bR\d+\b/.test(md) && !/\be\d+\b/.test(md), "正文不显示 R1/e1");
  assert.ok(!/\bs\d{4}\b(?!\])/.test(md.replace(/\[\[[^\]]*\]\]/g, "")), "链接文字里不出现片段编号");
  assert.ok(md.includes("## 限制与未解决的问题"));
});

ok("部分结果与引用表渲染：多来源分别成行，缺口可见", async () => {
  const { assembled } = await v3Doc;
  const notice = renderCompletenessNotice(assembled.completeness);
  assert.deepEqual(notice, [], "complete 不加警示");
  const table = renderReferenceTable(assembled.document!, {
    sourceLinkOf: (ref) => refAnchor("01 Sources", ref),
    sourceTitleOf: (ref) => (ref.item_id === "item-b" ? "另一篇文章" : "本文"),
  });
  assert.ok(table.includes("## 依据与原文"));
  assert.ok(table.includes("本文") && table.includes("另一篇文章"));
  assert.equal((table.match(/查看原文/g) ?? []).length, 2, "每条引用各一行，不按同号片段合并");
  const ghost: ContentRefV3 = { item_id: "no-such", source_revision: 9, segment_ids: ["s0001"], source_text_hash: "" };
  assert.equal(anchorIfSnapshot("01 Sources", ghost, new Set(["item-x@2"])), null, "本地没有该版原文时不给链接");
  assert.ok(String(anchorIfSnapshot("01 Sources", assembled.document!.references.e1, new Set(["item-a@2"])))
    .endsWith("normalized#^s0001"), "已有本地快照时给出锚点");
  const missing = renderReferenceTable({ ...assembled.document!, references: { e1: ghost } },
    { sourceLinkOf: (ref) => anchorIfSnapshot("01 Sources", ref, new Set()) });
  assert.ok(missing.includes("本地原文快照缺失"), missing);
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

// ---- 部分采纳：按块重组正文与引用（docs/23 §6.3）----
ok("按块勾选后重排正文、清除未用引用并重新校验摘录", async () => {
  const { assembled } = await v3Doc;
  const doc = assembled.document!;
  const keep = doc.sections.map((s) => s.blocks.map(() => false));
  keep[0][2] = true; // 只保留那条 quote
  const pruned = selectBlocksForAdoption(doc, keep);
  assert.deepEqual(Object.keys(pruned.document.references), ["e1"]);
  assert.deepEqual(pruned.document.sections[0].blocks[0].refs, ["e1"]);
  assert.equal(pruned.dropped_refs, 1);
  assert.deepEqual(quoteVerificationErrors(pruned.document, segText), []);
  const edited = { ...pruned.document, sections: [{ heading: "主要判断", blocks: [{ kind: "quote" as const, text: "被人改掉的摘录", refs: ["e1"] }] }], references: pruned.document.references };
  assert.ok(quoteVerificationErrors({ ...edited, references: pruned.document.references } as ContentDocumentV3, segText).length === 1);
  const hashBad = await referenceHashErrors({
    ...doc,
    references: { e1: { ...doc.references.e1, source_text_hash: "错" } },
  }, segText);
  assert.ok(hashBad.length === 1 && hashBad[0].includes("摘要与固定版本不一致"));
  const missingSnap = await referenceHashErrors({
    ...doc,
    references: { e1: { item_id: "ghost", source_revision: 1, segment_ids: ["s0001"], source_text_hash: "" } },
  }, segText);
  assert.ok(missingSnap[0].includes("暂缓采纳"), "引用缺失时暂缓，不用标题或摘要补位");
});

// ---- 共享夹具（docs/24 §10）：与服务端 pytest 读同一批 JSON，跨实现互校 ----
// 从打包产物目录回溯到仓库根：`.smoke/smoke.cjs` → `../../../tests/fixtures/content_v3`。
const FIXTURE_DIR = join(__dirname, "..", "..", "..", "tests", "fixtures", "content_v3");
function fixture(name: string): any {
  return JSON.parse(readFileSync(join(FIXTURE_DIR, name), "utf-8"));
}
/** 夹具的 R 表带的是拼接原文：拆回逐段文本，供哈希与逐字校验复用（docs/24 §2）。 */
function fixtureSegmentTexts(table: Record<string, any>) {
  const map = new Map<string, string>();
  for (const entry of Object.values(table)) {
    const parts = String(entry.text).split("\n");
    assert.equal(parts.length, entry.segment_ids.length, `${entry.item_id}：夹具原文段数与 segment_ids 不符`);
    entry.segment_ids.forEach((sid: string, i: number) => map.set(`${entry.item_id}@${entry.source_revision}|${sid}`, parts[i]));
  }
  return (itemId: string, sourceRevision: number, segmentId: string): string | null =>
    map.get(`${itemId}@${sourceRevision}|${segmentId}`) ?? null;
}
/** 用夹具写死的组装参数跑一次组装（与服务端 pytest 同一调用形式）。 */
async function assembleWithFixture(params: any, refTableFile: string, subject: unknown) {
  const table = fixture(refTableFile);
  const segmentTexts = fixtureSegmentTexts(table);
  const refTable = await buildRefTableFromSegments(
    Object.values(table).map((e: any) => ({
      item_id: e.item_id, source_revision: e.source_revision, segment_ids: e.segment_ids, locator: e.locator ?? null,
    })),
    segmentTexts,
  );
  const report = await assembleContentDocument(subject, {
    documentId: params.document_id,
    kind: params.kind,
    revision: params.revision,
    task: params.task,
    recipeVersion: params.recipe_version,
    refTable,
    segmentTexts,
    repairCalls: params.repair_calls ?? 0,
    createdAt: params.created_at,
    inputDocuments: params.input_documents ?? [],
    missingStages: params.missing_stages ?? [],
    extraGaps: params.extra_gaps ?? [],
  });
  return { report, segmentTexts };
}
const codesOf = (gaps: Array<{ code: string }>): string[] => gaps.map((g) => g.code);

ok("共享夹具：content.json 解析进 v3 类型，引用哈希与夹具（Python 端）同一算法", async () => {
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
      assert.equal(
        ref.source_text_hash,
        await refTextHashOf(String(entry.text).split("\n")),
        `${docFile} ${key}：与 docs/24 §2 的哈希算法不一致`,
      );
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
  const md = renderContentMarkdown(doc, { sourceLinkOf: (ref) => refAnchor("01 Sources", ref) });
  const item = doc.references.e1.item_id;
  assert.ok(md.includes(`[[01 Sources/_assets/${item}/source-000001/normalized#^s0001|第 1 段 查看原文]]`),
    `依据要直连该版本原文锚点、标签只说自然位置，实际：${md}`);
  assert.ok(!md.includes("第 p0001 段"), "段落编号 p0001 不得出现在给用户看的文字里");
  assert.ok(!/\be\d+\b/.test(md) && !/\bR\d+\b/.test(md), "界面不显示裸内部编号");
  assert.ok(md.includes("样本只覆盖图文来源"), "限制照实展示");
  const conv = parseContentDocument(fixture("document_conversation.json")).document!;
  const convMd = renderContentMarkdown(conv, { sourceLinkOf: (ref) => refAnchor("01 Sources", ref) });
  assert.ok(conv.sections.length >= 2, "对话按自然章节呈现");
  assert.ok(!/\bc\d{4}\b/.test(convMd) && !/\bR\d+\b/.test(convMd), "对话产物也不得出现观点编号");
});

ok("共享夹具：多来源同号片段组装后各自成引用（expectations_multi_source）", async () => {
  const spec = fixture("expectations_multi_source.json");
  const { report } = await assembleWithFixture(
    { ...spec.assembly, input_documents: spec.assembly.input_documents },
    "ref_table_multi_source.json",
    spec.model_output,
  );
  const expect = spec.expect;
  assert.equal(report.completeness.state, expect.state);
  assert.equal(report.completeness.gaps.length, 0, codesOf(report.completeness.gaps).join(","));
  assert.equal(report.document!.summary, expect.summary);
  assert.equal(report.dropped_blocks, expect.dropped_blocks);
  const references = report.document!.references as Record<string, any>;
  for (const [key, want] of Object.entries(expect.references as Record<string, any>)) {
    assert.deepEqual(
      { item_id: references[key].item_id, source_revision: references[key].source_revision, segment_ids: references[key].segment_ids },
      want, `${key} 引用身份不符`,
    );
    assert.match(references[key].source_text_hash, /^[0-9a-f]{64}$/);
  }
  const [a, b] = Object.entries(expect.same_segment_id_different_item as Record<string, string>);
  assert.equal(references[a[0]].segment_ids[0], a[1]);
  assert.equal(references[b[0]].segment_ids[0], b[1]);
  assert.notEqual(references[a[0]].item_id, references[b[0]].item_id, "两份材料的 s0001 不得合并");
  assert.notEqual(references[a[0]].source_text_hash, references[b[0]].source_text_hash);
  assert.deepEqual(report.document!.provenance.source_revisions, expect.provenance_source_revisions);
  assert.equal(report.document!.provenance.input_documents.length, expect.input_documents);
  assert.equal(independentSourceCount(report.document!), 2, "独立来源数按真实来源记录判断");
});

ok("共享夹具：坏输出按 expectations_bad_outputs 分类，部分结果不冒充完整成功", async () => {
  const spec = fixture("expectations_bad_outputs.json");
  for (const item of spec.cases as any[]) {
    const source = fixture(item.file);
    const subject = typeof source.raw_output === "string" ? source.raw_output : source.model_output;
    const { report } = await assembleWithFixture(spec.assembly, item.ref_table, subject);
    const want = item.expect;
    const at = item.file;
    assert.equal(report.document === null, want.document_is_none === true, `${at}：是否应无文档`);
    assert.equal(report.completeness.state, want.state, `${at}：状态`);
    for (const code of want.error_codes as string[]) assert.ok(report.errors.includes(code), `${at}：缺错误码 ${code}`);
    for (const code of want.gap_codes as string[]) assert.ok(codesOf(report.completeness.gaps).includes(code), `${at}：缺缺口 ${code}`);
    if (want.missing_stages) assert.deepEqual(report.completeness.missing_stages, want.missing_stages, at);
    assert.equal(report.dropped_blocks, want.dropped_blocks, `${at}：丢弃块数`);
    if (typeof want.repair_calls === "number") assert.equal(report.completeness.repair_calls, want.repair_calls, at);
    if (!report.document) continue;
    const doc = report.document;
    assert.equal(Object.keys(doc.references).length, want.reference_count, `${at}：引用条数`);
    assert.equal(doc.summary, want.summary, `${at}：摘要（被丢依据独占时由程序清空）`);
    assert.deepEqual(doc.sections.map((s) => s.blocks.map((b) => b.kind)), want.kinds, at);
    for (const token of (want.invalid_refs_kept as string[]) ?? []) {
      // 缺口诊断里可以原样写出 R99 供排查，但正文与引用表里不得出现非法引用
      const published = JSON.stringify({ sections: doc.sections, references: doc.references });
      assert.ok(!published.includes(token), `${at}：非法引用 ${token} 不得留在文档里`);
    }
    if (want.not_demoted_to_suggestion) {
      assert.ok(!doc.sections.some((s) => s.blocks.some((b) => b.text.includes("一份依据有效、一份无效"))),
        `${at}：整条暂不发布，不能悄悄降级成建议`);
    }
  }
});

ok("共享夹具：长字幕分块失败样本按 partial 呈现（chunk:2、摘要清空、引用只留核实部分）", async () => {
  const spec = fixture("expectations_long_subtitle.json");
  const { report } = await assembleWithFixture(
    spec.assembly, "ref_table_long_subtitle.json", fixture("model_output_long_subtitle.json"),
  );
  const want = spec.expect;
  assert.equal(report.completeness.state, want.state);
  assert.equal(report.document!.summary, want.summary, want.summary_cleared ? "被丢摘录独占依据时摘要由程序清空" : "摘要");
  assert.deepEqual(report.completeness.missing_stages, want.missing_stages);
  for (const code of want.gap_codes as string[]) {
    assert.ok(codesOf(report.completeness.gaps).includes(code), `缺口缺少 ${code}：${codesOf(report.completeness.gaps).join(",")}`);
  }
  assert.equal(report.dropped_blocks, want.dropped_blocks);
  assert.equal(report.document!.title, want.title);
  assert.deepEqual(report.document!.sections.map((s) => ({ heading: s.heading, kinds: s.blocks.map((b) => b.kind) })), want.sections);
  for (const [key, ref] of Object.entries(want.references as Record<string, any>)) {
    assert.deepEqual(
      { item_id: report.document!.references[key].item_id, source_revision: report.document!.references[key].source_revision, segment_ids: report.document!.references[key].segment_ids },
      ref, `${key} 的原文范围`,
    );
    assert.match(report.document!.references[key].source_text_hash, /^[0-9a-f]{64}$/, "哈希是 sha256");
  }
  const e1 = report.document!.references[Object.keys(want.locator_of as Record<string, unknown>)[0]];
  assert.deepEqual(e1.locator, (want.locator_of as any).e1, "locator 原样保留，供界面显示自然位置");
  const notice = renderCompletenessNotice(report.completeness);
  assert.ok(notice.some((l) => l.includes("部分结果")), "部分结果必须在笔记里看得出来");
  assert.ok(notice.some((l) => l.includes("第 2 段字幕加工失败")), "调用方给的分块事实要显示成人话");
});

ok("共享夹具：旧 Knowledge 笔记迁移保留人工区与未知字段，旧锚点进历史区", () => {
  const note = readFileSync(join(FIXTURE_DIR, "legacy_knowledge_note.md"), "utf-8");
  const legacy = fixture("legacy_evidence_map.json");
  // 夹具里是服务端旧 analysis 的 evidence_map（claim → evidence_ids）。插件只认本地形态，
  // 且必须要求显式 digest_claim_id：直接喂进来应当全部报错，而不是拿 c0001 当摘要锚点猜。
  const refused = expandLegacyEvidenceMap(legacy.claims as Record<string, unknown>);
  assert.equal(refused.refs.length, 0);
  assert.equal(refused.errors.length, Object.keys(legacy.claims).length);
  assert.ok(refused.errors.every((e) => e.includes("digest_claim_id") || e.includes("原文定位")), refused.errors.join("；"));

  const localEntry = (claimId: string, claim: any): [string, unknown] => [claimId, {
    knowledge_id: legacy.document_id, knowledge_revision: 3,
    digest_id: `dig-${legacy.source_item_id}`, digest_revision: legacy.bundle_revision,
    digest_claim_id: claimId, item_id: legacy.source_item_id,
    source_revision: legacy.source_revision, segment_ids: claim.evidence_ids,
  }];
  // 只有 claims 两条时，正文里的 c0003 无对应依据 → 不改写该主题，列入迁移待处理（docs/23 §8.2）
  const partialMap = Object.fromEntries(Object.entries<any>(legacy.claims).map(([id, c]) => localEntry(id, c)));
  const refused2 = migrateLegacyKnowledgeNote({
    kbId: legacy.document_id, revision: 3, text: note, evidenceMap: partialMap,
    sourcesFolder: "01 Sources", systemFolder: "99 System",
  });
  assert.equal(refused2.text, null, "映射与正文对不上时不自动改写");
  assert.ok(refused2.errors.some((e: string) => e.includes("c0003")), refused2.errors.join("；"));
  assert.ok(renderMigrationReport({
    migrated: [], pending: [{ kb_id: legacy.document_id, path: "03 Knowledge/内容接收与提炼.md", reason: refused2.errors[0] }],
    unchanged: 0, conflicts: [],
  }).includes("迁移待处理"));

  const evidenceMap = Object.fromEntries([
    ...Object.entries<any>(legacy.claims).map(([id, c]) => localEntry(id, c)),
    ...Object.entries<any>(legacy.extra_anchors).map(([id, c]) => localEntry(id, c)),
  ]);
  const out = migrateLegacyKnowledgeNote({
    kbId: legacy.document_id, revision: 3, text: note, evidenceMap,
    sourcesFolder: "01 Sources", systemFolder: "99 System",
  });
  assert.equal(out.errors.length, 0, out.errors.join("；"));
  const body = out.text!;
  const managed = extractPartition(body, KNOWLEDGE_START, KNOWLEDGE_END)!;
  assert.ok(managed.includes(`[[01 Sources/_assets/${legacy.source_item_id}/source-00000${legacy.source_revision}/normalized#^s0001|查看原文]]`),
    `正文直连固定原文，实际：${managed}`);
  assert.ok(!/\[c\d{4}\]/.test(managed), "新正文不再有观点编号");
  assert.ok(!managed.includes("revisions/digests"), "不再两跳经过 Digest 快照");
  const history = extractPartition(body, KNOWLEDGE_HISTORY_START, KNOWLEDGE_HISTORY_END)!;
  assert.ok(history.includes("#^c0001") && history.includes("#^c0002"), "旧快照锚点保留，外部链接仍可打开");
  assert.ok(/主题 rev 3/.test(history), "历史区标注所属历史版本");
  assert.ok(body.includes("这段是用户手写内容，自动融合不得改写。"), "人工区一字不动");
  assert.ok(body.includes("kb_custom_local_field: 用户自己的插件字段，迁移必须原样保留"), "未知 frontmatter 保留");
  assert.ok(body.includes("tags:") && body.includes("- 知识管理"), "用户自己的标签保留");
  assert.ok(body.includes("## 我自己补充的部分"));
});

// ---- 直连原文与旧证据展开（docs/23 §8.2）----
ok("旧 evidence_map 展开为直接 Source 引用，两份 s0001 分别保留", () => {
  const { refs, errors } = expandLegacyEvidenceMap({
    c0001: {
      knowledge_id: "kn-a", knowledge_revision: 3, digest_id: "dig-item-a", digest_revision: 3,
      digest_claim_id: "c0007", item_id: "item-a", source_revision: 2, segment_ids: ["s0001"],
    },
    c0002: {
      knowledge_id: "kn-a", knowledge_revision: 3, digest_id: "dig-item-b", digest_revision: 1,
      digest_claim_id: "c0001", item_id: "item-b", source_revision: 1, segment_ids: ["s0001", "s0002"],
    },
    c0003: { knowledge_id: "kn-a", item_id: "item-c", source_revision: 1 },
  });
  assert.equal(errors.length, 1, "缺 segment_ids/digest_claim_id 的条目要报出来");
  assert.equal(refs.length, 2);
  assert.equal(refs[0].digest_claim_id, "c0007", "旧快照锚点用 digest_claim_id，不是 knowledge claim_id");
  assert.equal(refs[1].item_id, "item-b");
  assert.notEqual(
    sourceAnchor("01 Sources", refs[0].item_id, refs[0].source_revision, refs[0].segment_ids[0]),
    sourceAnchor("01 Sources", refs[1].item_id, refs[1].source_revision, refs[1].segment_ids[0]),
  );
});

const legacyNote = [
  "---",
  'kb_id: "kn-a"',
  "kb_type: knowledge",
  "kb_revision: 3",
  'custom: "用户字段"',
  "---",
  "",
  "# 投递与回执",
  "",
  "## 这个主题解决什么问题",
  "",
  "讨论何时发回执。关键词：回执",
  "",
  KNOWLEDGE_START,
  "## 当前判断",
  "",
  "- [c0001] 回执只在写入成功后发（依据：[[99 System/KnowledgeInbox/revisions/digests/dig-item-a/r000003.md#^c0007|旧摘要]] · [[01 Sources/_assets/item-a/source-000002/normalized#^s0001|s0001]]） ^c0001",
  "",
  KNOWLEDGE_END,
  "",
  "## 我的实践与补充",
  "",
  "我自己写的经验，不能被迁移改写。",
  "",
].join("\n");
const legacyEvidenceMap = {
  c0001: {
    knowledge_id: "kn-a", knowledge_revision: 3, digest_id: "dig-item-a", digest_revision: 3,
    digest_claim_id: "c0007", item_id: "item-a", source_revision: 2, segment_ids: ["s0001"],
  },
};

ok("旧知识转换：正文直连原文，旧锚点进折叠历史区，人工区与未知字段不动", () => {
  const out = migrateLegacyKnowledgeNote({
    kbId: "kn-a", revision: 3, text: legacyNote, evidenceMap: legacyEvidenceMap,
    sourcesFolder: "01 Sources", systemFolder: "99 System",
  });
  assert.equal(out.errors.length, 0);
  const body = out.text!;
  const managed = extractPartition(body, KNOWLEDGE_START, KNOWLEDGE_END)!;
  assert.ok(managed.includes("[[01 Sources/_assets/item-a/source-000002/normalized#^s0001|查看原文]]"));
  assert.ok(!managed.includes("[c0001]"), "正文不再显示观点编号");
  assert.ok(!managed.includes("r000003.md"), "管理区不再两跳经过 Digest 快照");
  const history = extractPartition(body, KNOWLEDGE_HISTORY_START, KNOWLEDGE_HISTORY_END)!;
  assert.ok(history.includes("主题 rev 3 · c0001"), "历史区标注所属历史版本");
  assert.ok(history.includes("#^c0007"), "旧快照锚点保留可打开");
  assert.ok(body.includes("我自己写的经验，不能被迁移改写。"), "人工区保留");
  assert.ok(body.includes('custom: "用户字段"'), "未知 frontmatter 保留");
  assert.ok(body.includes("讨论何时发回执。"));
});

ok("正文与旧映射对不上时不改写，列入迁移待处理", () => {
  const unmatched = legacyNote.replace("- [c0001]", "- [c0009]");
  const out = migrateLegacyKnowledgeNote({
    kbId: "kn-a", revision: 3, text: unmatched, evidenceMap: legacyEvidenceMap,
    sourcesFolder: "01 Sources", systemFolder: "99 System",
  });
  assert.equal(out.text, null);
  assert.ok(out.errors[0].includes("c0009"));
  const report = renderMigrationReport({
    migrated: [], pending: [{ kb_id: "kn-a", path: "03 Knowledge/投递与回执.md", reason: out.errors[0] }],
    unchanged: 0, conflicts: [],
  });
  assert.ok(report.includes("迁移待处理") && report.includes("03 Knowledge/投递与回执.md"));
});

// ---- 文件重命名迁移（docs/23 §8.3）----
ok("改名映射：可读目标名、同名冲突、无法确定的保持原样", () => {
  const { plan, blocked } = planRenames([
    { kb_id: "src-a", path: "01 Sources/2026/09/2026-09-21 甲--item-a.md", kind: "source", title: "甲", captured_at: "2026-09-21T00:00:00Z" },
    { kb_id: "src-b", path: "01 Sources/2026/09/2026-09-21 甲--item-b.md", kind: "source", title: "甲", captured_at: "2026-09-21T00:00:00Z" },
    { kb_id: "dig-a", path: "02 Digests/2026/09/2026-09-21 甲--item-a.md", kind: "digest", title: "甲", captured_at: null },
    { kb_id: "src-c", path: "01 Sources/2026/09/无日期--item-c.md", kind: "source", title: "丙", captured_at: null },
  ], { sources: "01 Sources", digests: "02 Digests", knowledge: "03 Knowledge" });
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

// ---- 提示词契约：不再有逐观点晋升与证据回填（docs/23 §2、§6.1）----
ok("目标选择最小响应：T1 / keep_digest / 新主题；未知 ID 不猜", () => {
  const ctx = { candidateIds: ["T1", "T2"] };
  const hit = validateTargetSelection({ target: "T1", reason: "补充适用条件" }, ctx);
  assert.equal(hit.errors.length, 0);
  assert.equal(hit.selection?.target, "T1");
  assert.equal(validateTargetSelection({ target: "keep_digest" }, ctx).selection?.keepDigest, true);
  const fresh = validateTargetSelection({ target: null, new_topic: { name: "投递回执", scope: "边界" }, reason: "无匹配" }, ctx);
  assert.equal(fresh.selection?.newTopic?.name, "投递回执");
  assert.ok(validateTargetSelection({ target: "T9", reason: "x" }, ctx).errors.length === 1);
  assert.ok(validateTargetSelection({ target: null, reason: "x" }, ctx).errors.length === 1);
  assert.ok(validateTargetSelection({ target: null, new_topic: { name: "x".repeat(90), scope: "" }, reason: "" }, ctx).errors.length === 1);
  const prompt = buildTargetSelectionPrompt({
    title: "标题", summary: "摘要", points: ["要点"], candidates: [{ id: "T1", title: "主题", scope: "范围" }],
  });
  assert.ok(prompt.includes('"target"') && !prompt.includes("dimensions"));
  assert.ok(TARGET_SYSTEM_PROMPT.includes("keep_digest"));
});
ok("融合提示词只要主体 + no_op/change_summary/conflicts，不回填哈希与证据映射", () => {
  const prompt = buildFusionPrompt({
    knowledge: { title: "主题", scope: "范围" },
    currentBody: "正文",
    existingMaterial: [{ ref: "R1", source: "已有依据", text: "原文一" }],
    newMaterial: { title: "新材料", summary: "摘", sections: [{ heading: "h", blocks: [{ kind: "claim", text: "t", refs: ["R2"] }] }], material: [{ ref: "R2", source: "新", text: "原文二" }] },
    lockedRegions: ["## 我的实践与补充"],
    userInstruction: null,
  });
  for (const banned of ["evidence_map", "added_claims", "retired_claims", "base_hash", "claim_id", "schema_version", "c0001"]) {
    assert.ok(!prompt.includes(banned), `融合输入不应再要求 ${banned}`);
  }
  assert.ok(prompt.includes("R1") && prompt.includes("no_op") && prompt.includes("change_summary"));
  const okOut = validateFusionOutput({ no_op: false, change_summary: "加了条件", sections: [{ heading: "h", blocks: [] }], conflicts: [{ topic: "适用条件", description: "两份不同" }] });
  assert.equal(okOut.errors.length, 0);
  assert.equal(okOut.conflicts[0].topic, "适用条件");
  assert.equal(validateFusionOutput({ no_op: true }).noOp, true, "no_op 时主体可缺省");
  assert.ok(validateFusionOutput({ no_op: false, change_summary: "" }).errors.length >= 1);
  assert.ok(FUSION_SYSTEM_PROMPT.includes("suggestion"));
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
    kind === "knowledge" ? "kb_revision: 1" : "",
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
  ctx: { fs: BundleFs; receipts: string[]; written: string[] },
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
    onDigestWritten: async (itemId, path) => { ctx.written.push(`${itemId}@${path}`); },
  });
  await engine.runOnce("test");
  return { state, status };
}

ok("闸门：未知 format_version 不落盘、不发回执，只暂停该项并提示升级", async () => {
  const fs = new BundleFs();
  const receipts: string[] = [];
  const m = bundleManifest("item-x");
  m.processing = { ...m.processing, format_version: "4.0" } as KbManifest["processing"];
  const { state, status } = await engineHarness(m, { fs, receipts, written: [] });
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
  await engineHarness(m, { fs, receipts, written: [] });
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
  await engineHarness(bundleManifest("item-fx"), { fs, receipts, written: [] },
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

  await engineHarness(bundleManifest("item-fx"), { fs, receipts, written: [] },
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
  const written: string[] = [];
  await engineHarness(bundleManifest("item-x"), { fs, receipts, written });
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
  assert.equal(written.length, 1);
  const commit = await new CommitStore(fs as never, "99 System/KnowledgeInbox/commits").latestForItem("item-x");
  assert.equal(commit?.ack_sent, true);
  assert.equal(commit?.layout_version, LAYOUT_VERSION);
  assert.ok(await fs.exists("99 System/KnowledgeInbox/commits/item-x--000003.json"), "提交标记等内部文件仍可含 item_id");
});

ok("同标题新条目按（2）让路；同版本重复事件不重复建笔记、不产生第二份", async () => {
  const fs = new BundleFs();
  const receipts: string[] = [];
  const written: string[] = [];
  await engineHarness(bundleManifest("item-x"), { fs, receipts, written });
  const before = await fs.read("02 Digests/2026/09/2026-09-21 文章标题.md");
  await engineHarness(bundleManifest("item-x"), { fs, receipts, written });
  assert.equal(await fs.read("02 Digests/2026/09/2026-09-21 文章标题.md"), before, "同版本重复事件不重写笔记");
  await engineHarness(bundleManifest("item-y"), { fs, receipts, written });
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
  await engineHarness(bundleManifest("item-x"), { fs, receipts, written: [] });
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
  await engineHarness(next, { fs, receipts, written: [] });
  const after = await fs.read(digestPath);
  assert.ok(after.includes("我自己改写过这一段。"), "用户编辑不被覆盖");
  const conflicts = [...fs.files.keys()].filter((k) => k.includes("/conflicts/"));
  assert.equal(conflicts.length, 1);
  assert.ok(conflicts[0].includes("item-x--000004--digest.md"), "冲突文件名可用 item_id（内部文件）");
});

ok("云端更新不改本地整理结论：保留 kb_organize，旧 kb_promotion 被清除", async () => {
  const fs = new BundleFs();
  await engineHarness(bundleManifest("item-x"), { fs, receipts: [], written: [] });
  const digestPath = "02 Digests/2026/09/2026-09-21 文章标题.md";
  // 本地整理已给出候选，同时留一份旧版插件写过的逐观点晋升字段（应被新的合并清掉）
  await fs.write(digestPath, (await fs.read(digestPath))
    .replace("kb_organize: not_evaluated", "kb_organize: candidate\nkb_promotion: promoted"));
  const next = bundleManifest("item-x");
  next.bundle_revision = 4;
  await engineHarness(next, { fs, receipts: [], written: [] });
  const after = await fs.read(digestPath);
  assert.ok(after.includes("kb_organize: candidate"), "云端更新不得回退本地整理结论");
  assert.ok(!after.includes("kb_promotion"), "晋升账本字段已退出写入路径");
  assert.ok(after.includes("status/candidate"), "status/* 跟着 kb_organize");
});

// ---- 主题候选：目标选择 → 融合 → 程序差异 → 采纳/部分采纳/回滚（docs/23 §6）----
function topicNote(body: string, revision = 2): string {
  return [
    "---",
    'kb_id: "kn-content-intake"',
    "kb_type: knowledge",
    `kb_revision: ${revision}`,
    'custom_field: "用户字段"',
    'aliases: ["接收与提炼"]',
    "---",
    "",
    "# 内容接收与提炼",
    "",
    "## 这个主题解决什么问题",
    "",
    "讨论材料怎么被可靠接收、提炼，以及何时晋升为长期知识。关键词：采集, 总结, 提炼, 来源版本",
    "",
    KNOWLEDGE_START,
    body,
    KNOWLEDGE_END,
    "",
    "## 我的实践与补充",
    "",
    "我自己写的经验，自动融合不改写。",
    "",
  ].join("\n");
}

async function seedDigest(fs: MemFs | BundleFs, itemId: string, sourceRevision = 2) {
  const doc = digestDoc(itemId);
  const assets = sourceAssetsDir("01 Sources", itemId, sourceRevision);
  await fs.write(`${assets}/content.json`, JSON.stringify(doc));
  await fs.write(`${assets}/segments.json`, JSON.stringify({ segments: [{ segment_id: "s0001", text: SEGMENT_TEXT }] }));
  await fs.write(`${assets}/normalized.md`, `${SEGMENT_TEXT} ^s0001\n`);
  const note = renderDigestNote(bundleManifest(itemId), {
    sourceLink: "[[01 Sources/x|原始资料]]",
    cloudMd: renderContentMarkdown(parseContentDocument(doc).document!, {}),
    status: "ready",
    contentLines: ["kb_content_revision: 3", `kb_format_version: "${CONTENT_FORMAT_VERSION}"`],
  });
  await fs.write(`02 Digests/2026/09/21 ${itemId}.md`, note);
  return `02 Digests/2026/09/21 ${itemId}.md`;
}

function organizeWith(fs: MemFs | BundleFs, replies: string[], calls: string[] = []) {
  let i = 0;
  return {
    calls,
    service: new OrganizeService({
      fs: fs as never,
      settings: () => DEFAULT_SETTINGS,
      model: async () => ({
        configRef: "p1@v2/cred5",
        caller: {
          call: async (system: string) => {
            calls.push(system.slice(0, 8));
            return { outputText: JSON.stringify(replies[i++] ?? {}), finishReason: "stop" };
          },
        },
      }),
      log: () => undefined,
    }),
  };
}

const FUSION_OK = {
  title: "内容接收与提炼",
  summary: "先固定上下文，再区分来源与推断。",
  sections: [{
    heading: "当前判断",
    blocks: [
      { kind: "claim", text: "采集与总结分开处理。", refs: ["R1"] },
      { kind: "quote", text: SEGMENT_TEXT, refs: ["R1"] },
      { kind: "suggestion", text: "可以给失败原因分开计数。", refs: [] },
    ],
  }],
  limitations: [],
  no_op: false,
  change_summary: "增加适用条件，保留尚未解决的分歧。",
  conflicts: [{ topic: "适用条件", description: "两份材料的说法不同。" }],
};

ok("旧候选只归档不采纳；身份从 frontmatter 读取", async () => {
  const fs = new MemFs();
  await fs.write("99 System/KnowledgeInbox/proposals/prop-old.json", JSON.stringify({
    proposal_id: "prop-old", task_id: "task-x", knowledge_id: "kn-a", knowledge_title: "旧主题",
    base_hash: "h", proposed_managed_body: "x", added_claims: [], updated_claims: [], retired_claims: [],
    evidence_map: {}, conflicts: [], change_summary: "旧流程", promotion_decisions: [], state: "ready",
    created_at: "2026-09-20T00:00:00Z", applied_at: null, applied_knowledge_revision: null, no_op: false,
  }));
  const { service } = organizeWith(fs, []);
  assert.equal(looksLegacyProposal(JSON.parse(await fs.read("99 System/KnowledgeInbox/proposals/prop-old.json"))), true);
  assert.equal((await service.listProposals()).length, 0);
  assert.equal((await service.listLegacyProposals())[0].knowledge_title, "旧主题");
  const res = await service.accept("prop-old");
  assert.equal(res.applied, false);
  assert.ok(res.note.includes("旧版"));
  const digestPath = await seedDigest(fs, "item-x");
  assert.equal(itemIdOfDigestNote(await fs.read(digestPath)), "item-x");
});

ok("keep_digest：无增量时不生成候选，只写本地整理区", async () => {
  const fs = new MemFs();
  const digestPath = await seedDigest(fs, "item-x");
  const { service } = organizeWith(fs, [{ target: "keep_digest", reason: "只是重复已有结论" }]);
  await service.enqueue("item-x", digestPath);
  const result = await service.runBatch();
  assert.equal(result.keptDigest, 1);
  assert.equal(result.prepared, 0);
  assert.equal((await service.listProposals()).length, 0);
  assert.ok((await fs.read(digestPath)).includes("结论保留在本 Digest"));
  assert.ok((await fs.read(digestPath)).includes("kb_organize: keep_digest"));
});

ok("主题候选：目标选择 + 融合 + 程序差异（删除会显式出现在差异里）", async () => {
  const fs = new MemFs();
  const digestPath = await seedDigest(fs, "item-x");
  await fs.write("03 Knowledge/内容接收与提炼.md", topicNote("## 当前判断\n\n- 这条会被模型漏掉。\n- 这条也会被漏掉。"));
  const { service, calls } = organizeWith(fs, [{ target: "T1", reason: "为现有主题补充依据" }, FUSION_OK]);
  await service.enqueue("item-x", digestPath);
  const result = await service.runBatch();
  assert.equal(result.prepared, 1, result.messages.join("；"));
  assert.deepEqual(calls, ["你在帮用户把一篇", "你在维护用户本地"]);
  const [proposal] = await service.listProposals();
  assert.equal(proposal.knowledge_id, "kn-content-intake");
  assert.equal(proposal.no_op, false);
  assert.equal(proposal.base_revision, 2);
  assert.ok(proposal.base_hash.length === 64, "基线哈希由程序持有");
  assert.ok(proposal.candidate_document!.references.e1.item_id === "item-x", "引用直连原文");
  assert.ok(!JSON.stringify(proposal).includes("evidence_map") && !JSON.stringify(proposal).includes("promotion"));
  const diff = renderDiff(proposal.baseline_body, renderContentMarkdown(proposal.candidate_document!, { hideCitations: true }));
  // 模型没有复现旧正文时，差异必须显式出现删除行（`- ` 前缀 + 原来的 claim 项目符号）
  assert.ok(/^- - 这条会被模型漏掉。$/m.test(diff), `差异要显示删除：\n${diff}`);
  assert.ok(/^- - 这条也会被漏掉。$/m.test(diff), "两条旧结论都没被复现");
  assert.ok(/^\+ - 采集与总结分开处理。$/m.test(diff), `差异要显示新增：\n${diff}`);
  // 采纳前不写 Knowledge
  const before = await fs.read("03 Knowledge/内容接收与提炼.md");
  assert.ok(before.includes("这条会被模型漏掉。"));
  // 整篇采纳
  const applied = await service.accept(proposal.proposal_id);
  assert.equal(applied.applied, true, applied.note);
  const note = await fs.read("03 Knowledge/内容接收与提炼.md");
  const managed = extractPartition(note, KNOWLEDGE_START, KNOWLEDGE_END)!;
  assert.ok(managed.includes("采集与总结分开处理。"));
  assert.ok(managed.includes("[[01 Sources/_assets/item-x/source-000002/normalized#^s0001|查看原文]]"));
  assert.ok(managed.includes("## 依据与原文"));
  assert.ok(note.includes("我自己写的经验，自动融合不改写。"), "人工区保留");
  assert.ok(note.includes('custom_field: "用户字段"'), "未知 frontmatter 保留");
  assert.ok(note.includes(`aliases: ["接收与提炼"]`), "aliases 保留");
  assert.equal(readFrontmatterValue(note, "kb_revision"), "3");
  assert.ok(!/\[c\d{4}\]/.test(managed), "新正文不再产生观点编号");
  assert.ok(await fs.exists("99 System/KnowledgeInbox/revisions/knowledge/kn-content-intake/r000002.md"), "先存历史正文");
  assert.ok(await fs.exists("99 System/KnowledgeInbox/revisions/knowledge/kn-content-intake/refs-r000003.json"), "引用表与正文成对保存");
  // 幂等：同一候选不重复应用
  const again = await service.accept(proposal.proposal_id);
  assert.equal(again.applied, false);
  assert.ok(again.note.includes("幂等"));
});

ok("部分采纳：按块勾选后正文与引用一致，未用引用被清除", async () => {
  const fs = new MemFs();
  const digestPath = await seedDigest(fs, "item-x");
  await fs.write("03 Knowledge/内容接收与提炼.md", topicNote("## 当前判断\n\n- 保留旧结论。"));
  const { service } = organizeWith(fs, [{ target: "T1", reason: "补充建议" }, FUSION_OK]);
  await service.enqueue("item-x", digestPath);
  await service.runBatch();
  const [proposal] = await service.listProposals();
  const keep = proposal.candidate_document!.sections.map((s) => s.blocks.map(() => false));
  keep[0][2] = true; // 只采纳那条无引用的 AI 建议
  const res = await service.accept(proposal.proposal_id, { keep });
  assert.equal(res.applied, true, res.note);
  const managed = extractPartition(await fs.read("03 Knowledge/内容接收与提炼.md"), KNOWLEDGE_START, KNOWLEDGE_END)!;
  assert.ok(managed.includes("可以给失败原因分开计数。"));
  assert.ok(!managed.includes("采集与总结分开处理。"), "未勾选的块不写入");
  assert.ok(!managed.includes("## 依据与原文"), "未使用的引用被清除");
  const record = await new TopicReferenceStore(fs as never, "99 System").read("kn-content-intake", 3);
  assert.deepEqual(Object.keys(record!.references), [], "引用表与正文一致");
});

ok("基线被人工改过时拒绝覆盖；引用原文缺失时暂缓采纳", async () => {
  const fs = new MemFs();
  const digestPath = await seedDigest(fs, "item-x");
  await fs.write("03 Knowledge/内容接收与提炼.md", topicNote("## 当前判断\n\n- 旧结论。"));
  const { service } = organizeWith(fs, [{ target: "T1", reason: "补充" }, FUSION_OK]);
  await service.enqueue("item-x", digestPath);
  await service.runBatch();
  const [proposal] = await service.listProposals();
  // 用户（或别的设备）在候选生成后改了正文
  await fs.write("03 Knowledge/内容接收与提炼.md",
    (await fs.read("03 Knowledge/内容接收与提炼.md")).replace("- 旧结论。", "- 我改过这一条。"));
  const stale = await service.accept(proposal.proposal_id);
  assert.equal(stale.applied, false);
  assert.ok(stale.note.includes("过期"));
  assert.ok((await fs.read("03 Knowledge/内容接收与提炼.md")).includes("我改过这一条。"));
  assert.equal((await service.listProposals())[0].state, "stale");
  // 取消原片段索引后再采纳：引用缺失即暂缓，不用标题补位
  const fs2 = new MemFs();
  const p2 = await seedDigest(fs2, "item-x");
  await fs2.write("03 Knowledge/内容接收与提炼.md", topicNote("## 当前判断\n\n- 旧结论。"));
  const second = organizeWith(fs2, [{ target: "T1", reason: "补充" }, FUSION_OK]);
  await second.service.enqueue("item-x", p2);
  await second.service.runBatch();
  const [cand] = await second.service.listProposals();
  await fs2.remove("01 Sources/_assets/item-x/source-000002/segments.json");
  // 快照读取按「来源版本不可变」在实例内缓存；删盘后要由新实例（等价重启）重新校验
  const restart = organizeWith(fs2, []);
  const blocked = await restart.service.accept(cand.proposal_id);
  assert.equal(blocked.applied, false);
  // 快照缺失时先被摘录逐字校验挡住（引用表哈希校验是同一条防线的第二关）
  assert.ok(/(摘录校验|原文快照校验)未通过/.test(blocked.note), blocked.note);
});

ok("no_op 候选不写入任何内容；重启恢复不重复应用", async () => {
  const fs = new MemFs();
  const digestPath = await seedDigest(fs, "item-x");
  await fs.write("03 Knowledge/内容接收与提炼.md", topicNote("## 当前判断\n\n- 旧结论。"));
  const { service } = organizeWith(fs, [{ target: "T1", reason: "有主题" }, { no_op: true, change_summary: "" }]);
  await service.enqueue("item-x", digestPath);
  const result = await service.runBatch();
  assert.equal(result.keptDigest, 1);
  const [proposal] = await service.listProposals();
  assert.equal(proposal.no_op, true);
  assert.equal(proposal.candidate_document, null);
  const before = await fs.read("03 Knowledge/内容接收与提炼.md");
  const res = await service.accept(proposal.proposal_id);
  assert.equal(res.applied, false);
  assert.ok(res.note.includes("无需修改"));
  assert.equal(await fs.read("03 Knowledge/内容接收与提炼.md"), before, "no_op 不写盘");
  // 恢复检查：提交标记已存在时不得重复应用（正文已是候选结果的场景）
  const fs3 = new MemFs();
  const p3 = await seedDigest(fs3, "item-x");
  await fs3.write("03 Knowledge/内容接收与提炼.md", topicNote("## 当前判断\n\n- 旧结论。"));
  const third = organizeWith(fs3, [{ target: "T1", reason: "补充" }, FUSION_OK]);
  await third.service.enqueue("item-x", p3);
  await third.service.runBatch();
  const [cand] = await third.service.listProposals();
  const applied = await third.service.accept(cand.proposal_id);
  assert.equal(applied.applied, true);
  const writtenNote = await fs3.read("03 Knowledge/内容接收与提炼.md");
  const recoveryPath = "99 System/KnowledgeInbox/revisions/knowledge/kn-content-intake/recovery-" + cand.proposal_id + ".json";
  assert.ok(JSON.parse(await fs3.read(recoveryPath)).state === "committed");
  // 模拟状态文件回滚到「待采纳」：恢复检查应识别已提交，不再写第二版
  const raw = JSON.parse(await fs3.read(`99 System/KnowledgeInbox/proposals/${cand.proposal_id}.json`));
  raw.state = "ready";
  raw.applied_at = null;
  await fs3.write(`99 System/KnowledgeInbox/proposals/${cand.proposal_id}.json`, JSON.stringify(raw));
  const retry = await third.service.accept(cand.proposal_id);
  assert.equal(retry.applied, false);
  assert.ok(retry.note.includes("未重复写入"), retry.note);
  assert.equal(await fs3.read("03 Knowledge/内容接收与提炼.md"), writtenNote);
  assert.equal(readFrontmatterValue(writtenNote, "kb_revision"), "3", "版本没有因重复应用而推进");
});

ok("回滚同时恢复正文与引用表", async () => {
  const fs = new MemFs();
  const digestPath = await seedDigest(fs, "item-x");
  await fs.write("03 Knowledge/内容接收与提炼.md", topicNote("## 当前判断\n\n- 旧结论。"));
  const { service } = organizeWith(fs, [{ target: "T1", reason: "补充" }, FUSION_OK]);
  await service.enqueue("item-x", digestPath);
  await service.runBatch();
  const [proposal] = await service.listProposals();
  await service.accept(proposal.proposal_id);
  const rolled = await service.rollback(proposal.proposal_id);
  assert.equal(rolled.rolledBack, true, rolled.note);
  const note = await fs.read("03 Knowledge/内容接收与提炼.md");
  assert.ok(note.includes("- 旧结论。"), "正文回到上一版");
  assert.ok(!note.includes("## 依据与原文"));
  assert.equal(readFrontmatterValue(note, "kb_revision"), "2");
  const refs = new TopicReferenceStore(fs as never, "99 System");
  assert.equal(await refs.read("kn-content-intake", 3), null, "本版引用表一并撤销");
  // 应用后又被人工改过时不覆盖，先给差异
  const again = await service.accept(proposal.proposal_id);
  assert.equal(again.applied, true, again.note);
  await fs.write("03 Knowledge/内容接收与提炼.md",
    (await fs.read("03 Knowledge/内容接收与提炼.md")).replace("## 当前判断", "## 当前判断\n\n- 我又加了一条。"));
  const blocked = await service.rollback(proposal.proposal_id);
  assert.equal(blocked.rolledBack, false);
  assert.ok(blocked.note.includes("又被修改"));
  assert.ok(blocked.diff!.includes("- 我又加了一条。") || blocked.diff!.includes("+ 我又加了一条。"));
});

ok("融合输出全被丢弃时如实失败，不落候选", async () => {
  const fs = new MemFs();
  const digestPath = await seedDigest(fs, "item-x");
  await fs.write("03 Knowledge/内容接收与提炼.md", topicNote("## 当前判断\n\n- 旧结论。"));
  const { service } = organizeWith(fs, [{ target: "T1", reason: "补充" }, {
    title: "x", summary: "摘", sections: [{ heading: "h", blocks: [{ kind: "claim", text: "引用不存在", refs: ["R99"] }] }],
    limitations: [], no_op: false, change_summary: "坏候选",
  }]);
  await service.enqueue("item-x", digestPath);
  const result = await service.runBatch();
  assert.equal(result.failed, 1);
  assert.equal(result.prepared, 0);
  assert.equal((await service.listProposals()).length, 0);
  assert.ok(result.messages.join("；").includes("没有可用内容"));
  const task = (await service.listPending())[0];
  assert.equal(task.state, "failed");
  assert.ok(task.last_error!.includes("没有可用内容"));
});

// ---- 本地迁移端到端（docs/23 §8.2、§8.3）----
ok("旧知识迁移：展开 evidence_map、留历史、写迁移待处理报告", async () => {
  const fs = new MemFs();
  await fs.write("03 Knowledge/投递与回执.md", legacyNote);
  await fs.write("99 System/KnowledgeInbox/revisions/knowledge/kn-a/evidence-map.json", JSON.stringify(legacyEvidenceMap));
  await fs.write("99 System/KnowledgeInbox/revisions/digests/dig-item-a/r000003.md", "# 旧摘要（冻结证据）\n\n原话 ^c0007");
  const out = await runLegacyKnowledgeMigration(fs as never, DEFAULT_SETTINGS);
  assert.equal(out.migrated.length, 1, JSON.stringify(out.pending));
  const note = await fs.read("03 Knowledge/投递与回执.md");
  assert.ok(note.includes("[[01 Sources/_assets/item-a/source-000002/normalized#^s0001|查看原文]]"));
  assert.ok(extractPartition(note, KNOWLEDGE_HISTORY_START, KNOWLEDGE_HISTORY_END)!.includes("主题 rev 3 · c0001"));
  assert.ok(await fs.exists("99 System/KnowledgeInbox/revisions/digests/dig-item-a/r000003.md"), "旧 Digest 快照保持原路径");
  assert.ok(await fs.exists("99 System/KnowledgeInbox/migrations/legacy-kn-a-r3.md"), "转换前正文留档");
  const record = await new TopicReferenceStore(fs as never, "99 System").read("kn-a", 4);
  assert.equal(record!.references.e1.item_id, "item-a");
  assert.equal(record!.references.e1.source_text_hash.length, 64, "哈希按本地固定原文补算");
  const report = await fs.read("99 System/KnowledgeInbox/migrations/迁移待处理.md");
  assert.ok(report.includes("已转换 1 个主题"));
  // 不可靠对应的主题不改写，只报告
  await fs.write("03 Knowledge/别的主题.md", legacyNote.replace("[c0001]", "[c0009]").replace('kn_id: "kn-a"', 'kn_id: "kn-b"'));
  await fs.write("99 System/KnowledgeInbox/revisions/knowledge/kn-b/evidence-map.json", JSON.stringify(legacyEvidenceMap));
  const second = await runLegacyKnowledgeMigration(fs as never, DEFAULT_SETTINGS, {
    entries: [{ kb_id: "kn-b", path: "03 Knowledge/别的主题.md", kind: "knowledge", item_id: null, title: "别的主题", updated_at: "" }],
  });
  assert.equal(second.migrated.length, 0);
  assert.ok(second.pending[0].reason.includes("c0009"));
  assert.ok((await fs.read("03 Knowledge/别的主题.md")).includes("[c0009]"), "待处理主题保持原样，不自动改写");
});

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
