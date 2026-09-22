/**
 * 本地整理：主题候选与落盘（docs/23 §6；docs/24 §8）。
 *
 * 主流程已取消逐观点生命周期：没有 `c0001` 晋升账本、没有五维评分、
 * 没有 added/updated/retired_claims，也不让模型回填 evidence_map 或哈希版本。
 *
 *    收到新材料 → 本地检索相关主题（标题/别名/范围索引，不引入向量库）
 *    → 目标选择最小响应 {"target":"T1","reason":"…"}（无匹配给新主题；无增量 keep_digest）
 *    → 模型生成主题修改候选（内容主体 + no_op/change_summary/conflicts）
 *    → 程序计算差异、校验证据与基线 → 用户采纳 / 部分采纳 / 跳过
 *
 * 写入保护（docs/23 §6.4）：候选固定基线；采纳前重读正文，被改过即过期不覆盖；
 * 只替换管理区，保留人工区与未知 frontmatter；先存历史正文与引用表再提交；
 * 引用缺失的原文不用标题补位；重启恢复不重复应用；回滚时正文与引用表成对恢复。
 *
 * 纯逻辑模块：文件与模型调用全部通过注入依赖，可独立测试。
 */

import type { FsLike } from "../vault/records";
import { JsonStore, RevisionStore } from "../vault/records";
import {
  KNOWLEDGE_END,
  KNOWLEDGE_START,
  LOCAL_ORGANIZE_END,
  LOCAL_ORGANIZE_START,
  extractPartition,
  managedTags,
  mergeKbFrontmatter,
  mergeManagedTags,
  readFrontmatterValue,
  renderKnowledgeNote,
  renderLocalOrganize,
  renderProposalIndex,
  replacePartition,
  sha256Hex,
} from "../vault/template";
import {
  assembleContentDocument,
  buildRefTableFromSegments,
  parseContentDocument,
  quoteVerificationErrors,
  referenceHashErrors,
  renderContentMarkdown,
  renderReferenceTable,
  selectBlocksForAdoption,
} from "../vault/content";
import { DocumentIndex } from "../vault/documents";
import {
  digestKbId,
  documentsIndexPath,
  knowledgeIdFromTitle,
  knowledgeNotePath,
  organizeDir,
  proposalsDir,
  resolveAvailableNotePath,
  revisionDir,
  sourceAssetsDir,
} from "../vault/paths";
import { KnowledgeIndexStore, matchKnowledge } from "./index";
import { refAnchor, TopicReferenceStore } from "./citations";
import {
  CONTENT_FORMAT_VERSION,
  CONTENT_RECIPE_VERSION,
  ORGANIZE_RULE_VERSION,
  type ContentDocumentV3,
  type ContentRefV3,
  type KbSettings,
  type LegacyProposal,
  type OrganizeTask,
  type OrganizeTaskState,
  type TopicProposal,
} from "../types";
import {
  buildFusionPrompt,
  buildTargetSelectionPrompt,
  FUSION_SYSTEM_PROMPT,
  TARGET_SYSTEM_PROMPT,
  validateFusionOutput,
  validateTargetSelection,
} from "./prompts";
import { LocalModelError, parseModelJson, type ModelCallerLike } from "../providers/local";

export interface OrganizeDeps {
  fs: FsLike;
  settings: () => KbSettings;
  /** 解析当前可用的本地模型（可能抛出 LocalModelUnavailableError）。 */
  model: () => Promise<{ caller: ModelCallerLike; configRef: string }>;
  log: (msg: string) => void;
  /** 候选变化后重建 00 Inbox/知识更新候选.md。 */
  onProposalsChanged?: () => Promise<void>;
}

/** 一篇 Digest 的整理输入：同版本 content.json 与本地固定原文。 */
export interface DigestInput {
  itemId: string;
  digestPath: string;
  sourceRevision: number;
  bundleRevision: number;
  title: string;
  document: ContentDocumentV3;
  /** content.json 原始文本与其哈希：真实输入基线由程序持有。 */
  raw: string;
  baselineHash: string;
}

function nowIso(): string {
  return new Date().toISOString();
}

/** 任务存储：每个任务一个文件，便于崩溃后逐条恢复。 */
export class OrganizeTaskStore {
  private dir: string;
  constructor(private fs: FsLike, systemFolder: string) {
    this.dir = organizeDir(systemFolder);
  }

  private path(taskId: string): string {
    return `${this.dir}/${taskId}.json`;
  }

  async put(task: OrganizeTask): Promise<void> {
    await this.fs.write(this.path(task.task_id), JSON.stringify(task, null, 2));
  }

  async get(taskId: string): Promise<OrganizeTask | null> {
    const p = this.path(taskId);
    if (!(await this.fs.exists(p))) return null;
    try {
      return JSON.parse(await this.fs.read(p)) as OrganizeTask;
    } catch {
      return null;
    }
  }

  async all(): Promise<OrganizeTask[]> {
    const out: OrganizeTask[] = [];
    for (const entry of await this.fs.list(this.dir)) {
      if (!entry.endsWith(".json")) continue;
      try {
        out.push(JSON.parse(await this.fs.read(entry)) as OrganizeTask);
      } catch {
        // 损坏的任务文件跳过，不阻塞其余任务
      }
    }
    return out.sort((a, b) => a.created_at.localeCompare(b.created_at));
  }
}

/** 旧候选判定：带逐观点字段的候选只归档，不由新流程采纳（docs/23 §8.2 末条）。 */
export function looksLegacyProposal(raw: Record<string, unknown>): boolean {
  return Array.isArray(raw.promotion_decisions) || Array.isArray(raw.added_claims) || raw.protocol !== 3;
}

