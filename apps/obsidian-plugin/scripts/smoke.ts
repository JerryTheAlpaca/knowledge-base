/**
 * 纯逻辑冒烟测试（docs/08 §2、§3、§5、§6、§7.2；docs/02 §12.3、§13.2）。
 *
 * 覆盖：路径安全（A16）、三层路径与 ID、分区读写与哈希、Source/Digest/Knowledge 模板、
 * 多笔记 commit 记录（失败不标记全部完成）、本地索引匹配、晋升与融合输出校验、
 * 整理任务幂等与基线保护。
 *
 * 运行：esbuild 打包为 .smoke/smoke.cjs 后 node 执行；不依赖 obsidian。
 */
import { strict as assert } from "node:assert";
import {
  assertSafeRelativePath,
  joinUnder,
  sanitizeTitle,
  sourceNotePath,
  digestNotePath,
  knowledgeNotePath,
  sourceAssetsDir,
  bundleDirName,
  digestKbId,
  sourceKbId,
  knowledgeIdFromTitle,
  UnsafePathError,
} from "../src/vault/paths";
import {
  CLOUD_DIGEST_END,
  CLOUD_DIGEST_START,
  KNOWLEDGE_END,
  KNOWLEDGE_START,
  LOCAL_ORGANIZE_END,
  LOCAL_ORGANIZE_START,
  extractKnowledgeScope,
  extractPartition,
  managedTags,
  mergeKbFrontmatter,
  mergeManagedTags,
  partitionHashes,
  renderDigestNote,
  renderKnowledgeNote,
  renderLocalOrganize,
  renderProposalIndex,
  renderSourceNote,
  replacePartition,
  rewriteCloudDigestLinks,
  stripSegmentIds,
} from "../src/vault/template";
import { CommitStore, JsonStore, RevisionStore, Suppression } from "../src/vault/records";
import { KnowledgeIndexStore, matchKnowledge, parseKnowledgeEntry, tokenize } from "../src/knowledge/index";
import {
  validateFusionOutput,
  validatePromotionOutput,
  validateSegmentReferences,
} from "../src/knowledge/prompts";
import {
  aggregatePromotion,
  buildRelationNote,
  extractClaimIds,
  idempotencyKey,
  OrganizeService,
  parseClaimsFromCloudRegion,
  ProposalStore,
} from "../src/knowledge/organize";
import {
  renderCitations,
  renderDigestSnapshot,
  validateEvidenceMap,
} from "../src/knowledge/citations";
import {
  bindingSecretRef,
  generateLocal,
  LocalModelUnavailableError,
  parseModelJson,
  pinConfig,
  resolveLocalModel,
} from "../src/providers/local";
import type { KbManifest, KbSettings, LocalModelConfig, PromotionDecision } from "../src/types";

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
  analysisSchemaVersion: "2.0",
  layoutVersion: 2,
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

