/**
 * 证据链引用解析（docs/08 §6.1）。
 *
 * 要求：每个结论可两跳回到当时原文
 * ```
 * knowledge_id + knowledge_revision + claim_id
 *   → digest_id + digest_revision + digest_claim_id
 *   → item_id + source_revision + segment_id
 * ```
 *
 * - 冻结快照放在 `99 System/KnowledgeInbox/revisions/digests/<id>/r000001.md`，
 *   每条观点带块 ID `^c0001`，块内再链接 `01 Sources/_assets/<item>/source-000001/normalized#^s0001`。
 * - Knowledge 正文里显示的是易读链接「依据：Digest 标题」，用户无需阅读系统路径。
 * - **所有引用由本地 ID 解析器生成，禁止直接信任模型给出的路径**；
 *   代码校验引用存在、版本一致、引文确实出现。
 *
 * 纯逻辑模块：不依赖 obsidian，可独立测试。
 */

import { revisionDir, revisionFileName, sourceAssetsDir } from "../vault/paths";
import type { DigestClaimInput } from "./prompts";

/** 冻结快照路径（docs/08 §6.1）。 */
export function digestSnapshotPath(systemFolder: string, digestId: string, revision: number): string {
  return `${revisionDir(systemFolder, "digests", digestId)}/${revisionFileName(revision)}`;
}

/** 快照中的块锚点：`<路径>#^c0001`。 */
export function snapshotAnchor(systemFolder: string, digestId: string, revision: number, claimId: string): string {
  return `${digestSnapshotPath(systemFolder, digestId, revision)}#^${claimId}`;
}

/** 原文块锚点：`01 Sources/_assets/<item>/source-000001/normalized#^s0001`。 */
export function sourceAnchor(
  sourcesFolder: string, itemId: string, sourceRevision: number, segmentId: string,
): string {
  return `${sourceAssetsDir(sourcesFolder, itemId, sourceRevision)}/normalized#^${segmentId}`;
}

export interface SnapshotInput {
  digestId: string;
  digestRevision: number;
  sourceRevision: number;
  itemId: string;
  title: string;
  summary: string;
  claims: DigestClaimInput[];
  sourcesFolder: string;
  /** 生成时间（快照头展示）。 */
  createdAt: string;
}

/**
 * 生成冻结的 Digest 证据快照。
 *
 * 每条观点一个带块 ID 的段落，块内链接到当时的原文片段；
 * 重新提炼产生新快照，旧快照保留，旧 Knowledge 的引用不会悄悄变化。
 */
export function renderDigestSnapshot(input: SnapshotInput): string {
  const lines = [
    "---",
    `kb_id: ${JSON.stringify(input.digestId)}`,
    "kb_type: digest-snapshot",
    `kb_digest_revision: ${input.digestRevision}`,
    `kb_source_revision: ${input.sourceRevision}`,
    `kb_item_id: ${JSON.stringify(input.itemId)}`,
    "---",
    "",
    `# ${input.title}（冻结证据 r${String(input.digestRevision).padStart(6, "0")}）`,
    "",
    `生成于 ${input.createdAt}；本文件是当时证据的固定版本，不随重新提炼改变。`,
    "",
  ];
  if (input.summary) lines.push(`总结：${input.summary}`, "");
  lines.push("## 观点与原文依据", "");
  for (const claim of input.claims) {
    const links = claim.evidence_ids.map((sid) =>
      `[[${sourceAnchor(input.sourcesFolder, input.itemId, input.sourceRevision, sid)}|${sid}]]`);
    const cond = claim.conditions ? `（适用条件：${claim.conditions}）` : "";
    lines.push(`${claim.text}${cond}${links.length ? "　依据：" + links.join("、") : ""} ^${claim.claim_id}`);
    lines.push("");
  }
  return lines.join("\n");
}

/**
 * 把受管理正文里的观点引用渲染为易读链接（docs/08 §6.1）。
 *
 * 对每条证据映射，在正文中 `[claim_id]` 标记后注入「依据：<易读链接>」；
 * 已有依据的不重复注入。链接目标由本地解析器生成，不使用模型给的路径。
 */