/** 候选存储：`99 System/KnowledgeInbox/proposals/`。 */
export class ProposalStore {
  private dir: string;
  constructor(private fs: FsLike, systemFolder: string) {
    this.dir = proposalsDir(systemFolder);
  }

  private path(proposalId: string): string {
    return `${this.dir}/${proposalId}.json`;
  }

  async put(proposal: TopicProposal): Promise<void> {
    await this.fs.write(this.path(proposal.proposal_id), JSON.stringify(proposal, null, 2));
  }

  async get(proposalId: string): Promise<TopicProposal | null> {
    const p = this.path(proposalId);
    if (!(await this.fs.exists(p))) return null;
    try {
      const raw = JSON.parse(await this.fs.read(p)) as Record<string, unknown>;
      if (looksLegacyProposal(raw)) return null; // 旧候选不强行转换到新融合规则
      return raw as unknown as TopicProposal;
    } catch {
      return null;
    }
  }

  async all(): Promise<TopicProposal[]> {
    const out: TopicProposal[] = [];
    for (const entry of await this.fs.list(this.dir)) {
      if (!entry.endsWith(".json")) continue;
      const proposal = await this.get(entry.split("/").pop()!.replace(/\.json$/, ""));
      if (proposal) out.push(proposal);
    }
    return out.sort((a, b) => b.created_at.localeCompare(a.created_at));
  }

  /** 旧版逐观点候选：可读归档，仅供查看。 */
  async legacyAll(): Promise<LegacyProposal[]> {
    const out: LegacyProposal[] = [];
    for (const entry of await this.fs.list(this.dir)) {
      if (!entry.endsWith(".json")) continue;
      try {
        const raw = JSON.parse(await this.fs.read(entry)) as Record<string, unknown>;
        if (!looksLegacyProposal(raw)) continue;
        out.push({
          proposal_id: String(raw.proposal_id ?? entry.split("/").pop()),
          knowledge_id: raw.knowledge_id ? String(raw.knowledge_id) : null,
          knowledge_title: raw.knowledge_title ? String(raw.knowledge_title) : null,
          change_summary: String(raw.change_summary ?? ""),
          created_at: String(raw.created_at ?? ""),
          state: String(raw.state ?? "ready"),
        });
      } catch {
        // 损坏文件跳过
      }
    }
    return out.sort((a, b) => b.created_at.localeCompare(a.created_at));
  }
}

/** 幂等键：同一条目同一 Digest 内容版本只处理一次（docs/23 §6.4 第 1 条）。 */
export function idempotencyKey(itemId: string, digestRevision: number): string {
  return `${itemId}#content-r${digestRevision}`;
}

export function taskIdOf(itemId: string): string {
  return `task-${itemId}`;
}

/** 短哈希：候选 ID 由主题与输入派生（同输入得同 ID，重试幂等）。 */
export function hashShort(text: string): string {
  let h = 0x811c9dc5;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h.toString(16).padStart(8, "0");
}

/**
 * 本地固定原文快照：按 `item@source_revision` 惰性加载 `segments.json`。
 *
 * 缺少被引修订时返回 null，调用方据此暂缓采纳——不能拿最新原文冒充旧版本。
 */
class SegmentCache {
  private tables = new Map<string, Record<string, string> | null>();
  constructor(private fs: FsLike, private sourcesFolder: string) {}

  async load(itemId: string, sourceRevision: number): Promise<void> {
    const key = `${itemId}@${sourceRevision}`;
    if (this.tables.has(key)) return;
    const path = `${sourceAssetsDir(this.sourcesFolder, itemId, sourceRevision)}/segments.json`;
    const out: Record<string, string> = {};
    if (await this.fs.exists(path)) {
      try {
        const doc = JSON.parse(await this.fs.read(path)) as { segments?: Array<{ segment_id?: string; text?: string }> };
        for (const s of doc.segments ?? []) {
          if (s.segment_id) out[s.segment_id] = s.text ?? "";
        }
      } catch {
        // 片段索引损坏：留空表，校验会如实报「原文不完整」
      }
    }
    this.tables.set(key, Object.keys(out).length ? out : null);
  }

  textOf = (itemId: string, sourceRevision: number, segmentId: string): string | null => {
    const table = this.tables.get(`${itemId}@${sourceRevision}`);
    return table ? table[segmentId] ?? null : null;
  };
}