// ---- 文件名与三层路径（docs/08 §2）----
ok("清理 Windows 保留字符与尾部点/空格", () => {
  assert.equal(sanitizeTitle('demo<>:"/\\|?*title. '), "demo title");
  assert.equal(sanitizeTitle("CON"), "_CON");
  assert.equal(sanitizeTitle("      "), "未命名");
  assert.equal(sanitizeTitle("很长的标题".repeat(20)).length <= 60, true);
});
ok("Source/Digest 按采集日期分目录，Knowledge 用稳定主题名", () => {
  const at = "2026-09-09T09:20:00+08:00";
  assert.equal(sourceNotePath("01 Sources", at, "Agent实践", "item-demo"),
    "01 Sources/2026/09/2026-09-09 Agent实践--item-demo.md");
  assert.equal(digestNotePath("02 Digests", at, "Agent实践", "item-demo"),
    "02 Digests/2026/09/2026-09-09 Agent实践--item-demo.md");
  assert.equal(knowledgeNotePath("03 Knowledge", "Agent 操作技巧"), "03 Knowledge/Agent 操作技巧.md");
  assert.equal(sourceAssetsDir("01 Sources", "item-demo", 2), "01 Sources/_assets/item-demo/source-000002");
  assert.equal(bundleDirName(3), "bundle-000003");
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
  processing: { state: "ready", recipe_version: "v1", result_file_id: "result-json", source_revision: 2 },
  files: [
    { file_id: "f1", relative_path: "normalized.md", role: "source_material", mime: "text/markdown", bytes: 10, sha256: "aa" },
    { file_id: "f2", relative_path: "original-subtitle.srt", role: "source_material", mime: "text/plain", bytes: 10, sha256: "bb" },
  ],
  missing_materials: [],
  warnings: [],
  expires_at: "2026-10-09T01:23:00Z",
} as unknown as KbManifest;

ok("Source 笔记：无 AI 生成区，含完整性/定位/原件链接/可读原文", () => {
  const text = renderSourceNote(manifest, {
    assetsBase: "01 Sources/_assets/item-demo/source-000002",
    digestLink: "[[02 Digests/2026/09/2026-09-09 Agent实践--item-demo|查看提炼]]",
    status: "ready",
    normalizedText: "第一段 ^s0001\n第二段 ^s0002\n",
  });
  assert.ok(text.includes("kb_type: source"));
  assert.ok(text.includes("kb_id: \"src-item-demo\""));
  assert.ok(text.includes("完整性：已取得该分 P 字幕，不含视频画面。"));
  assert.ok(text.includes("定位：分 P 1 · cid 12345 · BV1xx"));
  assert.ok(text.includes("[[02 Digests/2026/09/2026-09-09 Agent实践--item-demo|查看提炼]]"));
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
    sourceLink: "[[01 Sources/2026/09/2026-09-09 Agent实践--item-demo|原始资料]]",
    cloudMd: "## 一句话总结\n\n演示。",
    status: "ready",
  });
  assert.ok(text.includes("kb_type: digest"));
  assert.ok(text.includes("kb_promotion: not_evaluated"));
  assert.ok(text.includes(CLOUD_DIGEST_START) && text.includes(CLOUD_DIGEST_END));
  assert.ok(text.includes(LOCAL_ORGANIZE_START) && text.includes(LOCAL_ORGANIZE_END));
  assert.ok(text.includes("尚未本地整理。"));
  assert.ok(text.includes("## 我的备注与判断"));
});
ok("Digest 无云端结果时给出诚实占位，不生成看似有效的空摘要", () => {
  const pendingManifest = {
    ...manifest,
    processing: { ...manifest.processing, state: "waiting_key", result_file_id: null },
  } as KbManifest;
  const text = renderDigestNote(pendingManifest, { sourceLink: null, cloudMd: null, status: "waiting_key" });
  assert.ok(text.includes("云端提炼尚未完成（状态：waiting_key）"));
  assert.ok(text.includes("（原始资料尚未入库）"));
});
ok("rewriteCloudDigestLinks 把证据链接改写为本地资产块链接", () => {
  const md = "## 核心观点\n\n- 观点（[[normalized#s0001|s0001]]）\n\n> 采集备注：\n> 备注";
  const out = rewriteCloudDigestLinks(md, "01 Sources/_assets/item-demo/source-000002");
  assert.ok(out.includes("[[01 Sources/_assets/item-demo/source-000002/normalized#^s0001|s0001]]"));
  assert.ok(!out.includes("采集备注"));
});
ok("本地整理区渲染晋升建议与关系说明", () => {
  const decisions: PromotionDecision[] = [{
    claim_id: "c0001", decision: "review", target_knowledge_id: "kn-agent-operations",
    new_topic: null, relation: "adds", reason: "新增可复用步骤",
    evidence_refs: ["s0001"],
    dimensions: {
      novelty: { level: "明显", reason: "新" }, utility: { level: "明显", reason: "可用" },
      credibility: { level: "有依据", reason: "有原文" }, reusability: { level: "长期", reason: "跨项目" },
      increment: { level: "新增", reason: "补充步骤" },
    },
  }];
  const text = renderLocalOrganize(decisions, "有 1 条观点具备长期增量。");
  assert.ok(text.includes("有 1 条观点具备长期增量。"));
  assert.ok(text.includes("`c0001` 建议晋升"));
  assert.ok(text.includes("新增可复用步骤"));
});
ok("模型给出的主题 ID 只有解析到真实笔记才渲染为链接", () => {
  const decisions: PromotionDecision[] = [{
    claim_id: "c0001", decision: "review", target_knowledge_id: "kn-agent-operations",
    new_topic: null, relation: "adds", reason: "新增", evidence_refs: [],
    dimensions: {
      novelty: { level: "明显", reason: "x" }, utility: { level: "明显", reason: "x" },
      credibility: { level: "有依据", reason: "x" }, reusability: { level: "长期", reason: "x" },
      increment: { level: "新增", reason: "x" },
    },
  }];
  const resolved = renderLocalOrganize(decisions, null,
    (kbId) => (kbId === "kn-agent-operations" ? "[[03 Knowledge/Agent 操作技巧|Agent 操作技巧]]" : null));
  assert.ok(resolved.includes("[[03 Knowledge/Agent 操作技巧|Agent 操作技巧]]"));
  const unresolved = renderLocalOrganize(decisions, null, () => null);
  assert.ok(!unresolved.includes("[["));
  assert.ok(unresolved.includes("未解析到笔记"));
});
ok("Knowledge 笔记：范围说明 + 机器区 + 人工区，aliases 保留", () => {
  const text = renderKnowledgeNote({
    kbId: "kn-agent-operations", title: "Agent 操作技巧", aliases: ["Agent操作技巧"],
    scope: "讨论可复用的 Agent 操作手法。关键词：提示词, 上下文",
    managedBody: "## 当前判断\n\n- [c0001] 先固定上下文。",
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
    proposalId: "p1", title: "Agent 操作技巧", knowledgeTitle: "Agent 操作技巧",
    state: "ready", changeSummary: "新增 1 条", createdAt: "2026-09-09T10:00:00Z",
  }]);
  assert.ok(withRows.includes("| Agent 操作技巧 | 新增 1 条 | ready |"));
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
  // 块状写法
  const block = ["---", "tags:", "  - 用户标签", "  - type/digest", "---", "", "# x"].join("\n");
  const mergedBlock = mergeManagedTags(block, managedTags("knowledge", null));
  assert.ok(mergedBlock.includes("type/knowledge"));
  assert.ok(mergedBlock.includes("用户标签"));
  assert.ok(!mergedBlock.includes("type/digest"));
  // 无 tags 时插入
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
  assert.equal(rec?.layout_version, 2);
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

// ---- 晋升输出校验（docs/08 §4）----
const goodDecision = {
  claim_id: "c0001", decision: "review", target_knowledge_id: "kn-agent-operations",
  new_topic: null, relation: "adds", reason: "新增可复用步骤", evidence_refs: ["s0001"],
  dimensions: {
    novelty: { level: "明显", reason: "新" }, utility: { level: "明显", reason: "可用" },
    credibility: { level: "有依据", reason: "原文有" }, reusability: { level: "长期", reason: "跨项目" },
    increment: { level: "新增", reason: "补步骤" },
  },
};
const promotionCtx = {
  claimIds: ["c0001"], candidateIds: ["kn-agent-operations"], segmentIds: ["s0001"],
};
ok("合法晋升输出通过校验", () => {
  const res = validatePromotionOutput({ decisions: [goodDecision] }, promotionCtx);
  assert.deepEqual(res.errors, []);
  assert.equal(res.decisions[0].decision, "review");
  assert.equal(res.decisions[0].dimensions.increment.level, "新增");
});
ok("拒绝输入中不存在的 claim_id 与片段引用", () => {
  const bad = { ...goodDecision, claim_id: "c9999", evidence_refs: ["s9999"] };
  const res = validatePromotionOutput({ decisions: [bad] }, promotionCtx);
  assert.ok(res.errors.some((e) => e.includes("claim_id")));
});
ok("拒绝非法等级词与空理由", () => {
  const bad = JSON.parse(JSON.stringify(goodDecision));
  bad.dimensions.novelty.level = "很高";
  bad.dimensions.utility.reason = "";
  const res = validatePromotionOutput({ decisions: [bad] }, promotionCtx);
  assert.ok(res.errors.some((e) => e.includes("novelty.level")));
  assert.ok(res.errors.some((e) => e.includes("utility.reason")));
});
ok("声明「已交叉核验」必须列出实际独立证据", () => {
  const bad = JSON.parse(JSON.stringify(goodDecision));
  bad.dimensions.credibility.level = "已交叉核验";
  bad.evidence_refs = [];
  const res = validatePromotionOutput({ decisions: [bad] }, promotionCtx);
  assert.ok(res.errors.some((e) => e.includes("已交叉核验")));
});
ok("review 必须给出目标主题或新建建议", () => {
  const bad = JSON.parse(JSON.stringify(goodDecision));
  bad.target_knowledge_id = null;
  bad.new_topic = null;
  const res = validatePromotionOutput({ decisions: [bad] }, promotionCtx);
  assert.ok(res.errors.some((e) => e.includes("target_knowledge_id 或 new_topic")));
});
ok("target_knowledge_id 必须是候选主题之一", () => {
  const bad = JSON.parse(JSON.stringify(goodDecision));
  bad.target_knowledge_id = "kn-unknown";
  const res = validatePromotionOutput({ decisions: [bad] }, promotionCtx);
  assert.ok(res.errors.some((e) => e.includes("候选主题")));
});

// ---- 融合输出校验（docs/08 §6.1、§7.1）----
const fusionCtx = {
  baseHash: "h1",
  allowedClaimRefs: ["dig-item-demo#c0001"],
  allowedSegmentIds: ["s0001"],
  existingClaimIds: ["c0002"],
};
function goodFusion() {
  return {
    base_hash: "h1",
    no_op: false,
    proposed_managed_body: "## 当前判断\n\n- [c0001] 先固定上下文（依据 s0001）。",
    added_claims: [{ claim_id: "c0001", text: "先固定上下文", evidence_refs: ["dig-item-demo#c0001"], segment_ids: ["s0001"] }],
    updated_claims: [{ claim_id: "c0002", text: "旧观点更新", change: "补适用条件", evidence_refs: [], segment_ids: [] }],
    retired_claims: [],
    evidence_map: {
      c0001: {
        knowledge_id: "kn-agent-operations", knowledge_revision: 1,
        digest_id: "dig-item-demo", digest_revision: 3,
        digest_claim_id: "c0001", item_id: "item-demo", source_revision: 2, segment_ids: ["s0001"],
      },
    },
    conflicts: [],
    change_summary: "新增 1 条观点",
    promotion_decisions: [],
  };
}
ok("合法融合输出通过校验（证据链两跳完整）", () => {
  const res = validateFusionOutput(goodFusion(), fusionCtx);
  assert.deepEqual(res.errors, []);
});
ok("基线哈希不一致时拒绝（主题可能已被修改）", () => {
  const bad = goodFusion();
  bad.base_hash = "h2";
  const res = validateFusionOutput(bad, fusionCtx);
  assert.ok(res.errors.some((e) => e.includes("base_hash")));
});
ok("证据链必须完整：缺少 source_revision 即报错", () => {
  const bad = goodFusion();
  delete (bad.evidence_map.c0001 as Record<string, unknown>).source_revision;
  const res = validateFusionOutput(bad, fusionCtx);
  assert.ok(res.errors.some((e) => e.includes("source_revision")));
});
ok("引用未输入的来源或片段被拒绝", () => {
  const bad = goodFusion();
  bad.added_claims[0].evidence_refs = ["dig-other#c0009"];
  bad.added_claims[0].segment_ids = ["s9999"];
  const res = validateFusionOutput(bad, fusionCtx);
  assert.ok(res.errors.some((e) => e.includes("未知来源")));
  assert.ok(res.errors.some((e) => e.includes("不存在的片段")));
});
ok("退休观点必须说明被哪条观点取代（不能无声删除）", () => {
  const bad = goodFusion();
  bad.retired_claims = [{ claim_id: "c0003" }];
  const res = validateFusionOutput(bad, fusionCtx);
  assert.ok(res.errors.some((e) => e.includes("被哪条观点取代")));
});
ok("「更新」只能针对当前正文已有观点（ID 沿用，不整体重编号）", () => {
  const bad = goodFusion();
  bad.updated_claims = [{ claim_id: "c9999", text: "新", change: "x" }];
  const res = validateFusionOutput(bad, fusionCtx);
  assert.ok(res.errors.some((e) => e.includes("不能标记为「更新」")));
});
ok("正文引用不存在的片段被拒绝", () => {
  assert.deepEqual(validateSegmentReferences("依据 s0001", ["s0001"]), []);
  assert.ok(validateSegmentReferences("依据 s9999", ["s0001"]).length > 0);
});
ok("从受管理正文提取已有 claim_id", () => {
  assert.deepEqual(extractClaimIds("- [c0001] A\n- [c0002] B\n- [c0001] C"), ["c0001", "c0002"]);
});

// ---- 证据链两跳（docs/08 §6.1）----
ok("冻结快照含块 ID 与原文片段链接", () => {
  const md = renderDigestSnapshot({
    digestId: "dig-item-demo", digestRevision: 3, sourceRevision: 2, itemId: "item-demo",
    title: "Agent实践", summary: "演示。",
    claims: [{ claim_id: "c0001", text: "先固定上下文", conditions: "长任务", evidence_ids: ["s0001"] }],
    sourcesFolder: "01 Sources", createdAt: "2026-09-09T00:00:00Z",
  });
  assert.ok(md.includes("^c0001"), "快照观点必须带块 ID");
  assert.ok(md.includes("[[01 Sources/_assets/item-demo/source-000002/normalized#^s0001|s0001]]"));
  assert.ok(md.includes("kb_type: digest-snapshot"));
});
ok("正文引用由本地解析器生成，含两跳路径", () => {
  const body = "## 当前判断\n\n- [c0001] 先固定上下文。";
  const out = renderCitations(body, {
    c0001: {
      knowledge_id: "kn-agent-operations", knowledge_revision: 2,
      digest_id: "dig-item-demo", digest_revision: 3, digest_claim_id: "c0001",
      item_id: "item-demo", source_revision: 2, segment_ids: ["s0001"],
    },
  }, {
    systemFolder: "99 System", sourcesFolder: "01 Sources",
    digestTitleOf: () => "Agent实践",
  });
  assert.ok(out.includes("[[99 System/KnowledgeInbox/revisions/digests/dig-item-demo/r000003.md#^c0001|Agent实践]]"));
  assert.ok(out.includes("[[01 Sources/_assets/item-demo/source-000002/normalized#^s0001|s0001]]"));
  // 重复渲染不叠加
  const again = renderCitations(out, {
    c0001: {
      knowledge_id: "kn-a", knowledge_revision: 2, digest_id: "dig-item-demo", digest_revision: 3,
      digest_claim_id: "c0001", item_id: "item-demo", source_revision: 2, segment_ids: ["s0001"],
    },
  }, { systemFolder: "99 System", sourcesFolder: "01 Sources", digestTitleOf: () => "Agent实践" });
  assert.equal(again.match(/依据：/g)?.length, 1);
});
ok("证据链校验：版本不一致或快照缺失即报错", () => {
  const map = {
    c0001: {
      knowledge_id: "kn-a", knowledge_revision: 2, digest_id: "dig-x", digest_revision: 1,
      digest_claim_id: "c0001", item_id: "item-x", source_revision: 1, segment_ids: ["s0001"],
    },
  };
  const okErrors = validateEvidenceMap(map, {
    knowledgeId: "kn-a", baseKnowledgeRevision: 2,
    snapshotExists: () => true, segmentExists: () => true,
  });
  assert.deepEqual(okErrors, []);
  const badRevision = validateEvidenceMap(map, {
    knowledgeId: "kn-a", baseKnowledgeRevision: 3,
    snapshotExists: () => true, segmentExists: () => true,
  });
  assert.ok(badRevision.some((e) => e.includes("基线版本")));
  const missingSnapshot = validateEvidenceMap(map, {
    knowledgeId: "kn-a", baseKnowledgeRevision: 2,
    snapshotExists: () => false, segmentExists: () => true,
  });
  assert.ok(missingSnapshot.some((e) => e.includes("不存在的 Digest 快照")));
  const missingSegment = validateEvidenceMap(map, {
    knowledgeId: "kn-a", baseKnowledgeRevision: 2,
    snapshotExists: () => true, segmentExists: () => false,
  });
  assert.ok(missingSegment.some((e) => e.includes("不存在的原文片段")));
});
ok("kb_promotion 汇总允许 partially_applied（不能用一个布尔值替代观点级状态）", () => {
  const decisions = [
    { ...goodDecision, claim_id: "c0001" },
    { ...goodDecision, claim_id: "c0002" },
  ] as PromotionDecision[];
  assert.equal(aggregatePromotion(decisions, []), "review");
  assert.equal(aggregatePromotion(decisions, ["c0001"]), "partially_applied");
  assert.equal(aggregatePromotion(decisions, ["c0001", "c0002"]), "applied");
  assert.equal(aggregatePromotion([{ ...goodDecision, decision: "keep_digest" } as PromotionDecision], []), "keep_digest");
  assert.equal(aggregatePromotion([], []), "not_evaluated");
});
ok("关系说明区分晋升/留在 Digest/暂缓", () => {
  const decisions = [
    goodDecision,
    { ...goodDecision, claim_id: "c0002", decision: "keep_digest" },
    { ...goodDecision, claim_id: "c0003", decision: "deferred" },
  ] as PromotionDecision[];
  const note = buildRelationNote(decisions, [{ kb_id: "kn-agent-operations", title: "Agent 操作技巧" }]);
  assert.ok(note.includes("1 条观点具备长期增量"));
  assert.ok(note.includes("1 条观点属于重复或无独立补证"));
  assert.ok(note.includes("1 条观点证据不足，暂缓"));
});

// ---- 旧 Bundle 兼容：从云端区解析观点 ----
ok("analysis.json 缺失时从云端区退化解析观点", () => {
  const cloud = [
    "## 一句话总结", "", "演示总结。", "",
    "## 核心观点与证据", "",
    "- [c0001] 先固定上下文（适用条件：长任务）（[[normalized#^s0001|s0001]]）",
  ].join("\n");
  const claims = parseClaimsFromCloudRegion(cloud);
  assert.equal(claims.length, 1);
  assert.equal(claims[0].claim_id, "c0001");
  assert.equal(claims[0].conditions, "长任务");
  assert.deepEqual(claims[0].evidence_ids, ["s0001"]);
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

// ---- 整理任务：幂等、基线保护、串行（docs/08 §7.2、§8.1）----
function digestNoteFor(itemId: string, cloudBody: string): string {
  const m = { ...manifest, item_id: itemId } as KbManifest;
  return renderDigestNote(m, {
    sourceLink: "[[01 Sources/x|原始资料]]", cloudMd: cloudBody, status: "ready",
  });
}
async function buildService(fs: MemFs, settings: KbSettings, output: string) {
  const calls: string[] = [];
  const service = new OrganizeService({
    fs: fs as never,
    settings: () => settings,
    model: async () => ({
      configRef: "p1@v2/cred5",
      caller: {
        call: async (system: string) => {
          calls.push(system.slice(0, 12));
          return { outputText: output, finishReason: "stop" };
        },
      },
    }),
    log: () => undefined,
  });
  return { service, calls };
}
ok("幂等键：同条目同 Digest 版本只处理一次", () => {
  assert.equal(idempotencyKey("item-demo", 3), "item-demo#digest-r3");
  assert.notEqual(idempotencyKey("item-demo", 3), idempotencyKey("item-demo", 4));
});
ok("留在 Digest：无长期增量时不生成候选，且写入本地整理区", async () => {
  const fs = new MemFs();
  const settings = { ...DEFAULT_SETTINGS, knowledgeFolder: "03 Knowledge" };
  const digestPath = "02 Digests/2026/09/2026-09-09 Agent实践--item-demo.md";
  await fs.write(digestPath, digestNoteFor("item-demo", "## 一句话总结\n\n演示。"));
  await fs.write("01 Sources/_assets/item-demo/source-000002/analysis.json", JSON.stringify({
    summary: "演示。",
    key_points: [{ claim_id: "c0001", text: "重复观点", conditions: null, evidence_ids: ["s0001"] }],
  }));
  await fs.write("01 Sources/_assets/item-demo/source-000002/segments.json", JSON.stringify({
    segments: [{ segment_id: "s0001", text: "原文" }],
  }));
  const promotion = JSON.stringify({
    decisions: [{
      ...goodDecision, decision: "keep_digest", target_knowledge_id: null, relation: "duplicate",
    }],
  });
  const { service } = await buildService(fs, settings, promotion);
  await service.enqueue("item-demo", digestPath);
  const result = await service.runBatch();
  assert.equal(result.keptDigest, 1);
  assert.equal(result.prepared, 0);
  const updated = await fs.read(digestPath);
  assert.ok(updated.includes("留在 Digest"));
  assert.ok(updated.includes("kb_promotion: keep_digest"));
  assert.ok((await new ProposalStore(fs as never, "99 System").all()).length === 0);
});
ok("有长期增量：生成待采纳候选，不直接写入 Knowledge", async () => {
  const fs = new MemFs();
  const settings = { ...DEFAULT_SETTINGS, knowledgeFolder: "03 Knowledge" };
  const digestPath = "02 Digests/2026/09/2026-09-09 Agent实践--item-demo.md";
  await fs.write(digestPath, digestNoteFor("item-demo", "## 一句话总结\n\n演示。"));
  await fs.write("01 Sources/_assets/item-demo/source-000002/analysis.json", JSON.stringify({
    summary: "演示。",
    key_points: [{ claim_id: "c0001", text: "先固定上下文", conditions: "长任务", evidence_ids: ["s0001"] }],
  }));
  await fs.write("01 Sources/_assets/item-demo/source-000002/segments.json", JSON.stringify({
    segments: [{ segment_id: "s0001", text: "原文" }],
  }));
  // 已有主题
  await fs.write("03 Knowledge/Agent 操作技巧.md", renderKnowledgeNote({
    kbId: "kn-agent-operations", title: "Agent 操作技巧", aliases: [],
    scope: "讨论可复用的 Agent 操作手法。关键词：提示词", managedBody: "## 当前判断\n\n- [c0002] 旧观点。",
    revision: 1, reviewedAt: "2026-09-09",
  }));
  let stage = 0;
  const service = new OrganizeService({
    fs: fs as never,
    settings: () => settings,
    model: async () => ({
      configRef: "p1@v2/cred5",
      caller: {
        call: async () => {
          stage += 1;
          if (stage === 1) return { outputText: JSON.stringify({ decisions: [goodDecision] }), finishReason: "stop" };
          return {
            outputText: JSON.stringify({
              base_hash: (await (async () => {
                const body = extractPartition(
                  await fs.read("03 Knowledge/Agent 操作技巧.md"), KNOWLEDGE_START, KNOWLEDGE_END) ?? "";
                const { sha256Hex } = await import("../src/vault/template");
                return sha256Hex(body);
              })()),
              no_op: false,
              proposed_managed_body: "## 当前判断\n\n- [c0002] 旧观点。\n- [c0001] 先固定上下文（依据 s0001）。",
              added_claims: [{ claim_id: "c0001", text: "先固定上下文", evidence_refs: ["dig-item-demo#c0001"], segment_ids: ["s0001"] }],
              updated_claims: [], retired_claims: [],
              evidence_map: {
                c0001: {
                  knowledge_id: "kn-agent-operations", knowledge_revision: 1,
                  digest_id: "dig-item-demo", digest_revision: 3, digest_claim_id: "c0001",
                  item_id: "item-demo", source_revision: 2, segment_ids: ["s0001"],
                },
              },
              conflicts: [], change_summary: "新增 1 条观点", promotion_decisions: [],
            }),
            finishReason: "stop",
          };
        },
      },
    }),
    log: () => undefined,
  });
  await service.enqueue("item-demo", digestPath);
  const result = await service.runBatch();
  assert.equal(result.prepared, 1);
  const proposals = await service.listProposals();
  assert.equal(proposals.length, 1);
  assert.equal(proposals[0].state, "ready");
  // 采纳前不写 Knowledge
  assert.ok(!(await fs.read("03 Knowledge/Agent 操作技巧.md")).includes("c0001"));
});
ok("采纳后写入 Knowledge 并保存历史快照与证据映射", async () => {
  const fs = new MemFs();
  const settings = { ...DEFAULT_SETTINGS, knowledgeFolder: "03 Knowledge" };
  const knowledgePath = "03 Knowledge/Agent 操作技巧.md";
  await fs.write(knowledgePath, renderKnowledgeNote({
    kbId: "kn-agent-operations", title: "Agent 操作技巧", aliases: [],
    scope: "范围说明", managedBody: "## 当前判断\n\n- [c0002] 旧观点。", revision: 1, reviewedAt: "2026-09-09",
  }));
  const index = new KnowledgeIndexStore(fs as never, "99 System");
  await index.rebuild("03 Knowledge");
  const body = extractPartition(await fs.read(knowledgePath), KNOWLEDGE_START, KNOWLEDGE_END) ?? "";
  const { sha256Hex } = await import("../src/vault/template");
  const baseHash = await sha256Hex(body);
  const proposals = new ProposalStore(fs as never, "99 System");
  await proposals.put({
    proposal_id: "prop-1", task_id: "task-item-demo", knowledge_id: "kn-agent-operations",
    knowledge_title: "Agent 操作技巧", base_hash: baseHash,
    proposed_managed_body: "## 当前判断\n\n- [c0002] 旧观点。\n- [c0001] 新观点。",
    added_claims: [], updated_claims: [], retired_claims: [], evidence_map: {}, conflicts: [],
    change_summary: "新增 1 条", promotion_decisions: [], state: "ready",
    created_at: "2026-09-09T00:00:00Z", applied_at: null, applied_knowledge_revision: null, no_op: false,
  });
  const { service } = await buildService(fs, settings, "{}");
  const res = await service.accept("prop-1");
  assert.equal(res.applied, true);
  const after = await fs.read(knowledgePath);
  assert.ok(after.includes("[c0001] 新观点。"));
  assert.ok(after.includes("kb_revision: 2"));
  assert.ok(after.includes("## 我的实践与补充"), "人工区必须保留");
  assert.ok(await fs.exists("99 System/KnowledgeInbox/revisions/knowledge/kn-agent-operations/r000001.md"));
});
ok("基线变化时候选被拦为过期，不覆盖用户改动", async () => {
  const fs = new MemFs();
  const settings = { ...DEFAULT_SETTINGS, knowledgeFolder: "03 Knowledge" };
  const knowledgePath = "03 Knowledge/Agent 操作技巧.md";
  await fs.write(knowledgePath, renderKnowledgeNote({
    kbId: "kn-agent-operations", title: "Agent 操作技巧", aliases: [],
    scope: "范围说明", managedBody: "## 当前判断\n\n- [c0002] 旧观点。", revision: 1, reviewedAt: "2026-09-09",
  }));
  await new KnowledgeIndexStore(fs as never, "99 System").rebuild("03 Knowledge");
  const proposals = new ProposalStore(fs as never, "99 System");
  await proposals.put({
    proposal_id: "prop-2", task_id: "task-item-demo", knowledge_id: "kn-agent-operations",
    knowledge_title: "Agent 操作技巧", base_hash: "stale-hash",
    proposed_managed_body: "## 当前判断\n\n- [c0001] 新观点。",
    added_claims: [], updated_claims: [], retired_claims: [], evidence_map: {}, conflicts: [],
    change_summary: "x", promotion_decisions: [], state: "ready",
    created_at: "2026-09-09T00:00:00Z", applied_at: null, applied_knowledge_revision: null, no_op: false,
  });
  const { service } = await buildService(fs, settings, "{}");
  const res = await service.accept("prop-2");
  assert.equal(res.applied, false);
  assert.ok(res.note.includes("已被修改"));
  assert.ok(!(await fs.read(knowledgePath)).includes("c0001"), "不得覆盖用户改动");
});
ok("重复采纳是幂等的，不重复写入", async () => {
  const fs = new MemFs();
  const settings = { ...DEFAULT_SETTINGS, knowledgeFolder: "03 Knowledge" };
  const proposals = new ProposalStore(fs as never, "99 System");
  await proposals.put({
    proposal_id: "prop-3", task_id: "t", knowledge_id: null, knowledge_title: "新主题",
    base_hash: "h", proposed_managed_body: "## 当前判断\n\n- [c0001] A。",
    added_claims: [], updated_claims: [], retired_claims: [], evidence_map: {}, conflicts: [],
    change_summary: "x", promotion_decisions: [], state: "applied",
    created_at: "2026-09-09T00:00:00Z", applied_at: "2026-09-09T01:00:00Z",
    applied_knowledge_revision: 1, no_op: false,
  });
  const { service } = await buildService(fs, settings, "{}");
  const res = await service.accept("prop-3");
  assert.equal(res.applied, false);
  assert.ok(res.note.includes("已经应用过"));
});
ok("端到端：采纳后 Knowledge 结论可两跳回到当时原文", async () => {
  const fs = new MemFs();
  const settings = { ...DEFAULT_SETTINGS, knowledgeFolder: "03 Knowledge" };
  const digestPath = "02 Digests/2026/09/2026-09-09 Agent实践--item-demo.md";
  await fs.write(digestPath, digestNoteFor("item-demo", "## 一句话总结\n\n演示。"));
  await fs.write("01 Sources/_assets/item-demo/source-000002/analysis.json", JSON.stringify({
    summary: "演示。",
    key_points: [{ claim_id: "c0001", text: "先固定上下文", conditions: null, evidence_ids: ["s0001"] }],
  }));
  await fs.write("01 Sources/_assets/item-demo/source-000002/segments.json", JSON.stringify({
    segments: [{ segment_id: "s0001", text: "原文" }],
  }));
  const knowledgePath = "03 Knowledge/Agent 操作技巧.md";
  await fs.write(knowledgePath, renderKnowledgeNote({
    kbId: "kn-agent-operations", title: "Agent 操作技巧", aliases: [], scope: "范围",
    managedBody: "## 当前判断\n\n- [c0002] 旧观点。", revision: 1, reviewedAt: "2026-09-09",
  }));
  await new KnowledgeIndexStore(fs as never, "99 System").rebuild("03 Knowledge");
  const body = extractPartition(await fs.read(knowledgePath), KNOWLEDGE_START, KNOWLEDGE_END) ?? "";
  const { sha256Hex } = await import("../src/vault/template");
  await new ProposalStore(fs as never, "99 System").put({
    proposal_id: "prop-e2e", task_id: "task-item-demo", knowledge_id: "kn-agent-operations",
    knowledge_title: "Agent 操作技巧", base_hash: await sha256Hex(body),
    proposed_managed_body: "## 当前判断\n\n- [c0002] 旧观点。\n- [c0001] 先固定上下文。",
    added_claims: [], updated_claims: [], retired_claims: [],
    evidence_map: {
      c0001: {
        knowledge_id: "kn-agent-operations", knowledge_revision: 1,
        digest_id: "dig-item-demo", digest_revision: 3, digest_claim_id: "c0001",
        item_id: "item-demo", source_revision: 2, segment_ids: ["s0001"],
      },
    },
    conflicts: [], change_summary: "新增 1 条", promotion_decisions: [], state: "ready",
    created_at: "2026-09-09T00:00:00Z", applied_at: null, applied_knowledge_revision: null, no_op: false,
  });
  const { service } = await buildService(fs, settings, "{}");
  const res = await service.accept("prop-e2e");
  assert.equal(res.applied, true);
  const after = await fs.read(knowledgePath);
  // 第一跳：Knowledge → 冻结 Digest 快照块
  assert.ok(after.includes(
    "[[99 System/KnowledgeInbox/revisions/digests/dig-item-demo/r000003.md#^c0001|item-demo]]"),
    "缺少指向冻结 Digest 快照的第一跳引用");
  // 第二跳：快照 → 当时原文片段
  const snapshot = await fs.read("99 System/KnowledgeInbox/revisions/digests/dig-item-demo/r000003.md");
  assert.ok(snapshot.includes("^c0001"));
  assert.ok(snapshot.includes("[[01 Sources/_assets/item-demo/source-000002/normalized#^s0001|s0001]]"));
  // 快照随本地证据留存
  assert.ok(await fs.exists("99 System/KnowledgeInbox/revisions/digests/dig-item-demo/r000003.md"));
});
ok("证据链校验失败时不写入，候选标为过期", async () => {
  const fs = new MemFs();
  const settings = { ...DEFAULT_SETTINGS, knowledgeFolder: "03 Knowledge" };
  const knowledgePath = "03 Knowledge/Agent 操作技巧.md";
  await fs.write(knowledgePath, renderKnowledgeNote({
    kbId: "kn-agent-operations", title: "Agent 操作技巧", aliases: [], scope: "范围",
    managedBody: "## 当前判断\n\n- [c0002] 旧观点。", revision: 1, reviewedAt: "2026-09-09",
  }));
  await new KnowledgeIndexStore(fs as never, "99 System").rebuild("03 Knowledge");
  const body = extractPartition(await fs.read(knowledgePath), KNOWLEDGE_START, KNOWLEDGE_END) ?? "";
  const { sha256Hex } = await import("../src/vault/template");
  await new ProposalStore(fs as never, "99 System").put({
    proposal_id: "prop-bad", task_id: "t", knowledge_id: "kn-agent-operations",
    knowledge_title: "Agent 操作技巧", base_hash: await sha256Hex(body),
    proposed_managed_body: "## 当前判断\n\n- [c0001] 新观点。",
    added_claims: [], updated_claims: [], retired_claims: [],
    evidence_map: {
      c0001: {
        knowledge_id: "kn-agent-operations", knowledge_revision: 1,
        digest_id: "dig-x", digest_revision: 1, digest_claim_id: "c0001",
        item_id: "item-x", source_revision: 1, segment_ids: ["s9999"],
      },
    },
    conflicts: [], change_summary: "x", promotion_decisions: [], state: "ready",
    created_at: "2026-09-09T00:00:00Z", applied_at: null, applied_knowledge_revision: null, no_op: false,
  });
  const { service } = await buildService(fs, settings, "{}");
  const res = await service.accept("prop-bad");
  assert.equal(res.applied, false);
  assert.ok(res.note.includes("证据链校验未通过"));
  assert.ok(!(await fs.read(knowledgePath)).includes("c0001"));
});
ok("回滚：以历史快照恢复；笔记再被改动时先展示差异不覆盖", async () => {
  const fs = new MemFs();
  const settings = { ...DEFAULT_SETTINGS, knowledgeFolder: "03 Knowledge" };
  const knowledgePath = "03 Knowledge/Agent 操作技巧.md";
  const original = renderKnowledgeNote({
    kbId: "kn-agent-operations", title: "Agent 操作技巧", aliases: [], scope: "范围",
    managedBody: "## 当前判断\n\n- [c0002] 旧观点。", revision: 1, reviewedAt: "2026-09-09",
  });
  await fs.write(knowledgePath, original);
  await new KnowledgeIndexStore(fs as never, "99 System").rebuild("03 Knowledge");
  const body = extractPartition(original, KNOWLEDGE_START, KNOWLEDGE_END) ?? "";
  const { sha256Hex } = await import("../src/vault/template");
  const proposals = new ProposalStore(fs as never, "99 System");
  await proposals.put({
    proposal_id: "prop-rb", task_id: "t", knowledge_id: "kn-agent-operations",
    knowledge_title: "Agent 操作技巧", base_hash: await sha256Hex(body),
    proposed_managed_body: "## 当前判断\n\n- [c0002] 旧观点。\n- [c0001] 新观点。",
    added_claims: [], updated_claims: [], retired_claims: [], evidence_map: {}, conflicts: [],
    change_summary: "x", promotion_decisions: [], state: "ready",
    created_at: "2026-09-09T00:00:00Z", applied_at: null, applied_knowledge_revision: null, no_op: false,
  });
  const { service } = await buildService(fs, settings, "{}");
  assert.equal((await service.accept("prop-rb")).applied, true);
  // 用户在应用后又改了笔记
  const edited = (await fs.read(knowledgePath)).replace("## 我的实践与补充", "## 我的实践与补充\n\n用户新写的内容");
  await fs.write(knowledgePath, edited);
  const rolled = await service.rollback("prop-rb");
  assert.equal(rolled.rolledBack, true, "内容未变的部分应可回滚");
  const after = await fs.read(knowledgePath);
  assert.ok(after.includes("## 我的实践与补充\n\n用户新写的内容"), "人工区不得被覆盖");
  assert.ok(!after.includes("[c0001] 新观点。"));
});
ok("回滚遇到机器区被再次修改时拒绝覆盖并给出差异", async () => {
  const fs = new MemFs();
  const settings = { ...DEFAULT_SETTINGS, knowledgeFolder: "03 Knowledge" };
  const knowledgePath = "03 Knowledge/Agent 操作技巧.md";
  await fs.write(knowledgePath, renderKnowledgeNote({
    kbId: "kn-agent-operations", title: "Agent 操作技巧", aliases: [], scope: "范围",
    managedBody: "## 当前判断\n\n- [c0002] 旧观点。", revision: 1, reviewedAt: "2026-09-09",
  }));
  await new KnowledgeIndexStore(fs as never, "99 System").rebuild("03 Knowledge");
  const body = extractPartition(await fs.read(knowledgePath), KNOWLEDGE_START, KNOWLEDGE_END) ?? "";
  const { sha256Hex } = await import("../src/vault/template");
  const proposals = new ProposalStore(fs as never, "99 System");
  await proposals.put({
    proposal_id: "prop-rb2", task_id: "t", knowledge_id: "kn-agent-operations",
    knowledge_title: "Agent 操作技巧", base_hash: await sha256Hex(body),
    proposed_managed_body: "## 当前判断\n\n- [c0002] 旧观点。\n- [c0001] 新观点。",
    added_claims: [], updated_claims: [], retired_claims: [], evidence_map: {}, conflicts: [],
    change_summary: "x", promotion_decisions: [], state: "ready",
    created_at: "2026-09-09T00:00:00Z", applied_at: null, applied_knowledge_revision: null, no_op: false,
  });
  const { service } = await buildService(fs, settings, "{}");
  await service.accept("prop-rb2");
  // 用户直接编辑机器区
  const edited = (await fs.read(knowledgePath)).replace("[c0001] 新观点。", "[c0001] 新观点（用户改写）。");
  await fs.write(knowledgePath, edited);
  const rolled = await service.rollback("prop-rb2");
  assert.equal(rolled.rolledBack, false);
  assert.ok(rolled.note.includes("又被修改"));
  assert.ok(rolled.diff && rolled.diff.includes("用户改写"));
  assert.ok((await fs.read(knowledgePath)).includes("用户改写"), "不得直接用旧快照覆盖");
});
ok("同一主题的两个 Digest 合成一次融合，只落一个候选", async () => {
  const fs = new MemFs();
  const settings = { ...DEFAULT_SETTINGS, knowledgeFolder: "03 Knowledge" };
  const knowledgePath = "03 Knowledge/Agent 操作技巧.md";
  await fs.write(knowledgePath, renderKnowledgeNote({
    kbId: "kn-agent-operations", title: "Agent 操作技巧", aliases: [], scope: "范围",
    managedBody: "## 当前判断\n\n- [c0002] 旧观点。", revision: 1, reviewedAt: "2026-09-09",
  }));
  await new KnowledgeIndexStore(fs as never, "99 System").rebuild("03 Knowledge");
  const body = extractPartition(await fs.read(knowledgePath), KNOWLEDGE_START, KNOWLEDGE_END) ?? "";
  const { sha256Hex } = await import("../src/vault/template");
  const baseHash = await sha256Hex(body);

  // 两个不同条目，都晋升到同一主题
  for (const [itemId, claimText] of [["item-a", "先固定上下文"], ["item-b", "先设定停止条件"]] as const) {
    const digestPath = `02 Digests/2026/09/2026-09-09 X--${itemId}.md`;
    await fs.write(digestPath, digestNoteFor(itemId, "## 一句话总结\n\n演示。"));
    const assets = `01 Sources/_assets/${itemId}/source-000002`;
    await fs.write(`${assets}/analysis.json`, JSON.stringify({
      summary: "演示。",
      key_points: [{ claim_id: "c0001", text: claimText, conditions: null, evidence_ids: ["s0001"] }],
    }));
    await fs.write(`${assets}/segments.json`, JSON.stringify({
      segments: [{ segment_id: "s0001", text: "原文" }],
    }));
  }

  let fusionCalls = 0;
  let promotionCalls = 0;
  const service = new OrganizeService({
    fs: fs as never, settings: () => settings,
    model: async () => ({
      configRef: "p1@v2/cred5",
      caller: {
        call: async (system: string) => {
          if (system.includes("判断一篇单来源提炼")) {
            promotionCalls += 1;
            return { outputText: JSON.stringify({ decisions: [goodDecision] }), finishReason: "stop" };
          }
          fusionCalls += 1;
          assert.ok(system.includes("某个主题"), "融合调用应使用融合提示词");
          return {
            outputText: JSON.stringify({
              base_hash: baseHash, no_op: false,
              proposed_managed_body: "## 当前判断\n\n- [c0002] 旧观点。\n- [c0001] 合并后的观点（依据 s0001）。",
              added_claims: [], updated_claims: [], retired_claims: [],
              evidence_map: {
                c0001: {
                  knowledge_id: "kn-agent-operations", knowledge_revision: 1,
                  digest_id: "dig-item-a", digest_revision: 3, digest_claim_id: "c0001",
                  item_id: "item-a", source_revision: 2, segment_ids: ["s0001"],
                },
              },
              conflicts: [], change_summary: "合并 2 个来源", promotion_decisions: [],
            }),
            finishReason: "stop",
          };
        },
      },
    }),
    log: () => undefined,
  });
  await service.enqueue("item-a", "02 Digests/2026/09/2026-09-09 X--item-a.md");
  await service.enqueue("item-b", "02 Digests/2026/09/2026-09-09 X--item-b.md");
  const result = await service.runBatch();
  assert.equal(promotionCalls, 2, "每篇 Digest 各自做一次晋升判断");
  assert.equal(fusionCalls, 1, "同一主题只发起一次融合");
  assert.equal(result.prepared, 1);
  assert.equal((await service.listProposals()).length, 1);
});
ok("结果未知的任务不自动重发，需显式重试", async () => {
  const fs = new MemFs();
  const settings = { ...DEFAULT_SETTINGS, knowledgeFolder: "03 Knowledge" };
  const tasks = new (await import("../src/knowledge/organize")).OrganizeTaskStore(fs as never, "99 System");
  await tasks.put({
    task_id: "task-x", item_id: "x", digest_path: "02 Digests/x.md",
    digest_source_revision: 1, digest_cloud_hash: "h", target_knowledge_id: null, base_hash: null,
    state: "unknown_outcome", model_config_ref: "", rule_version: "v1", idempotency_key: "k",
    created_at: "2026-09-09T00:00:00Z", updated_at: "2026-09-09T00:00:00Z",
    attempts: 1, last_error: "超时", proposal_path: null,
  });
  let calls = 0;
  const service = new OrganizeService({
    fs: fs as never, settings: () => settings,
    model: async () => ({ configRef: "", caller: { call: async () => { calls += 1; return { outputText: "{}", finishReason: null }; } } }),
    log: () => undefined,
  });
  const result = await service.runBatch();
  assert.equal(calls, 0, "结果未知的任务不得盲目再次调用模型");
  assert.equal(result.skipped, 1);
  await service.retry("task-x");
  assert.equal((await tasks.get("task-x"))?.state, "pending");
});

void (async () => {
  await Promise.all(asyncChecks);
  console.log(`\n全部 ${passed} 项通过`);
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
