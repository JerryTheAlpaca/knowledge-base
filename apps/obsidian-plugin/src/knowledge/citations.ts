/**
 * 引用与旧证据转换（docs/24 §2、§8；docs/23 §6.4、§8.2）。
 *
 * v3 起知识正文的依据**直接指向固定版本原文**：
 * `01 Sources/_assets/<item_id>/source-00000N/normalized#^s0001`
 * 不再强制经 Digest 观点中转，也不生成新的 `c` 编号。
 *
 * 旧数据只在这里被读取和展开：旧 `evidence_map` 的每条来源展开成直接 Source 引用，
 * 多条来源分别保留（两份材料的 `s0001` 绝不合并）；`digest_claim_id` 必须显式读取，
 * 不能把 Knowledge 的 claim_id 误当摘要观点 ID。旧 Digest 冻结快照留在原路径，
 * 既有链接仍可打开，本模块不再写入新快照。
 *
 * 纯逻辑模块：不依赖 obsidian，可独立测试。
 */

import { knowledgeRefsFileName, revisionDir, sourceAssetsDir } from "../vault/paths";
import type { FsLike } from "../vault/records";
import type { ContentRefV3 } from "../types";

/** 原文块锚点：`01 Sources/_assets/<item>/source-000001/normalized#^s0001`。 */
export function sourceAnchor(
  sourcesFolder: string, itemId: string, sourceRevision: number, segmentId: string,
): string {
  return `${sourceAssetsDir(sourcesFolder, itemId, sourceRevision)}/normalized#^${segmentId}`;
}

/** 一条 v3 引用的锚点：指向该范围内第一段原文（连续范围由引用表本身描述）。 */
export function refAnchor(sourcesFolder: string, ref: ContentRefV3): string | null {
  const first = ref.segment_ids[0];
  if (!ref.item_id || !ref.source_revision || !first) return null;
  return sourceAnchor(sourcesFolder, ref.item_id, ref.source_revision, first);
}

/** 旧 Digest 冻结快照路径：只读保留，保证既有两跳链接仍能打开（docs/23 §8.2）。 */
export function digestSnapshotPath(systemFolder: string, digestId: string, revision: number): string {
  return `${systemFolder}/KnowledgeInbox/revisions/digests/${digestId.replace(/[^\w-]/g, "_")}/r${String(revision).padStart(6, "0")}.md`;
}

/** 旧证据条目展开后的直接来源引用。 */
export interface LegacySourceRef {
  /** 旧 Knowledge 观点号（仅用于把正文行对应回来源，不进入新正文）。 */
  claim_id: string;
  item_id: string;
  source_revision: number;
  segment_ids: string[];
  digest_id: string;
  digest_revision: number;
  digest_claim_id: string;
}

/**
 * 把旧 `evidence_map` 展开为直接 Source 引用。
 *
 * 返回按输入顺序排列的引用；同一观点指向多个来源时逐条保留，
 * 不同条目的同名 `s0001` 因 `item_id + source_revision` 不同而不会互相覆盖。
 * 字段缺失或错位的条目进 `errors`，调用方据此把该主题列入迁移待处理而不是自动改写。
 */
export function expandLegacyEvidenceMap(
  evidenceMap: Record<string, unknown>,
): { refs: LegacySourceRef[]; errors: string[] } {
  const refs: LegacySourceRef[] = [];
  const errors: string[] = [];
  for (const [claimId, raw] of Object.entries(evidenceMap)) {
    if (claimId.startsWith("__")) continue; // __new_topic__ 等控制字段
    if (!raw || typeof raw !== "object") { errors.push(`${claimId}：证据条目不是对象`); continue; }
    const e = raw as Record<string, unknown>;
    const itemId = String(e.item_id ?? "");
    const sourceRevision = Number(e.source_revision ?? 0);
    const segmentIds = (Array.isArray(e.segment_ids) ? e.segment_ids : []).map(String).filter(Boolean);
    const digestId = String(e.digest_id ?? "");
    const digestRevision = Number(e.digest_revision ?? 0);
    const digestClaimId = String(e.digest_claim_id ?? "");
    if (!itemId || !sourceRevision || !segmentIds.length) {
      errors.push(`${claimId}：缺少原文定位（item_id/source_revision/segment_ids）`);
      continue;
    }
    if (!digestClaimId) {
      // 旧系统的错链常把 knowledge claim_id 当摘要锚点用；缺失时不猜测、不照搬
      errors.push(`${claimId}：缺少 digest_claim_id，无法确定当时对应的摘要观点`);
      continue;
    }
    refs.push({
      claim_id: claimId,
      item_id: itemId,
      source_revision: sourceRevision,
      segment_ids: segmentIds,
      digest_id: digestId,
      digest_revision: digestRevision,
      digest_claim_id: digestClaimId,
    });
  }
  return { refs, errors };
}

/** 展开后的引用 → v3 引用表条目（`source_text_hash` 由调用方按本地快照补算）。 */
export function legacyRefToContent(ref: LegacySourceRef): ContentRefV3 {
  return {
    item_id: ref.item_id,
    source_revision: ref.source_revision,
    segment_ids: [...ref.segment_ids],
    source_text_hash: "",
    locator: null,
  };
}

/** 旧正文里的观点标记与块锚点：迁移时移入「历史引用」，不再产生新的 `c` 编号。 */
export const LEGACY_CLAIM_MARKER = /\[(c\d{4})\]/g;
export const LEGACY_BLOCK_ANCHOR = /(?:\s+)\^(c\d{4})\s*$/;

// ---- 主题引用表：与正文版本成对保存，回滚时一起恢复（docs/23 §6.4 第 7 条） ----

export interface TopicReferenceRecord {
  revision: number;
  document_id: string;
  /** 该版正文的管理区哈希：回滚前确认正文没有被别的写入改过。 */
  body_hash: string;
  references: Record<string, ContentRefV3>;
}

export class TopicReferenceStore {
  constructor(private fs: FsLike, private systemFolder: string) {}

  private dir(kbId: string): string {
    return revisionDir(this.systemFolder, "knowledge", kbId);
  }

  path(kbId: string, revision: number): string {
    return `${this.dir(kbId)}/${knowledgeRefsFileName(revision)}`;
  }

  async save(kbId: string, record: TopicReferenceRecord): Promise<void> {
    await this.fs.write(this.path(kbId, record.revision), JSON.stringify(record, null, 2));
  }

  async read(kbId: string, revision: number): Promise<TopicReferenceRecord | null> {
    const p = this.path(kbId, revision);
    if (!(await this.fs.exists(p))) return null;
    try {
      return JSON.parse(await this.fs.read(p)) as TopicReferenceRecord;
    } catch {
      return null;
    }
  }

  /** 已有引用表记录的最高版本（新建主题为 null）。 */
  async latest(kbId: string): Promise<TopicReferenceRecord | null> {
    let best: TopicReferenceRecord | null = null;
    for (const entry of await this.fs.list(this.dir(kbId))) {
      const m = /refs-r(\d{6})\.json$/.exec(entry.split("/").pop() ?? entry);
      if (!m) continue;
      const record = await this.read(kbId, Number(m[1]));
      if (record && (!best || record.revision > best.revision)) best = record;
    }
    return best;
  }
}
