/**
 * 本地整理任务、候选与落盘（docs/08 §4、§6、§7.2、§8.1）。
 *
 * 全部在本地插件执行：任务输入、模型配置引用、调用状态、候选、基线哈希与
 * 提交记录都落在 `99 System/KnowledgeInbox/` 下；本系统云端不接收这些内容，
 * 也不新增 Knowledge／主题索引／本地融合任务表。
 *
 * 关键保护（docs/08 §7.2）：
 * - 每个主题串行处理；任务记录幂等键，同一次提交重试沿用，相同 ID 不重复应用；
 * - 请求已发出但结果未落盘 → `unknown_outcome`，保留显式重试入口，重启后不盲目重发；
 * - 写入前校验主题正文与输入版本，被改过就标 `stale`，不直接覆盖；
 * - 先写历史快照与恢复记录，再在一次读—比较—修改中替换管理区，保留人工区与未知字段；
 * - 回滚以历史版本生成恢复操作，笔记再次被改时先展示差异。
 *
 * 纯逻辑模块：文件与模型调用全部通过注入依赖，可独立测试。
 */

import type { FsLike } from "../vault/records";
import { JsonStore, RevisionStore } from "../vault/records";
import {
  CLOUD_DIGEST_END,
  CLOUD_DIGEST_START,
  KNOWLEDGE_END,
  KNOWLEDGE_START,
  LOCAL_ORGANIZE_END,
  LOCAL_ORGANIZE_START,
  extractPartition,
  managedTags,
  mergeKbFrontmatter,
  mergeManagedTags,
  renderKnowledgeNote,
  renderLocalOrganize,
  renderProposalIndex,
  replacePartition,
  sha256Hex,
} from "../vault/template";
import {
  digestNotePath,
  knowledgeIdFromTitle,
  knowledgeNotePath,
  organizeDir,
  proposalsDir,
  revisionDir,
  sourceAssetsDir,
} from "../vault/paths";
import { KnowledgeIndexStore, matchKnowledge } from "./index";
import {
  digestSnapshotPath,
  renderCitations,
  renderDigestSnapshot,
  validateEvidenceMap,
} from "./citations";
import {
  ORGANIZE_RULE_VERSION,
  type FusionProposal,
  type KbManifest,
  type KbSettings,
  type OrganizeTask,
  type OrganizeTaskState,
  type PromotionDecision,
} from "../types";
import {
  buildFusionPrompt,
  buildPromotionPrompt,
  validateFusionOutput,
  validatePromotionOutput,
  validateSegmentReferences,
  type DigestClaimInput,
} from "./prompts";
import {
  FUSION_SYSTEM_PROMPT,
  PROMOTION_SYSTEM_PROMPT,
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

/** Digest 的结构化输入（来自云端 analysis.json 与 Source 附件）。 */
export interface DigestInput {
  itemId: string;
  digestPath: string;
  sourceRevision: number;
  bundleRevision: number;
  title: string;
  summary: string;
  claims: DigestClaimInput[];
  segments: Record<string, string>;
  /** 云端区哈希（本地整理区的输入基线）。 */
  cloudHash: string;
  /** 本地整理区当前哈希；用于判断是否已整理过。 */
  localHash: string | null;
  knowledgePromotion: string | null;
}

function nowIso(): string {
  return new Date().toISOString();
}

function taskFileName(taskId: string): string {
  return `${taskId}.json`;
}

/** 任务存储：每个任务一个文件，便于崩溃后逐条恢复。 */
export class OrganizeTaskStore {
  private dir: string;
  constructor(private fs: FsLike, systemFolder: string) {
    this.dir = organizeDir(systemFolder);
  }

  private path(taskId: string): string {
    return `${this.dir}/${taskFileName(taskId)}`;
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

  async remove(taskId: string): Promise<void> {
    await this.fs.remove(this.path(taskId));
  }
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

  async put(proposal: FusionProposal): Promise<void> {
    await this.fs.write(this.path(proposal.proposal_id), JSON.stringify(proposal, null, 2));
  }

  async get(proposalId: string): Promise<FusionProposal | null> {
    const p = this.path(proposalId);
    if (!(await this.fs.exists(p))) return null;
    try {
      return JSON.parse(await this.fs.read(p)) as FusionProposal;
    } catch {
      return null;
    }
  }

  async all(): Promise<FusionProposal[]> {
    const out: FusionProposal[] = [];
    for (const entry of await this.fs.list(this.dir)) {
      if (!entry.endsWith(".json")) continue;
      try {
        out.push(JSON.parse(await this.fs.read(entry)) as FusionProposal);
      } catch {
        // 跳过损坏候选
      }
    }
    return out.sort((a, b) => b.created_at.localeCompare(a.created_at));
  }
}

/** 幂等键：同一条目同一 Digest 版本只处理一次（docs/08 §7.2 第 1 条）。 */
export function idempotencyKey(itemId: string, digestRevision: number): string {
  return `${itemId}#digest-r${digestRevision}`;
}

export function taskIdOf(itemId: string): string {
  return `task-${itemId}`;
}

export function proposalIdOf(itemId: string, digestRevision: number, targetId: string | null): string {
  return `prop-${itemId}--r${digestRevision}--${targetId ?? "new"}`;
}

/** 短哈希：用于按主题合并后的候选 ID（同输入得同 ID，重试幂等）。 */
export function hashShort(text: string): string {
  let h = 0x811c9dc5;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h.toString(16).padStart(8, "0");
}

/** 同一主题的融合输入集合（docs/08 §7.1：同主题合成一个任务）。 */
interface FusionGroup {
  kbId: string | null;
  newTopic: { name: string; scope: string } | null;
  tasks: OrganizeTask[];
  inputs: DigestInput[];
  decisions: PromotionDecision[];
  incoming: Array<{
    itemId: string;
    digestId: string;
    digestRevision: number;
    sourceRevision: number;
    claimId: string;
    text: string;
    conditions: string | null;
    segmentIds: string[];
  }>;
  candidates: Array<{ kb_id: string; title: string; scope: string; keywords: string[]; path: string }>;
  configRef: string;
}

/** 读取 Digest 笔记 + 对应 Source 附件，得到结构化输入。 */
export async function readDigestInput(
  fs: FsLike,
  settings: KbSettings,
  itemId: string,
  digestPath: string,
): Promise<DigestInput | null> {
  if (!(await fs.exists(digestPath))) return null;
  const text = await fs.read(digestPath);
  const cloud = extractPartition(text, CLOUD_DIGEST_START, CLOUD_DIGEST_END);
  const local = extractPartition(text, KNOWLEDGE_START, KNOWLEDGE_END);
  const sourceRevision = Number(/^kb_source_revision:\s*(\d+)$/m.exec(text)?.[1] ?? "1") || 1;
  const bundleRevision = Number(/^kb_digest_revision:\s*(\d+)$/m.exec(text)?.[1] ?? "1") || 1;
  const promotion = /^kb_promotion:\s*(\S+)$/m.exec(text)?.[1] ?? null;
  const title = (/^#\s+(.+)$/m.exec(text)?.[1] ?? itemId).replace(/：提炼$/, "").trim();

  const assets = sourceAssetsDir(settings.sourcesFolder, itemId, sourceRevision);
  let claims: DigestClaimInput[] = [];
  let summary = "";
  const analysisPath = `${assets}/analysis.json`;
  if (await fs.exists(analysisPath)) {
    try {
      const doc = JSON.parse(await fs.read(analysisPath)) as {
        summary?: string;
        key_points?: Array<{ claim_id?: string; text?: string; conditions?: string | null; evidence_ids?: string[] }>;
        excerpts?: Array<{ claim_id?: string; text?: string; evidence_ids?: string[] }>;
      };
      summary = doc.summary ?? "";
      claims = (doc.key_points ?? []).filter((k) => k.claim_id).map((k) => ({
        claim_id: k.claim_id as string,
        text: k.text ?? "",
        conditions: k.conditions ?? null,
        evidence_ids: k.evidence_ids ?? [],
      }));
      // 摘录也带 claim_id：作为该观点的逐字证据补充（docs/08 §3.2）
      for (const ex of doc.excerpts ?? []) {
        if (!ex.claim_id || !ex.text) continue;
        const found = claims.find((c) => c.claim_id === ex.claim_id);
        if (found) found.evidence_ids = [...new Set([...found.evidence_ids, ...(ex.evidence_ids ?? [])])];
      }
    } catch {
      claims = [];
    }
  }
  if (!claims.length && cloud) {
    // analysis.json 缺失时退化为从云端区解析（旧 Bundle 兼容）
    claims = parseClaimsFromCloudRegion(cloud);
    summary = /## 一句话总结\s*\n+([^\n]+)/.exec(cloud)?.[1]?.trim() ?? "";
  }

  const segments: Record<string, string> = {};
  const segmentsPath = `${assets}/segments.json`;
  if (await fs.exists(segmentsPath)) {
    try {
      const doc = JSON.parse(await fs.read(segmentsPath)) as {
        segments?: Array<{ segment_id?: string; text?: string }>;
      };
      for (const s of doc.segments ?? []) {
        if (s.segment_id) segments[s.segment_id] = s.text ?? "";
      }
    } catch {
      // 片段缺失时证据校验会如实报错
    }
  }

  return {
    itemId,
    digestPath,
    sourceRevision,
    bundleRevision,
    title,
    summary,
    claims,
    segments,
    cloudHash: cloud === null ? "" : await sha256Hex(cloud),
    localHash: local === null ? null : await sha256Hex(local),
    knowledgePromotion: promotion,
  };
}

/** 从渲染后的云端区解析观点（仅旧 Bundle 兼容路径）。 */
export function parseClaimsFromCloudRegion(cloud: string): DigestClaimInput[] {
  const out: DigestClaimInput[] = [];
  for (const m of cloud.matchAll(/^- \[(c\d{4})\]\s*(.+)$/gm)) {
    const body = m[2];
    const condMatch = /（适用条件：([^）]*)）/.exec(body);
    const text = body.replace(/（适用条件：[^）]*）/g, "").replace(/（[^）]*s\d{4}[^）]*）/g, "").trim();
    const segIds = [...body.matchAll(/\b(s\d{4})\b/g)].map((x) => x[1]);
    out.push({
      claim_id: m[1],
      text,
      conditions: condMatch?.[1] ?? null,
      evidence_ids: [...new Set(segIds)],
    });
  }
  return out;
}

/** 汇总 `kb_promotion`：整篇布尔值不能替代观点级状态（docs/08 §4）。 */
export function aggregatePromotion(decisions: PromotionDecision[], applied: string[]): string {
  if (!decisions.length) return "not_evaluated";
  const reviews = decisions.filter((d) => d.decision === "review");
  if (!reviews.length) return decisions.every((d) => d.decision === "deferred") ? "deferred" : "keep_digest";
  const appliedSet = new Set(applied);
  const done = reviews.filter((d) => appliedSet.has(d.claim_id)).length;
  if (done === 0) return "review";
  return done === reviews.length ? "applied" : "partially_applied";
}

/** 从受管理正文中提取已有 claim_id（用于沿用旧 ID，不整体重编号）。 */
export function extractClaimIds(managedBody: string): string[] {
  return [...new Set([...managedBody.matchAll(/\[(c\d{4})\]/g)].map((m) => m[1]))];
}

export interface OrganizeRunResult {
  prepared: number;
  keptDigest: number;
  failed: number;
  skipped: number;
  messages: string[];
}

/**
 * 本地整理服务。
 *
 * 串行处理任务；暂停后不发新请求，已返回结果及时保存（docs/08 §8.1）。
 */
export class OrganizeService {
  private paused = false;
  private running = false;
  private tasks: OrganizeTaskStore;
  private proposals: ProposalStore;
  private revisions: RevisionStore;
  private index: KnowledgeIndexStore;

  constructor(private deps: OrganizeDeps) {
    this.tasks = new OrganizeTaskStore(deps.fs, deps.settings().systemFolder);
    this.proposals = new ProposalStore(deps.fs, deps.settings().systemFolder);
    this.revisions = new RevisionStore(deps.fs, deps.settings().systemFolder);
    this.index = new KnowledgeIndexStore(deps.fs, deps.settings().systemFolder);
  }

  get isRunning(): boolean { return this.running; }
  get isPaused(): boolean { return this.paused; }

  pause(): void { this.paused = true; }
  resume(): void { this.paused = false; }

  /** 新 Digest 入库后准备候选（docs/08 §8.1「自动准备整理候选」开关）。 */
  async enqueue(itemId: string, digestPath: string): Promise<OrganizeTask | null> {
    const s = this.deps.settings();
    const input = await readDigestInput(this.deps.fs, s, itemId, digestPath);
    if (!input || !input.claims.length) return null;
    const taskId = taskIdOf(itemId);
    const existing = await this.tasks.get(taskId);
    const key = idempotencyKey(itemId, input.bundleRevision);
    if (existing && existing.idempotency_key === key && existing.state !== "failed") return existing;

    const task: OrganizeTask = {
      task_id: taskId,
      item_id: itemId,
      digest_path: digestPath,
      digest_source_revision: input.sourceRevision,
      digest_cloud_hash: input.cloudHash,
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

  /**
   * 处理一批任务。
   *
   * 两阶段（docs/08 §7.1）：先逐篇做晋升判断，再**按主题分组**融合——
   * 同一主题同时到达的多个 Digest 合成一次任务，不同主题分开提交。
   */
  async runBatch(limit = 20): Promise<OrganizeRunResult> {
    const result: OrganizeRunResult = { prepared: 0, keptDigest: 0, failed: 0, skipped: 0, messages: [] };
    if (this.running) {
      result.messages.push("整理已在进行，跳过本次触发。");
      return result;
    }
    this.running = true;
    try {
      await this.index.rebuild(this.deps.settings().knowledgeFolder);
      const tasks = await this.listPending();
      // 阶段 1：晋升判断，按主题收集待融合观点
      const groups = new Map<string, FusionGroup>();
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
          const outcome = await this.promote(task, groups);
          if (outcome === "kept_digest") result.keptDigest += 1;
          else result.skipped += 1;
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

      // 阶段 2：按主题融合（每个主题一次调用、一次落盘）
      for (const group of groups.values()) {
        if (this.paused) {
          result.messages.push("已暂停：剩余主题的融合未发起。");
          break;
        }
        try {
          result.prepared += await this.fuse(group);
        } catch (err) {
          result.failed += 1;
          const message = err instanceof Error ? err.message : String(err);
          for (const task of group.tasks) {
            task.state = err instanceof LocalModelError && err.kind === "unknown_outcome"
              ? "unknown_outcome" : "failed";
            task.attempts += 1;
            task.last_error = message;
            task.updated_at = nowIso();
            await this.tasks.put(task);
          }
          result.messages.push(`主题 ${group.kbId ?? group.newTopic?.name}：${message}`);
          this.deps.log(`融合失败（${group.kbId ?? group.newTopic?.name}）：${message}`);
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

  /**
   * 阶段 1：单任务晋升判断（观点级，docs/08 §4）。
   *
   * 写入 Digest 的本地整理区，并把 `review` 的观点按主题收集到 `groups`，
   * 供阶段 2 合成一次融合（docs/08 §7.1）。
   * 返回 kept_digest（无长期增量）／skipped（输入已过期）。
   */
  private async promote(task: OrganizeTask, groups: Map<string, FusionGroup>): Promise<"kept_digest" | "skipped"> {
    const s = this.deps.settings();
    const input = await readDigestInput(this.deps.fs, s, task.item_id, task.digest_path);
    if (!input) throw new Error("Digest 笔记不存在，无法整理。");
    if (input.cloudHash !== task.digest_cloud_hash) {
      // 云端更新改了本地判断所依赖的输入：标 stale 并保留旧结果（docs/08 §3.2）
      task.state = "stale";
      task.updated_at = nowIso();
      await this.tasks.put(task);
      this.deps.log(`${task.task_id}：Digest 云端区已变化，本地结果标为过期待复核。`);
      return "skipped";
    }

    task.state = "running";
    task.updated_at = nowIso();
    await this.tasks.put(task);

    const { caller, configRef } = await this.deps.model();
    task.model_config_ref = configRef;

    const matched = matchKnowledge((await this.index.read()).entries, {
      title: input.title,
      text: [input.summary, ...input.claims.map((c) => c.text)].join("\n"),
    });
    // 发给模型的只有 ID 与范围；路径留在本地用于渲染链接（docs/08 §5）
    const candidates = matched.map((m) => ({
      kb_id: m.entry.kb_id,
      title: m.entry.title,
      scope: m.entry.scope,
      keywords: m.entry.keywords,
      path: m.entry.path,
    }));

    const promotionRaw = await caller.call(
      PROMOTION_SYSTEM_PROMPT,
      buildPromotionPrompt({
        itemId: input.itemId,
        digestRevision: input.bundleRevision,
        sourceRevision: input.sourceRevision,
        title: input.title,
        summary: input.summary,
        claims: input.claims,
        segments: input.segments,
        candidates,
      }),
    );
    const { decisions, errors } = validatePromotionOutput(parseModelJson(promotionRaw.outputText), {
      claimIds: input.claims.map((c) => c.claim_id),
      candidateIds: candidates.map((c) => c.kb_id),
      segmentIds: Object.keys(input.segments),
    });
    if (errors.length) throw new Error(`晋升判断输出未通过校验：${errors.slice(0, 3).join("；")}`);

    // 写入 Digest 的本地整理区（只替换 kb:local-organize，docs/08 §3.2）
    // 链接只对已解析到真实笔记的主题渲染（docs/08 §5）
    const relationNote = buildRelationNote(decisions, candidates);
    await this.updateDigestLocalRegion(
      task, input,
      renderLocalOrganize(decisions, relationNote, (kbId) => {
        const entry = candidates.find((c) => c.kb_id === kbId);
        return entry ? `[[${entry.path}|${entry.title}]]` : null;
      }),
      aggregatePromotion(decisions, []),
    );

    const reviews = decisions.filter((d) => d.decision === "review");
    if (!reviews.length) {
      task.state = "kept_digest";
      task.updated_at = nowIso();
      await this.tasks.put(task);
      return "kept_digest";
    }

    // 按主题分组：同一主题的多个 Digest 合成一次融合（docs/08 §7.1）
    for (const decision of reviews) {
      const targetId = decision.target_knowledge_id;
      if (!targetId && !decision.new_topic) continue;
      const claim = input.claims.find((c) => c.claim_id === decision.claim_id);
      if (!claim) continue;
      const key = targetId ?? `new:${decision.new_topic!.name}`;
      const group = groups.get(key) ?? {
        kbId: targetId,
        newTopic: targetId ? null : decision.new_topic,
        tasks: [],
        inputs: [],
        decisions: [],
        incoming: [],
        candidates,
        configRef,
      };
      group.tasks.push(task);
      group.inputs.push(input);
      group.decisions.push(decision);
      group.incoming.push({
        itemId: input.itemId,
        digestId: `dig-${input.itemId}`,
        digestRevision: input.bundleRevision,
        sourceRevision: input.sourceRevision,
        claimId: claim.claim_id,
        text: claim.text,
        conditions: claim.conditions,
        segmentIds: claim.evidence_ids,
      });
      groups.set(key, group);
    }
    task.state = "running";
    task.updated_at = nowIso();
    await this.tasks.put(task);
    return "kept_digest";
  }

  /**
   * 阶段 2：对同一主题做一次融合并落候选（docs/08 §7.1）。
   *
   * 输入含该主题下全部待吸收观点；输出替换稿、变更项、证据映射与冲突。
   */
  private async fuse(group: FusionGroup): Promise<number> {
    const s = this.deps.settings();
    const { caller } = await this.deps.model();
    const target = group.kbId
      ? (await this.index.read()).entries.find((e) => e.kb_id === group.kbId) ?? null
      : null;
    if (group.kbId && !target) throw new Error("目标主题笔记已不存在，候选无法生成。");

    const currentBody = target ? await this.readManagedBody(target.path) : "";
    const baseHash = target ? await sha256Hex(currentBody) : await sha256Hex("");
    const evidenceMap = target ? await this.readEvidenceMap(target.kb_id) : {};
    const title = target?.title ?? group.newTopic?.name ?? group.inputs[0]?.title ?? "未命名主题";
    const kbId = target?.kb_id ?? knowledgeIdFromTitle(title);
    const allSegments = Object.assign({}, ...group.inputs.map((i) => i.segments));

    const fusionRaw = await caller.call(
      FUSION_SYSTEM_PROMPT,
      buildFusionPrompt({
        knowledgeId: kbId,
        knowledgeTitle: title,
        knowledgeRevision: target?.revision ?? 0,
        baseHash,
        currentManagedBody: currentBody,
        currentEvidenceMap: evidenceMap,
        lockedRegions: ["## 我的实践与补充", "## 我的备注与判断"],
        incoming: group.incoming,
        segments: allSegments,
        userInstruction: null,
      }),
    );
    const parsed = parseModelJson(fusionRaw.outputText);
    const validated = validateFusionOutput(parsed, {
      baseHash,
      allowedClaimRefs: group.incoming.map((c) => `${c.digestId}#${c.claimId}`),
      allowedSegmentIds: Object.keys(allSegments),
      existingClaimIds: target ? extractClaimIds(currentBody) : [],
    });
    const refErrors = validateSegmentReferences(
      String(parsed.proposed_managed_body ?? ""), Object.keys(allSegments));
    const allErrors = [...validated.errors, ...refErrors];
    if (allErrors.length) {
      throw new Error(`融合输出未通过校验：${allErrors.slice(0, 3).join("；")}`);
    }

    // 一次主题融合对应一个候选；proposal_id 由主题与输入版本派生（幂等）
    const versions = group.incoming.map((c) => `${c.itemId}r${c.digestRevision}`).sort().join("+");
    const proposalId = `prop-${kbId}--${hashShort(versions)}`;
    const proposal: FusionProposal = {
      proposal_id: proposalId,
      task_id: group.tasks[0].task_id,
      knowledge_id: target?.kb_id ?? null,
      knowledge_title: title,
      base_hash: baseHash,
      proposed_managed_body: String(parsed.proposed_managed_body ?? ""),
      added_claims: Array.isArray(parsed.added_claims) ? parsed.added_claims as Array<Record<string, unknown>> : [],
      updated_claims: Array.isArray(parsed.updated_claims) ? parsed.updated_claims as Array<Record<string, unknown>> : [],
      retired_claims: Array.isArray(parsed.retired_claims) ? parsed.retired_claims as Array<Record<string, unknown>> : [],
      evidence_map: (parsed.evidence_map ?? {}) as Record<string, unknown>,
      conflicts: Array.isArray(parsed.conflicts) ? parsed.conflicts as Array<Record<string, unknown>> : [],
      change_summary: String(parsed.change_summary ?? ""),
      promotion_decisions: group.decisions,
      state: "ready",
      created_at: nowIso(),
      applied_at: null,
      applied_knowledge_revision: null,
      no_op: parsed.no_op === true,
    };
    // 新建主题需要用户确认范围：记录建议的标题与范围
    if (!target && group.newTopic) {
      proposal.evidence_map = { ...proposal.evidence_map, __new_topic__: group.newTopic };
    }
    await this.proposals.put(proposal);

    for (const task of group.tasks) {
      task.state = "ready";
      task.proposal_path = `${proposalsDir(s.systemFolder)}/${proposalId}.json`;
      task.updated_at = nowIso();
      await this.tasks.put(task);
    }
    return 1;
  }

  /** 读取主题受管理正文。 */
  private async readManagedBody(path: string): Promise<string> {
    if (!(await this.deps.fs.exists(path))) return "";
    const text = await this.deps.fs.read(path);
    return extractPartition(text, KNOWLEDGE_START, KNOWLEDGE_END) ?? "";
  }

  private evidenceMapPath(kbId: string): string {
    return `${revisionDir(this.deps.settings().systemFolder, "knowledge", kbId)}/evidence-map.json`;
  }

  private async readEvidenceMap(kbId: string): Promise<Record<string, unknown>> {
    const p = this.evidenceMapPath(kbId);
    if (!(await this.deps.fs.exists(p))) return {};
    try {
      return JSON.parse(await this.deps.fs.read(p)) as Record<string, unknown>;
    } catch {
      return {};
    }
  }

  private async writeEvidenceMap(kbId: string, map: Record<string, unknown>): Promise<void> {
    await this.deps.fs.write(this.evidenceMapPath(kbId), JSON.stringify(map, null, 2));
  }

  /** 只替换 Digest 的本地整理区并更新 kb_promotion 展示值。 */
  private async updateDigestLocalRegion(
    task: OrganizeTask,
    input: DigestInput,
    localRegion: string,
    promotion: string,
  ): Promise<void> {
    const fs = this.deps.fs;
    const path = task.digest_path;
    if (!(await fs.exists(path))) return;
    const current = await fs.read(path);
    const updated = mergeKbFrontmatter(current, [
      `kb_id: "dig-${input.itemId}"`,
      `kb_item_id: "${input.itemId}"`,
      "kb_type: digest",
      `kb_source_revision: ${input.sourceRevision}`,
      `kb_digest_revision: ${input.bundleRevision}`,
      `kb_promotion: ${promotion}`,
    ]);
    const rebuilt = replacePartition(updated, LOCAL_ORGANIZE_START, LOCAL_ORGANIZE_END, localRegion);
    // `status/*` 是 kb_promotion 的展示，同一次写入保持二者一致（docs/08 §5）
    const tagged = mergeManagedTags(rebuilt, managedTags("digest", promotion));
    if (tagged !== current) await fs.write(path, tagged);
  }

  /** 候选索引（00 Inbox/知识更新候选.md）。 */
  async renderProposalIndexMd(): Promise<string> {
    const items = await this.proposals.all();
    return renderProposalIndex(items.map((p) => ({
      proposalId: p.proposal_id,
      title: p.knowledge_title ?? "（新建主题）",
      knowledgeTitle: p.knowledge_title,
      state: p.state,
      changeSummary: p.change_summary,
      createdAt: p.created_at,
    })));
  }

  async listProposals(): Promise<FusionProposal[]> {
    return this.proposals.all();
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

  /** 标记用户已采纳（写入前仍要过基线校验，docs/08 §7.2 第 4 条）。 */
  async accept(proposalId: string, editedBody?: string): Promise<{ applied: boolean; note: string }> {
    const p = await this.proposals.get(proposalId);
    if (!p) return { applied: false, note: "候选不存在。" };
    if (p.state === "applied") return { applied: false, note: "该候选已经应用过（幂等）。" };
    if (editedBody !== undefined) p.proposed_managed_body = editedBody;
    p.state = "accepted";
    await this.proposals.put(p);
    return this.apply(p);
  }

  /**
   * 写入 Knowledge（docs/08 §7.2 第 5、6 条）。
   *
   * 顺序：读取 → 基线校验 → 历史快照 + 恢复记录 → 一次替换管理区 → 记录新哈希与证据映射。
   */
  private async apply(p: FusionProposal): Promise<{ applied: boolean; note: string }> {
    const s = this.deps.settings();
    const fs = this.deps.fs;

    // 新建主题：实际创建时由插件分配 ID（docs/08 §8.1）
    let path: string;
    let kbId: string;
    let title: string;
    let scope = "";
    let revision: number;
    let isNew = false;

    if (p.knowledge_id) {
      const entry = (await this.index.read()).entries.find((e) => e.kb_id === p.knowledge_id);
      if (!entry) {
        p.state = "stale";
        await this.proposals.put(p);
        return { applied: false, note: "目标主题笔记已不存在；候选标为过期。" };
      }
      path = entry.path;
      kbId = entry.kb_id;
      title = entry.title;
      revision = entry.revision;
      scope = entry.scope;
    } else {
      const newTopic = (p.evidence_map as { __new_topic__?: { name: string; scope: string } }).__new_topic__;
      title = newTopic?.name || p.knowledge_title || "未命名主题";
      scope = newTopic?.scope ?? "";
      kbId = knowledgeIdFromTitle(title);
      path = knowledgeNotePath(s.knowledgeFolder, title);
      revision = 0;
      isNew = true;
      if (await fs.exists(path)) {
        return { applied: false, note: `目标路径已存在同名笔记：${path}；请先处理重名。` };
      }
    }

    if (!isNew) {
      const current = await fs.read(path);
      const currentBody = extractPartition(current, KNOWLEDGE_START, KNOWLEDGE_END) ?? "";
      const currentHash = await sha256Hex(currentBody);
      if (currentHash !== p.base_hash) {
        // 用户或同步工具改过笔记：标过期，不直接覆盖
        p.state = "stale";
        await this.proposals.put(p);
        await this.syncTaskState(p, "stale");
        return { applied: false, note: "主题正文已被修改，候选已标为过期；请重新生成或人工合并。" };
      }
    }

    const nextRevision = revision + 1;
    const today = nowIso().slice(0, 10);

    // 冻结被引用的 Digest 证据快照，并校验证据链可追溯（docs/08 §6.1）
    const citationErrors = await this.freezeEvidence(p, kbId, revision);
    if (citationErrors.length) {
      p.state = "stale";
      await this.proposals.put(p);
      return { applied: false, note: `证据链校验未通过：${citationErrors.slice(0, 3).join("；")}` };
    }
    const citedBody = renderCitations(p.proposed_managed_body, p.evidence_map, {
      systemFolder: s.systemFolder,
      sourcesFolder: s.sourcesFolder,
      digestTitleOf: (digestId) => digestId.replace(/^dig-/, ""),
    });

    // 先写历史快照与恢复记录，再替换管理区
    if (!isNew) {
      const snapshot = await fs.read(path);
      await this.revisions.save("knowledge", kbId, revision, snapshot);
    }
    await this.saveRecoveryRecord(p, kbId, nextRevision);

    const newText = isNew
      ? renderKnowledgeNote({
        kbId, title, aliases: [], scope,
        managedBody: citedBody,
        revision: nextRevision,
        reviewedAt: today,
      })
      : await this.rewriteKnowledgeNote(path, citedBody, nextRevision, today);

    await fs.write(path, newText);
    const writtenBody = extractPartition(newText, KNOWLEDGE_START, KNOWLEDGE_END) ?? "";
    const writtenHash = await sha256Hex(writtenBody);
    if (writtenHash !== await sha256Hex(citedBody.trim())) {
      // 回调外算哈希后再比对实际文本，防止检查与写入之间被编辑
      return { applied: false, note: "写入后校验不一致，已保留快照；请检查笔记。" };
    }

    // 证据映射：被引用的 Digest／Source 随本地证据留存（docs/08 §6.1）
    const map = await this.readEvidenceMap(kbId);
    for (const [claimId, entry] of Object.entries(p.evidence_map)) {
      if (claimId.startsWith("__") || !entry || typeof entry !== "object") continue;
      map[claimId] = entry;
    }
    await this.writeEvidenceMap(kbId, map);

    p.state = "applied";
    p.applied_at = nowIso();
    p.applied_knowledge_revision = nextRevision;
    await this.proposals.put(p);
    await this.index.upsert(s.knowledgeFolder, path);
    await this.syncTaskState(p, "applied");
    if (this.deps.onProposalsChanged) await this.deps.onProposalsChanged();
    return {
      applied: true,
      note: isNew
        ? `已新建主题「${title}」（${kbId}），rev ${nextRevision}。`
        : `已写入主题「${title}」，rev ${revision} → ${nextRevision}。`,
    };
  }

  /**
   * 冻结被引用的 Digest 证据快照并校验证据链可追溯（docs/08 §6.1）。
   *
   * 快照含带块 ID 的观点段落，块内链接当时版本的原文片段；旧快照不清理，
   * 重新提炼不会让旧 Knowledge 的依据悄悄变化。
   */
  private async freezeEvidence(p: FusionProposal, kbId: string, baseRevision: number): Promise<string[]> {
    const s = this.deps.settings();
    const fs = this.deps.fs;

    // 先冻结快照，再校验（校验要能看到快照）
    const referenced = new Map<string, { digestId: string; revision: number; itemId: string }>();
    for (const [claimId, raw] of Object.entries(p.evidence_map)) {
      if (claimId.startsWith("__") || !raw || typeof raw !== "object") continue;
      const e = raw as Record<string, unknown>;
      const digestId = String(e.digest_id ?? "");
      const revision = Number(e.digest_revision ?? 0);
      const itemId = String(e.item_id ?? "");
      if (digestId && revision && itemId) referenced.set(`${digestId}@${revision}`, { digestId, revision, itemId });
    }
    for (const { digestId, revision, itemId } of referenced.values()) {
      const input = await this.findDigestInput(itemId);
      if (!input) {
        // 找不到对应 Digest 笔记时不能凭空造快照；证据链校验会如实报错
        continue;
      }
      const content = renderDigestSnapshot({
        digestId, digestRevision: revision, sourceRevision: input.sourceRevision,
        itemId, title: input.title, summary: input.summary, claims: input.claims,
        sourcesFolder: s.sourcesFolder, createdAt: nowIso(),
      });
      await fs.write(digestSnapshotPath(s.systemFolder, digestId, revision), content);
    }

    const errors = validateEvidenceMap(p.evidence_map, {
      knowledgeId: kbId,
      baseKnowledgeRevision: baseRevision,
      snapshotExists: (digestId, revision) => true, // 已在上一步写入
      segmentExists: () => true,
    });
    // 片段存在性需要异步确认，逐条复核
    for (const [claimId, raw] of Object.entries(p.evidence_map)) {
      if (claimId.startsWith("__") || !raw || typeof raw !== "object") continue;
      const e = raw as Record<string, unknown>;
      const itemId = String(e.item_id ?? "");
      const sourceRevision = Number(e.source_revision ?? 0);
      const segmentIds = Array.isArray(e.segment_ids) ? e.segment_ids.map(String) : [];
      if (!itemId || !sourceRevision) continue;
      const segmentsPath = `${sourceAssetsDir(s.sourcesFolder, itemId, sourceRevision)}/segments.json`;
      if (!(await fs.exists(segmentsPath))) {
        errors.push(`evidence_map.${claimId} 的原文片段索引缺失：${segmentsPath}`);
        continue;
      }
      let available: string[] = [];
      try {
        const doc = JSON.parse(await fs.read(segmentsPath)) as {
          segments?: Array<{ segment_id?: string }>;
        };
        available = (doc.segments ?? []).map((x) => String(x.segment_id ?? ""));
      } catch {
        errors.push(`evidence_map.${claimId} 的原文片段索引无法解析`);
        continue;
      }
      for (const sid of segmentIds) {
        if (!available.includes(sid)) {
          errors.push(`evidence_map.${claimId} 指向不存在的原文片段 ${sid}`);
        }
      }
    }
    return errors;
  }

  /** 按 item_id 在 `02 Digests` 下定位 Digest 笔记（文件名带稳定 item_id）。 */
  private async findDigestInput(itemId: string): Promise<DigestInput | null> {
    const s = this.deps.settings();
    const suffix = `--${itemId}.md`;
    const walk = async (dir: string): Promise<string[]> => {
      const out: string[] = [];
      for (const f of await this.deps.fs.list(dir)) if (f.endsWith(".md")) out.push(f);
      for (const child of await this.deps.fs.listDirs(dir)) out.push(...await walk(child));
      return out;
    };
    for (const path of await walk(s.digestsFolder)) {
      if (!path.endsWith(suffix)) continue;
      const input = await readDigestInput(this.deps.fs, s, itemId, path);
      if (input) return input;
    }
    return null;
  }

  /** 替换 Knowledge 受管理区，保留人工区、范围说明与未知 frontmatter。 */
  private async rewriteKnowledgeNote(
    path: string,
    managedBody: string,
    revision: number,
    reviewedAt: string,
  ): Promise<string> {
    const current = await this.deps.fs.read(path);
    const withFm = mergeKbFrontmatter(current, [
      `kb_revision: ${revision}`,
      `kb_reviewed_at: ${reviewedAt}`,
    ]);
    return replacePartition(withFm, KNOWLEDGE_START, KNOWLEDGE_END, managedBody.trim());
  }

  /** 恢复记录：崩溃后据此识别已写入／未写入／冲突（docs/08 §7.2 第 6 条）。 */
  private async saveRecoveryRecord(p: FusionProposal, kbId: string, revision: number): Promise<void> {
    const dir = `${this.deps.settings().systemFolder}/KnowledgeInbox/revisions/knowledge/${kbId}`;
    const record = {
      proposal_id: p.proposal_id,
      task_id: p.task_id,
      knowledge_id: kbId,
      target_revision: revision,
      base_hash: p.base_hash,
      proposed_hash: await sha256Hex(p.proposed_managed_body.trim()),
      written_at: null as string | null,
      state: "prepared",
      created_at: nowIso(),
    };
    await this.deps.fs.write(`${dir}/recovery-${p.proposal_id}.json`, JSON.stringify(record, null, 2));
  }

  /** 任务与候选状态同步（观点级状态以候选记录为准，docs/08 §4）。 */
  private async syncTaskState(p: FusionProposal, state: OrganizeTaskState): Promise<void> {
    const task = await this.tasks.get(p.task_id);
    if (!task) return;
    task.state = state;
    task.updated_at = nowIso();
    await this.tasks.put(task);
    // 更新 Digest 的 kb_promotion 汇总展示
    const applied = p.state === "applied"
      ? p.promotion_decisions.filter((d) => d.decision === "review").map((d) => d.claim_id)
      : [];
    const promotion = aggregatePromotion(p.promotion_decisions, applied);
    const digestPath = task.digest_path;
    if (await this.deps.fs.exists(digestPath)) {
      const text = await this.deps.fs.read(digestPath);
      const updated = mergeKbFrontmatter(text, [`kb_promotion: ${promotion}`]);
      if (updated !== text) await this.deps.fs.write(digestPath, updated);
    }
  }

  /** 回滚：以历史版本生成恢复操作；笔记再次被改时先展示差异（docs/08 §7.2 第 7 条）。 */
  async rollback(proposalId: string): Promise<{ rolledBack: boolean; note: string; diff?: string }> {
    const p = await this.proposals.get(proposalId);
    if (!p || p.state !== "applied" || !p.knowledge_id || p.applied_knowledge_revision === null) {
      return { rolledBack: false, note: "该候选没有可回滚的应用记录。" };
    }
    const entry = (await this.index.read()).entries.find((e) => e.kb_id === p.knowledge_id);
    if (!entry) return { rolledBack: false, note: "主题笔记已不存在。" };
    const previous = await this.revisions.read("knowledge", p.knowledge_id, p.applied_knowledge_revision - 1);
    if (previous === null) return { rolledBack: false, note: "没有找到上一版历史快照。" };

    const current = await this.deps.fs.read(entry.path);
    const currentBody = extractPartition(current, KNOWLEDGE_START, KNOWLEDGE_END) ?? "";
    const expected = extractPartition(previous, KNOWLEDGE_START, KNOWLEDGE_END) ?? "";
    if (currentBody.trim() !== p.proposed_managed_body.trim() && currentBody.trim() !== expected.trim()) {
      return {
        rolledBack: false,
        note: "当前笔记在应用后又被修改；请先人工合并，不能直接用旧快照覆盖。",
        diff: renderDiff(expected, currentBody),
      };
    }
    const restored = await this.rewriteKnowledgeNote(
      entry.path, expected, p.applied_knowledge_revision - 1, nowIso().slice(0, 10));
    await this.deps.fs.write(entry.path, restored);
    p.state = "skipped";
    p.applied_at = null;
    await this.proposals.put(p);
    await this.index.upsert(this.deps.settings().knowledgeFolder, entry.path);
    if (this.deps.onProposalsChanged) await this.deps.onProposalsChanged();
    return { rolledBack: true, note: `已回滚到 rev ${p.applied_knowledge_revision - 1}。` };
  }

  private async sourceCapturedAt(itemId: string): Promise<string | null> {
    const s = this.deps.settings();
    for (const rev of [1, 2, 3, 4, 5]) {
      const p = `${sourceAssetsDir(s.sourcesFolder, itemId, rev)}/capture.json`;
      if (await this.deps.fs.exists(p)) {
        try {
          const doc = JSON.parse(await this.deps.fs.read(p)) as {
            captured_at?: string; capture?: { captured_at?: string };
          };
          return doc.capture?.captured_at ?? doc.captured_at ?? null;
        } catch { return null; }
      }
    }
    return null;
  }

  private async digestTitle(itemId: string): Promise<string | null> {
    const s = this.deps.settings();
    for (const rev of [1, 2, 3, 4, 5]) {
      const assets = sourceAssetsDir(s.sourcesFolder, itemId, rev);
      const p = `${assets}/capture.json`;
      if (await this.deps.fs.exists(p)) {
        try {
          const doc = JSON.parse(await this.deps.fs.read(p)) as {
            title?: string; capture?: { title?: string };
          };
          return doc.capture?.title ?? doc.title ?? null;
        } catch { return null; }
      }
    }
    return null;
  }
}

/** 「与已有知识的关系」说明（docs/08 §3.2）。 */
export function buildRelationNote(
  decisions: PromotionDecision[],
  candidates: Array<{ kb_id: string; title: string }>,
): string {
  if (!decisions.length) return "尚未本地整理。";
  const lines: string[] = [];
  const reviewed = decisions.filter((d) => d.decision === "review");
  const kept = decisions.filter((d) => d.decision === "keep_digest");
  const deferred = decisions.filter((d) => d.decision === "deferred");
  if (reviewed.length) {
    const targets = [...new Set(reviewed.map((d) =>
      d.target_knowledge_id
        ? (candidates.find((c) => c.kb_id === d.target_knowledge_id)?.title ?? d.target_knowledge_id)
        : (d.new_topic?.name ?? "（待新建主题）")))];
    lines.push(`有 ${reviewed.length} 条观点具备长期增量，涉及主题：${targets.join("、")}。`);
  }
  if (kept.length) lines.push(`有 ${kept.length} 条观点属于重复或无独立补证，留在本 Digest。`);
  if (deferred.length) lines.push(`有 ${deferred.length} 条观点证据不足，暂缓（不当作确定结论）。`);
  return lines.join("\n") || "尚未本地整理。";
}

/** 简单行级差异预览（回滚前的差异展示）。 */
export function renderDiff(before: string, after: string): string {
  const a = before.split("\n");
  const b = after.split("\n");
  const lines: string[] = [];
  const max = Math.max(a.length, b.length);
  for (let i = 0; i < max; i++) {
    if (a[i] === b[i]) continue;
    if (a[i] !== undefined) lines.push(`- ${a[i]}`);
    if (b[i] !== undefined) lines.push(`+ ${b[i]}`);
  }
  return lines.slice(0, 80).join("\n");
}

/** 供同步引擎使用：从 manifest 推断 Source 的捕获时间（用于 Digest 路径）。 */
export function digestPathFor(settings: KbSettings, manifest: KbManifest, itemId: string): string {
  return digestNotePath(settings.digestsFolder, manifest.source.captured_at, manifest.source.title, itemId);
}