/** 读取一篇 Digest 的 v3 内容文档与输入基线（回读以 content.json 为准）。 */
export async function readDigestInput(
  fs: FsLike,
  settings: KbSettings,
  itemId: string,
  digestPath: string,
): Promise<DigestInput | null> {
  if (!(await fs.exists(digestPath))) return null;
  const note = await fs.read(digestPath);
  const sourceRevision = Number(readFrontmatterValue(note, "kb_source_revision") ?? "1") || 1;
  const bundleRevision = Number(readFrontmatterValue(note, "kb_digest_revision") ?? "1") || 1;
  const title = (/^#\s+(.+)$/m.exec(note)?.[1] ?? itemId).replace(/：提炼$/, "").trim();
  const contentPath = `${sourceAssetsDir(settings.sourcesFolder, itemId, sourceRevision)}/content.json`;
  if (!(await fs.exists(contentPath))) return null;
  const raw = await fs.read(contentPath);
  let parsed: ReturnType<typeof parseContentDocument>;
  try {
    parsed = parseContentDocument(JSON.parse(raw));
  } catch {
    return null;
  }
  if (!parsed.document) return null;
  return {
    itemId,
    digestPath,
    sourceRevision,
    bundleRevision,
    title,
    document: parsed.document,
    raw,
    baselineHash: await sha256Hex(raw),
  };
}

export interface OrganizeRunResult {
  prepared: number;
  keptDigest: number;
  failed: number;
  skipped: number;
  messages: string[];
}

/** 本地整理服务：串行处理任务；暂停后不发新请求，已返回结果及时保存。 */
export class OrganizeService {
  private paused = false;
  private running = false;
  private tasks: OrganizeTaskStore;
  private proposals: ProposalStore;
  private revisions: RevisionStore;
  private index: KnowledgeIndexStore;
  private refs: TopicReferenceStore;
  private docs: DocumentIndex;
  private segments: SegmentCache;

  constructor(private deps: OrganizeDeps) {
    const systemFolder = deps.settings().systemFolder;
    this.tasks = new OrganizeTaskStore(deps.fs, systemFolder);
    this.proposals = new ProposalStore(deps.fs, systemFolder);
    this.revisions = new RevisionStore(deps.fs, systemFolder);
    this.index = new KnowledgeIndexStore(deps.fs, systemFolder);
    this.refs = new TopicReferenceStore(deps.fs, systemFolder);
    this.docs = new DocumentIndex(deps.fs, documentsIndexPath(systemFolder));
    this.segments = new SegmentCache(deps.fs, deps.settings().sourcesFolder);
  }

  get isRunning(): boolean { return this.running; }
  get isPaused(): boolean { return this.paused; }

  pause(): void { this.paused = true; }
  resume(): void { this.paused = false; }

  /** 新 Digest 入库后准备整理候选；没有 v3 内容文档时不排队，也不假装成功。 */
  async enqueue(itemId: string, digestPath: string): Promise<OrganizeTask | null> {
    const s = this.deps.settings();
    const input = await readDigestInput(this.deps.fs, s, itemId, digestPath);
    if (!input) {
      this.deps.log(`条目 ${itemId} 没有可读取的 v3 内容文档，未加入整理队列。`);
      return null;
    }
    const taskId = taskIdOf(itemId);
    const existing = await this.tasks.get(taskId);
    const key = idempotencyKey(itemId, input.document.revision);
    if (existing && existing.idempotency_key === key && existing.state !== "failed") return existing;

    const task: OrganizeTask = {
      task_id: taskId,
      item_id: itemId,
      digest_path: digestPath,
      digest_source_revision: input.sourceRevision,
      digest_document_id: input.document.document_id,
      digest_revision: input.document.revision,
      digest_cloud_hash: input.baselineHash,
      target_knowledge_id: null,
      base_hash: null,
      state: "pending",
      model_config_ref: "",
      rule_version: ORGANIZE_RULE_VERSION,
      idempotency_key: key,
      created_at: existing?.created_at ?? nowIso(),
      updated_at: nowIso(),
      attempts: 0,
      last_error: null,
      proposal_path: null,
    };
    await this.tasks.put(task);
    return task;
  }

  /** 手动指定范围：当前 Digest / 未整理与已过期 / 手动选择。 */
  async listPending(): Promise<OrganizeTask[]> {
    return (await this.tasks.all()).filter((t) =>
      ["pending", "failed", "unknown_outcome", "stale"].includes(t.state));
  }

  /** 处理一批任务：每篇材料一次目标选择，每个主题一次候选生成（docs/23 §6.1）。 */
  async runBatch(limit = 20): Promise<OrganizeRunResult> {
    const result: OrganizeRunResult = { prepared: 0, keptDigest: 0, failed: 0, skipped: 0, messages: [] };
    if (this.running) {
      result.messages.push("整理已在进行，跳过本次触发。");
      return result;
    }
    this.running = true;
    try {
      const tasks = await this.listPending();
      if (tasks.length) await this.index.rebuild(this.deps.settings().knowledgeFolder);
      for (const task of tasks.slice(0, limit)) {
        if (this.paused) {
          result.messages.push("已暂停：不再发起新的模型请求。");
          break;
        }
        if (task.state === "unknown_outcome") {
          result.skipped += 1;
          result.messages.push(`${task.task_id}：上次调用结果未知，需显式重试，未自动重发。`);
          continue;
        }
        try {
          const outcome = await this.prepare(task);
          if (outcome === "candidate") result.prepared += 1;
          else if (outcome === "skipped") {
            result.skipped += 1;
            result.messages.push(`${task.task_id}：输入已变化，标为过期待复核。`);
          } else {
            result.keptDigest += 1;
            result.messages.push(`${task.task_id}：${outcome === "no_change" ? "现有主题无需修改。" : "无长期增量，留在 Digest。"}`);
          }
        } catch (err) {
          result.failed += 1;
          const message = err instanceof Error ? err.message : String(err);
          task.state = err instanceof LocalModelError && err.kind === "unknown_outcome"
            ? "unknown_outcome" : "failed";
          task.attempts += 1;
          task.last_error = message;
          task.updated_at = nowIso();
          await this.tasks.put(task);
          result.messages.push(`${task.task_id}：${message}`);
          this.deps.log(`整理失败（${task.task_id}）：${message}`);
          if (err instanceof LocalModelError && err.kind === "auth") {
            result.messages.push("Key 失效：只暂停该配置的任务，不回退到另一配置。");
            break;
          }
        }
      }
      if (this.deps.onProposalsChanged) await this.deps.onProposalsChanged();
    } finally {
      this.running = false;
    }
    return result;
  }

  /** 显式重试一个结果未知或失败的任务（不盲目重发，由用户点按触发）。 */
  async retry(taskId: string): Promise<void> {
    const task = await this.tasks.get(taskId);
    if (!task) return;
    task.state = "pending";
    task.last_error = null;
    task.updated_at = nowIso();
    await this.tasks.put(task);
  }

  /** 一篇材料 → 主题修改候选（或 keep_digest / 无需修改）。 */
  private async prepare(task: OrganizeTask): Promise<"candidate" | "kept_digest" | "no_change" | "skipped"> {
    const s = this.deps.settings();
    const input = await readDigestInput(this.deps.fs, s, task.item_id, task.digest_path);
    if (!input) throw new Error("Digest 内容文档不可读取（content.json 缺失或格式不受支持）。");
    if (input.baselineHash !== task.digest_cloud_hash) {
      task.state = "stale";
      task.updated_at = nowIso();
      await this.tasks.put(task);
      return "skipped";
    }
    task.state = "running";
    task.updated_at = nowIso();
    await this.tasks.put(task);

    const { caller, configRef } = await this.deps.model();
    task.model_config_ref = configRef;
    this.labels.set(input.itemId, input.title);

    // 1) 本地检索相关主题：复用标题/别名/范围索引
    const matched = matchKnowledge((await this.index.read()).entries, {
      title: input.title,
      text: [input.document.summary, ...input.document.sections.flatMap((sec) => sec.blocks.map((b) => b.text))].join("\n"),
    });
    const candidates = matched.map((m, i) => ({
      id: `T${i + 1}`, kb_id: m.entry.kb_id, title: m.entry.title, scope: m.entry.scope, path: m.entry.path,
    }));

    // 2) 目标选择：最小响应
    const targetRaw = await caller.call(
      TARGET_SYSTEM_PROMPT,
      buildTargetSelectionPrompt({
        title: input.title,
        summary: input.document.summary,
        points: input.document.sections.flatMap((sec) => sec.blocks
          .filter((b) => b.kind === "claim" || b.kind === "quote")
          .map((b) => (sec.heading ? `${sec.heading}：` : "") + b.text)),
        candidates: candidates.map((c) => ({ id: c.id, title: c.title, scope: c.scope })),
      }),
    );
    const { selection, errors } = validateTargetSelection(parseModelJson(targetRaw.outputText), {
      candidateIds: candidates.map((c) => c.id),
    });
    if (errors.length) throw new Error(`目标选择未通过校验：${errors.slice(0, 3).join("；")}`);
    if (!selection) throw new Error("目标选择结果为空。");

    if (selection.keepDigest) {
      await this.writeLocalRegion(task, input, {
        outcome: "keep_digest", reason: selection.reason, targetTitle: null, targetPath: null,
      });
      task.state = "kept_digest";
      task.updated_at = nowIso();
      await this.tasks.put(task);
      return "kept_digest";
    }

    const chosen = selection.target ? candidates.find((c) => c.id === selection.target) ?? null : null;
    if (!selection.target && !selection.newTopic) throw new Error("未匹配已有主题时必须给出新主题建议。");
    task.target_knowledge_id = chosen?.kb_id ?? null;

    const targetTitle = chosen?.title ?? selection.newTopic?.name ?? input.title;
    const kbId = chosen?.kb_id ?? knowledgeIdFromTitle(targetTitle);
    const baseline = await this.readTopicBaseline(chosen?.path ?? null, kbId);
    const baseHash = await sha256Hex(baseline.body);

    // 3) 现有依据与新来源共用同一张任务引用表：旧依据继续直连原文
    const sources = await this.collectRefSources(input, baseline.references);
    const refTable = await buildRefTableFromSegments(sources.entries, this.segments.textOf);

    const fusionRaw = await caller.call(
      FUSION_SYSTEM_PROMPT,
      buildFusionPrompt({
        knowledge: { title: targetTitle, scope: chosen?.scope ?? selection.newTopic?.scope ?? "" },
        currentBody: baseline.body,
        existingMaterial: sources.existingLabels,
        newMaterial: {
          title: input.title,
          summary: input.document.summary,
          sections: input.document.sections.map((sec) => ({
            heading: sec.heading,
            blocks: sec.blocks.map((b) => ({ kind: b.kind, text: b.text, refs: b.refs })),
          })),
          material: sources.newLabels,
        },
        lockedRegions: ["## 我的实践与补充", "## 我的备注与判断"],
        userInstruction: null,
      }),
    );
    const parsed = parseModelJson(fusionRaw.outputText);
    const extension = validateFusionOutput(parsed);
    if (extension.errors.length) {
      throw new Error(`主题候选未通过校验：${extension.errors.slice(0, 3).join("；")}`);
    }
    const proposalId = `prop-${kbId}--${hashShort(`${input.document.document_id}r${input.document.revision}+${baseHash.slice(0, 8)}`)}`;
    const proposalBase = {
      proposal_id: proposalId,
      task_id: task.task_id,
      protocol: 3 as const,
      knowledge_id: chosen?.kb_id ?? null,
      knowledge_title: targetTitle,
      new_topic: chosen ? null : selection.newTopic,
      base_hash: baseHash,
      base_revision: baseline.revision,
      baseline_body: baseline.body,
      change_summary: extension.changeSummary,
      conflicts: extension.conflicts,
      target_reason: selection.reason,
      state: "ready",
      created_at: nowIso(),
      applied_at: null,
      applied_knowledge_revision: null,
    };

    if (extension.noOp) {
      // no_op 不重发整篇正文，也不写任何东西（docs/24 §3）
      await this.proposals.put({ ...proposalBase, candidate_document: null, no_op: true });
      await this.writeLocalRegion(task, input, {
        outcome: "no_change", reason: selection.reason, targetTitle, targetPath: chosen?.path ?? null,
      });
      task.state = "ready";
      task.proposal_path = `${proposalsDir(s.systemFolder)}/${proposalId}.json`;
      task.updated_at = nowIso();
      await this.tasks.put(task);
      return "no_change";
    }

    // 4) 程序组装 v3 文档：身份、来源版本与引用表都由程序填
    const assembled = await assembleContentDocument(parsed, {
      documentId: kbId,
      kind: "knowledge",
      revision: baseline.revision + 1,
      task: "knowledge_fusion",
      recipeVersion: `${CONTENT_RECIPE_VERSION}+${ORGANIZE_RULE_VERSION}`,
      refTable,
      segmentTexts: this.segments.textOf,
      createdAt: nowIso(),
      inputDocuments: [
        { document_id: input.document.document_id, kind: "digest", revision: input.document.revision },
        ...(chosen ? [{ document_id: kbId, kind: "knowledge", revision: baseline.revision }] : []),
      ],
      sourceRevisions: sources.sourceRevisions,
    });
    if (!assembled.document || assembled.completeness.state === "failed") {
      const detail = assembled.completeness.gaps.map((g) => g.message).filter(Boolean).join("；");
      throw new Error(`主题候选没有可用内容：${detail || assembled.errors.join("；") || "未知原因"}`);
    }

    await this.proposals.put({
      ...proposalBase,
      candidate_document: assembled.document,
      no_op: false,
    });
    await this.writeLocalRegion(task, input, {
      outcome: "candidate", reason: selection.reason, targetTitle, targetPath: chosen?.path ?? null,
    });
    task.state = "ready";
    task.proposal_path = `${proposalsDir(s.systemFolder)}/${proposalId}.json`;
    task.updated_at = nowIso();
    await this.tasks.put(task);
    return "candidate";
  }

  /** 主题基线：受管理正文 + 该版引用表（未迁移主题没有引用表）。 */
  private async readTopicBaseline(
    path: string | null,
    kbId: string,
  ): Promise<{ body: string; revision: number; references: Record<string, ContentRefV3> }> {
    if (!path || !(await this.deps.fs.exists(path))) return { body: "", revision: 0, references: {} };
    const text = await this.deps.fs.read(path);
    // 与写盘一致地按 trim 后取哈希：`rewriteKnowledgeNote` 写入的是 trim 过的正文，
    // 否则回滚恢复旧正文会被误判成「主题正文已被修改」。
    const body = (extractPartition(text, KNOWLEDGE_START, KNOWLEDGE_END) ?? "").trim();
    const revision = Number(readFrontmatterValue(text, "kb_revision") ?? "0") || 0;
    const record = await this.refs.read(kbId, revision) ?? await this.refs.latest(kbId);
    return { body, revision, references: record?.references ?? {} };
  }

  /** 引用表素材：主题现有依据在前，新来源在后；同一范围只分配一个 R 键。 */
  private async collectRefSources(input: DigestInput, existingRefs: Record<string, ContentRefV3>): Promise<{
    entries: Array<{ item_id: string; source_revision: number; segment_ids: string[] }>;
    existingLabels: Array<{ ref: string; source: string; text: string }>;
    newLabels: Array<{ ref: string; source: string; text: string }>;
    sourceRevisions: Array<{ item_id: string; source_revision: number }>;
  }> {
    const s = this.deps.settings();
    const entries: Array<{ item_id: string; source_revision: number; segment_ids: string[] }> = [];
    const existingLabels: Array<{ ref: string; source: string; text: string }> = [];
    const newLabels: Array<{ ref: string; source: string; text: string }> = [];
    const seen = new Map<string, { item_id: string; source_revision: number }>();

    const add = async (ref: ContentRefV3, source: string, bucket: Array<{ ref: string; source: string; text: string }>) => {
      await this.segments.load(ref.item_id, ref.source_revision);
      const dedupe = `${ref.item_id}@${ref.source_revision}:${ref.segment_ids.join(",")}`;
      if (seen.has(dedupe)) return;
      seen.set(dedupe, { item_id: ref.item_id, source_revision: ref.source_revision });
      entries.push({ item_id: ref.item_id, source_revision: ref.source_revision, segment_ids: ref.segment_ids });
      const text = ref.segment_ids.map((sid) => this.segments.textOf(ref.item_id, ref.source_revision, sid) ?? "").join("\n");
      bucket.push({ ref: `R${entries.length}`, source, text });
    };

    for (const ref of Object.values(existingRefs)) await add(ref, "主题已有依据", existingLabels);
    for (const ref of Object.values(input.document.references)) await add(ref, input.title, newLabels);
    return {
      entries,
      existingLabels,
      newLabels,
      sourceRevisions: [...new Map(
        [...seen.values()].map((v) => [`${v.item_id}@${v.source_revision}`, v] as const),
      ).values()],
    };
  }

  /** 只替换 Digest 的本地整理区与整理结论（人工区与未知字段不动）。 */
  private async writeLocalRegion(
    task: OrganizeTask,
    input: DigestInput,
    result: {
      outcome: "candidate" | "keep_digest" | "no_change";
      reason: string | null;
      targetTitle: string | null;
      targetPath: string | null;
    },
  ): Promise<void> {
    const fs = this.deps.fs;
    const path = task.digest_path;
    if (!(await fs.exists(path))) return;
    const current = await fs.read(path);
    const region = renderLocalOrganize({
      relationNote: [
        result.targetTitle ? `建议修改主题：${result.targetTitle}` : "未匹配到已有主题。",
        result.reason ? `依据：${result.reason}` : "",
      ].filter(Boolean).join("\n"),
      ...result,
    });
    const organize = result.outcome === "candidate" ? "candidate"
      : result.outcome === "keep_digest" ? "keep_digest" : "no_change";
    const withFm = mergeKbFrontmatter(current, [
      `kb_id: "${digestKbId(input.itemId)}"`,
      `kb_item_id: "${input.itemId}"`,
      "kb_type: digest",
      `kb_source_revision: ${input.sourceRevision}`,
      `kb_digest_revision: ${input.bundleRevision}`,
      `kb_content_revision: ${input.document.revision}`,
      `kb_format_version: "${CONTENT_FORMAT_VERSION}"`,
      `kb_organize: ${organize}`,
    ]);
    const rebuilt = replacePartition(withFm, LOCAL_ORGANIZE_START, LOCAL_ORGANIZE_END, region);
    const tagged = mergeManagedTags(rebuilt, managedTags("digest", organize));
    if (tagged !== current) await fs.write(path, tagged);
  }

  /** 候选索引（00 Inbox/知识更新候选.md）。 */
  async renderProposalIndexMd(): Promise<string> {
    const items = await this.proposals.all();
    return renderProposalIndex(items.map((p) => ({
      proposalId: p.proposal_id,
      knowledgeTitle: p.knowledge_title,
      state: p.state,
      changeSummary: p.change_summary,
      createdAt: p.created_at,
      noOp: p.no_op,
    })));
  }

  async listProposals(): Promise<TopicProposal[]> {
    return this.proposals.all();
  }

  /** 旧版逐观点候选（归档，只读）。 */
  async listLegacyProposals(): Promise<LegacyProposal[]> {
    return this.proposals.legacyAll();
  }

  /** 跳过候选：只改本地状态，不写 Knowledge。 */
  async skip(proposalId: string): Promise<void> {
    const p = await this.proposals.get(proposalId);
    if (!p) return;
    p.state = "skipped";
    await this.proposals.put(p);
    await this.syncTaskState(p, "skipped");
    if (this.deps.onProposalsChanged) await this.deps.onProposalsChanged();
  }

  /**
   * 采纳候选：整篇或按块部分采纳。
   *
   * `keep` 是按 section/block 的勾选矩阵：取消勾选的块不写入，未再使用的引用被清除，
   * 勾选后的 `quote` 块重新做逐字校验（docs/23 §6.3）。自由文本编辑不会回写成结构化
   * 候选，因此界面只提供整篇采纳与按块采纳，不假装可靠的逐段合并。
   */
  async accept(proposalId: string, opts: { keep?: boolean[][] } = {}): Promise<{ applied: boolean; note: string }> {
    const p = await this.proposals.get(proposalId);
    if (!p) return { applied: false, note: "候选不存在；若是旧版逐观点候选，它已归档，需要基于当前材料重新生成。" };
    if (p.state === "applied") return { applied: false, note: "该候选已经应用过（幂等）。" };
    if (p.no_op || !p.candidate_document) {
      p.state = "applied";
      p.applied_at = nowIso();
      await this.proposals.put(p);
      await this.syncTaskState(p, "applied");
      return { applied: false, note: "候选判定为无需修改，未写入任何内容。" };
    }
    return this.apply(p, opts.keep);
  }

  /** 写入 Knowledge：先校验证据与基线，再存历史正文与引用表，最后替换管理区。 */
  private async apply(p: TopicProposal, keep?: boolean[][]): Promise<{ applied: boolean; note: string }> {
    const s = this.deps.settings();
    const fs = this.deps.fs;
    let document = p.candidate_document!;
    if (keep) {
      const pruned = selectBlocksForAdoption(document, keep);
      document = pruned.document;
      if (!document.sections.some((sec) => sec.blocks.length)) {
        return { applied: false, note: "没有勾选任何内容块，未写入。" };
      }
    }

    // 证据：所引原文必须在本地固定快照中逐字可核（缺版本即暂缓，不拿最新原文冒充）
    for (const ref of Object.values(document.references)) {
      await this.segments.load(ref.item_id, ref.source_revision);
    }
    const quoteErrors = quoteVerificationErrors(document, this.segments.textOf);
    if (quoteErrors.length) return { applied: false, note: `摘录校验未通过：${quoteErrors.slice(0, 2).join("；")}` };
    const hashErrors = await referenceHashErrors(document, this.segments.textOf);
    if (hashErrors.length) return { applied: false, note: `原文快照校验未通过：${hashErrors.slice(0, 2).join("；")}` };

    // 目标定位靠文档身份，不靠文件名
    const entry = p.knowledge_id ? (await this.index.read()).entries.find((e) => e.kb_id === p.knowledge_id) : null;
    const kbId = p.knowledge_id ?? knowledgeIdFromTitle(p.knowledge_title ?? "未命名主题");
    const isNew = !entry;
    const path = entry?.path ?? await this.reserveKnowledgePath(kbId, p.knowledge_title ?? "未命名主题");
    const current = (await fs.exists(path)) ? await fs.read(path) : "";
    const nextRevision = p.base_revision + 1;
    const body = await this.renderKnowledgeBody(document);
    const bodyHash = await sha256Hex(body.trim());

    // 恢复记录先于基线判断：本候选若已提交过，正文与基线必然不一致，
    // 但正确处理是补状态而不是把它标成过期（docs/23 §6.4 第 6 条）。
    const recovery = new JsonStore<Record<string, unknown>>(fs,
      `${revisionDir(s.systemFolder, "knowledge", kbId)}/recovery-${p.proposal_id}.json`, () => ({}));
    const prior = await recovery.read();
    if (prior.state === "committed" && String(prior.proposed_hash) === bodyHash) {
      p.state = "applied";
      p.applied_at = p.applied_at ?? nowIso();
      p.applied_knowledge_revision = Number(prior.target_revision) || nextRevision;
      await this.proposals.put(p);
      await this.index.upsert(s.knowledgeFolder, path);
      return { applied: false, note: "本次候选此前已提交（恢复检查），未重复写入。" };
    }
    if (!isNew) {
      if (!current) {
        p.state = "stale";
        await this.proposals.put(p);
        return { applied: false, note: "目标主题笔记已不存在；候选标为过期。" };
      }
      const currentBody = extractPartition(current, KNOWLEDGE_START, KNOWLEDGE_END) ?? "";
      if (await sha256Hex(currentBody.trim()) !== p.base_hash) {
        p.state = "stale";
        await this.proposals.put(p);
        await this.syncTaskState(p, "stale");
        return { applied: false, note: "主题正文已被修改，候选已标为过期；请重新生成或人工合并。" };
      }
    }

    await recovery.write({
      proposal_id: p.proposal_id,
      task_id: p.task_id,
      knowledge_id: kbId,
      target_revision: nextRevision,
      base_hash: p.base_hash,
      proposed_hash: bodyHash,
      state: "prepared",
      created_at: nowIso(),
    });

    // 先存历史正文与本版引用表，再提交正文（回滚成对恢复）
    if (current) await this.revisions.save("knowledge", kbId, p.base_revision, current);
    await this.refs.save(kbId, {
      revision: nextRevision,
      document_id: document.document_id,
      body_hash: bodyHash,
      references: document.references,
    });

    const today = nowIso().slice(0, 10);
    const newText = !current
      ? renderKnowledgeNote({
        kbId, title: p.knowledge_title ?? "未命名主题", aliases: [],
        scope: p.new_topic?.scope ?? "", managedBody: body,
        revision: nextRevision, reviewedAt: today,
      })
      : this.rewriteKnowledgeNote(current, body, nextRevision, today);
    await fs.write(path, newText);
    const written = extractPartition(newText, KNOWLEDGE_START, KNOWLEDGE_END) ?? "";
    if (await sha256Hex(written.trim()) !== bodyHash) {
      return { applied: false, note: "写入后校验不一致，历史快照与引用表已保留；请检查笔记。" };
    }
    await recovery.write({ ...(await recovery.read()), state: "committed", committed_at: nowIso() });

    p.state = "applied";
    p.applied_at = nowIso();
    p.applied_knowledge_revision = nextRevision;
    await this.proposals.put(p);
    await this.docs.register({
      kb_id: kbId, path, kind: "knowledge", item_id: null,
      title: p.knowledge_title ?? "", updated_at: nowIso(),
    });
    await this.index.upsert(s.knowledgeFolder, path);
    await this.syncTaskState(p, "applied");
    if (this.deps.onProposalsChanged) await this.deps.onProposalsChanged();
    return {
      applied: true,
      note: isNew
        ? `已新建主题「${p.knowledge_title}」（${kbId}），rev ${nextRevision}。`
        : `已写入主题「${p.knowledge_title}」，rev ${p.base_revision} → ${nextRevision}。`,
    };
  }

  /** 主题正文 = 内容主体渲染 + 「依据与原文」表（引用直连固定原文）。 */
  private async renderKnowledgeBody(doc: ContentDocumentV3): Promise<string> {
    await this.labelRefs(doc);
    const s = this.deps.settings();
    const linkOf = (ref: ContentRefV3) => refAnchor(s.sourcesFolder, ref);
    const body = renderContentMarkdown(doc, { sourceLinkOf: linkOf });
    const table = renderReferenceTable(doc, {
      sourceLinkOf: linkOf,
      sourceTitleOf: (ref) => this.sourceLabel(ref),
    });
    return table ? `${body}\n\n${table}` : body;
  }

  /** 引用来源的显示标题从文档索引取（Digest 笔记标题），不显示任何内部编号。 */
  private async labelRefs(doc: ContentDocumentV3): Promise<void> {
    for (const ref of Object.values(doc.references)) {
      if (this.labels.has(ref.item_id)) continue;
      const entry = await this.docs.entryOf(digestKbId(ref.item_id));
      if (entry?.title) this.labels.set(ref.item_id, entry.title.replace(/：提炼$/, "").trim());
    }
  }

  /** 来源显示名：用该条目 Digest 的标题；本地没有时如实显示条目短号。 */
  private labels = new Map<string, string>();

  private sourceLabel(ref: ContentRefV3): string {
    return this.labels.get(ref.item_id) ?? `来源 ${ref.item_id.slice(0, 8)}`;
  }

  private async reserveKnowledgePath(kbId: string, title: string): Promise<string> {
    const s = this.deps.settings();
    const desired = knowledgeNotePath(s.knowledgeFolder, title);
    await this.docs.ensure([{ folder: s.knowledgeFolder, kind: "knowledge" }]);
    return resolveAvailableNotePath(desired, async (candidate) =>
      (await this.deps.fs.exists(candidate)) || (await this.docs.isTakenByOther(candidate, kbId)));
  }

  /** 替换 Knowledge 受管理区，保留人工区、范围说明、历史引用区与未知 frontmatter。 */
  private rewriteKnowledgeNote(current: string, managedBody: string, revision: number, reviewedAt: string): string {
    const withFm = mergeKbFrontmatter(current, [
      `kb_revision: ${revision}`,
      `kb_reviewed_at: ${reviewedAt}`,
    ]);
    return replacePartition(withFm, KNOWLEDGE_START, KNOWLEDGE_END, managedBody.trim());
  }

  /** 任务与候选状态同步；采纳后把整理结论写回 Digest 的 `kb_organize`。 */
  private async syncTaskState(p: TopicProposal, state: OrganizeTaskState): Promise<void> {
    const task = await this.tasks.get(p.task_id);
    if (!task) return;
    task.state = state;
    task.updated_at = nowIso();
    await this.tasks.put(task);
    if (state !== "applied" || !(await this.deps.fs.exists(task.digest_path))) return;
    const text = await this.deps.fs.read(task.digest_path);
    const updated = mergeKbFrontmatter(text, ["kb_organize: applied"]);
    const tagged = mergeManagedTags(updated, managedTags("digest", "applied"));
    if (tagged !== text) await this.deps.fs.write(task.digest_path, tagged);
  }

  /** 回滚：正文与引用表成对恢复；笔记再次被改时先展示差异。 */
  async rollback(proposalId: string): Promise<{ rolledBack: boolean; note: string; diff?: string }> {
    const p = await this.proposals.get(proposalId);
    if (!p || p.state !== "applied" || !p.knowledge_id || p.applied_knowledge_revision === null) {
      return { rolledBack: false, note: "该候选没有可回滚的应用记录。" };
    }
    const entry = (await this.index.read()).entries.find((e) => e.kb_id === p.knowledge_id);
    if (!entry) return { rolledBack: false, note: "主题笔记已不存在。" };
    const previousRevision = p.applied_knowledge_revision - 1;
    const previous = await this.revisions.read("knowledge", p.knowledge_id, previousRevision);
    if (previous === null) return { rolledBack: false, note: "没有找到上一版历史快照。" };

    const current = await this.deps.fs.read(entry.path);
    const currentBody = extractPartition(current, KNOWLEDGE_START, KNOWLEDGE_END) ?? "";
    const expected = extractPartition(previous, KNOWLEDGE_START, KNOWLEDGE_END) ?? "";
    const appliedRecord = await this.refs.read(p.knowledge_id, p.applied_knowledge_revision);
    // 应用后又被人工改过时不能直接覆盖：正文哈希既不是本版候选也不是旧版结果就拒绝
    const appliedHash = appliedRecord?.body_hash ?? null;
    const stillMatches = currentBody.trim() === expected.trim()
      || (appliedHash !== null && await sha256Hex(currentBody.trim()) === appliedHash);
    if (!stillMatches) {
      return {
        rolledBack: false,
        note: "当前笔记在应用后又被修改；请先人工合并，不能直接用旧快照覆盖。",
        diff: renderDiff(expected, currentBody),
      };
    }
    const restored = this.rewriteKnowledgeNote(current, expected, previousRevision, nowIso().slice(0, 10));
    await this.deps.fs.write(entry.path, restored);
    // 提交恢复记录随之作废：它只用于「崩溃重启不重复写入」，回滚后同一候选应能再次采纳
    const settings = this.deps.settings();
    const recovery = new JsonStore<Record<string, unknown>>(this.deps.fs,
      `${revisionDir(settings.systemFolder, "knowledge", p.knowledge_id)}/recovery-${p.proposal_id}.json`, () => ({}));
    const prior = await recovery.read();
    if (prior.state) {
      await recovery.write({ ...prior, state: "rolled_back", rolled_back_at: nowIso() });
    }
    if (appliedRecord) {
      const prevRefs = await this.refs.read(p.knowledge_id, previousRevision);
      if (prevRefs) await this.refs.save(p.knowledge_id, prevRefs);
      else await this.deps.fs.remove(this.refs.path(p.knowledge_id, p.applied_knowledge_revision));
    }
    p.state = "skipped";
    p.applied_at = null;
    p.applied_knowledge_revision = null;
    await this.proposals.put(p);
    await this.index.upsert(this.deps.settings().knowledgeFolder, entry.path);
    if (this.deps.onProposalsChanged) await this.deps.onProposalsChanged();
    return { rolledBack: true, note: `已回滚到 rev ${previousRevision}（正文与引用表一起恢复）。` };
  }
}

/** 程序计算的正文差异预览（新增/改动/删除，docs/23 §6.2）。
 *
 * 模型没有可靠复现旧正文时，删除会在这里显式出现；默认仍需用户采纳，绝不静默落盘。
 */
export function renderDiff(before: string, after: string): string {
  const a = before.split("\n");
  const b = after.split("\n");
  let start = 0;
  while (start < a.length && start < b.length && a[start] === b[start]) start++;
  let endA = a.length;
  let endB = b.length;
  while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) { endA--; endB--; }
  const lines: string[] = [];
  const max = Math.max(endA, endB);
  for (let i = start; i < max; i++) {
    if (i < endA && i < endB && a[i] === b[i]) continue;
    if (i < endA) lines.push(`- ${a[i]}`);
    if (i < endB) lines.push(`+ ${b[i]}`);
  }
  return lines.slice(0, 200).join("\n");
}

/** 一篇 Digest 笔记对应的 item_id：身份在 frontmatter，不再从文件名猜。 */
export function itemIdOfDigestNote(text: string): string | null {
  if (readFrontmatterValue(text, "kb_type") !== "digest") return null;
  return readFrontmatterValue(text, "kb_item_id");
}