export function renderCitations(
  body: string,
  evidenceMap: Record<string, unknown>,
  opts: {
    systemFolder: string;
    sourcesFolder: string;
    /** Digest 标题，用于易读链接文本。 */
    digestTitleOf: (digestId: string) => string;
    /** 允许的映射：claim_id → 证据条目（已通过校验）。 */
  },
): string {
  let out = body;
  for (const [claimId, raw] of Object.entries(evidenceMap)) {
    if (claimId.startsWith("__") || !raw || typeof raw !== "object") continue;
    const e = raw as Record<string, unknown>;
    const digestId = String(e.digest_id ?? "");
    const digestRevision = Number(e.digest_revision ?? 0);
    const itemId = String(e.item_id ?? "");
    const sourceRevision = Number(e.source_revision ?? 0);
    const segmentIds = Array.isArray(e.segment_ids) ? e.segment_ids.map(String) : [];
    if (!digestId || !digestRevision) continue;

    const snapshot = snapshotAnchor(opts.systemFolder, digestId, digestRevision, claimId);
    const label = opts.digestTitleOf(digestId) || digestId;
    const sourceLinks = segmentIds
      .map((sid) => `[[${sourceAnchor(opts.sourcesFolder, itemId, sourceRevision, sid)}|${sid}]]`)
      .join("、");
    const citation = `（依据：[[${snapshot}|${label}]]${sourceLinks ? ` · ${sourceLinks}` : ""}）`;

    // 定位全部 `[c0001]` 标记（审查 C-05：同一观点可能在多个小节重复引用，
    // 每处都应有依据可跳）；已在 24 字符内写过依据的位置跳过，避免重复注入
    const marker = `[${claimId}]`;
    let from = 0;
    while (true) {
      const at = out.indexOf(marker, from);
      if (at === -1) break;
      const tail = out.slice(at, at + marker.length + 24);
      if (!tail.includes("依据：")) {
        out = `${out.slice(0, at + marker.length)}${citation}${out.slice(at + marker.length)}`;
        from = at + marker.length + citation.length;
      } else {
        from = at + marker.length;
      }
    }
  }
  return out;
}

/**
 * 引用可追溯性校验：映射里的版本与片段必须存在（docs/08 §6.1）。
 *
 * `knowledge_revision` 是候选生成时读取的主题版本（基线），不是写入后的新版本；
 * 这里校验它与基线一致，确保证据链指向的是判断所依据的那一版。
 *
 * 只证明可追溯，不证明语义正确；语义仍需采纳时检查与抽样复核。
 */
export function validateEvidenceMap(
  evidenceMap: Record<string, unknown>,
  ctx: {
    knowledgeId: string;
    /** 候选生成时读取的主题版本（基线版本）。 */
    baseKnowledgeRevision: number;
    /** 快照是否存在：digestId@revision → 是否可读。 */
    snapshotExists: (digestId: string, revision: number) => boolean;
    /** 原文片段是否存在：itemId + sourceRevision + segmentId → 是否可读。 */
    segmentExists: (itemId: string, sourceRevision: number, segmentId: string) => boolean;
  },
): string[] {
  const errors: string[] = [];
  for (const [claimId, raw] of Object.entries(evidenceMap)) {
    if (claimId.startsWith("__")) continue;
    if (!raw || typeof raw !== "object") {
      errors.push(`evidence_map.${claimId} 必须是对象`);
      continue;
    }
    const e = raw as Record<string, unknown>;
    if (String(e.knowledge_id ?? "") !== ctx.knowledgeId) {
      errors.push(`evidence_map.${claimId}.knowledge_id 与目标主题不一致`);
    }
    if (Number(e.knowledge_revision ?? -1) !== ctx.baseKnowledgeRevision) {
      errors.push(`evidence_map.${claimId}.knowledge_revision 与判断所依据的基线版本不一致`);
    }
    const digestId = String(e.digest_id ?? "");
    const digestRevision = Number(e.digest_revision ?? 0);
    if (!digestId || !digestRevision) {
      errors.push(`evidence_map.${claimId} 缺少 digest_id 或 digest_revision`);
    } else if (!ctx.snapshotExists(digestId, digestRevision)) {
      errors.push(`evidence_map.${claimId} 指向不存在的 Digest 快照 ${digestId}@r${digestRevision}`);
    }
    const itemId = String(e.item_id ?? "");
    const sourceRevision = Number(e.source_revision ?? 0);
    const segmentIds = Array.isArray(e.segment_ids) ? e.segment_ids.map(String) : [];
    if (!itemId || !sourceRevision || !segmentIds.length) {
      errors.push(`evidence_map.${claimId} 缺少原文定位（item_id/source_revision/segment_ids）`);
    } else {
      for (const sid of segmentIds) {
        if (!ctx.segmentExists(itemId, sourceRevision, sid)) {
          errors.push(`evidence_map.${claimId} 指向不存在的原文片段 ${itemId}@r${sourceRevision}#${sid}`);
        }
      }
    }
  }
  return errors;
}
