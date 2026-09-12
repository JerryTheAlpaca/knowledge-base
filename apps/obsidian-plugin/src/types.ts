/** 与服务端契约及本地整理契约对应的类型
 * （docs/02 §6.3、§10.1、§13；docs/08 §2、§3、§4–§8；apps/server/kbserver/api/routes_sync.py）。
 */

export interface KbFileEntry {
  file_id: string;
  relative_path: string;
  role: string;
  mime: string;
  bytes: number;
  sha256: string;
}

export interface KbManifestSource {
  platform: string;
  title: string | null;
  author: string | null;
  original_url: string | null;
  canonical_url: string | null;
  published_at: string | null;
  captured_at: string | null;
  source_locator: Record<string, unknown>;
  coverage: string;
  content_scope: string;
  original_media_retained: boolean;
}

export interface KbManifest {
  schema_version: string;
  item_id: string;
  source_revision: number;
  bundle_revision: number;
  created_at: string;
  source: KbManifestSource;
  processing: {
    state: string;
    recipe_version: string;
    result_file_id: string | null;
    source_revision: number;
  };
  files: KbFileEntry[];
  missing_materials: string[];
  warnings: string[];
  expires_at: string;
}

export interface KbEvent {
  seq: number;
  event_type: string;
  item_id: string | null;
  bundle_revision: number | null;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface EventsPage {
  events: KbEvent[];
  next_cursor: number;
  has_more: boolean;
}

export interface PairResult {
  token: string;
  device_id: string;
  user_id: string;
  scopes: string[];
  expires_at: string;
}

/** POST /v1/auth/device/start 响应（docs/05 §4.5 浏览器授权流程；docs/08 §8.3 额外权限）。 */
export interface DeviceStartResult {
  request_id: string;
  poll_secret: string;
  browser_url: string;
  expires_at: string;
  interval_seconds: number;
  granted_scopes: string[];
}

/** POST /v1/auth/device/poll 响应：pending 或最终 Token。 */
export interface DevicePollResult {
  status: "pending" | "ok";
  token?: string;
  device_id?: string;
  user_id?: string;
  scopes?: string[];
  expires_at?: string;
}

export interface ReceiptResult {
  item_id: string;
  bundle_revision: number;
  manifest_sha256: string;
  device_id: string;
  consumer_epoch: number;
  local_commit_id: string;
  status: string;
}

/** 99 System/KnowledgeInbox/commits/<item_id>--<rev>.json（docs/02 §13.2；docs/08 §7.2、§9）。
 *
 * 一个 Bundle 可能产出 Source + Digest 两篇笔记：每篇独立记录路径、哈希与状态，
 * 失败不标记已全部完成。
 */
export interface CommitNoteRecord {
  /** source | digest */
  role: string;
  note_path: string;
  /** 机器管理区最后写入内容的 SHA-256（冲突检测基线）。 */
  managed_digest: string | null;
  /** 写入状态：written | pending | failed | merge_needed */
  state: string;
  conflicts: string[];
}

export interface CommitRecord {
  item_id: string;
  bundle_revision: number;
  manifest_sha256: string;
  /** 布局版本：旧版（1）只有单个 note_path；新版（2）使用 notes 列表。 */
  layout_version: number;
  /** 兼容字段：Source 笔记路径（旧记录读取与显示用）。 */
  note_path: string;
  /** 生成区最后写入内容的 SHA-256（旧记录冲突检测基线）。 */
  generated_digest: string | null;
  notes: CommitNoteRecord[];
  local_commit_id: string;
  committed_at: string;
  ack_sent: boolean;
  conflicts: string[];
}

/** 本地待办（事件已读取、尚未落盘；docs/02 §13.1）。 */
export interface PendingEntry {
  item_id: string;
  revision: number;
  seq: number;
  enqueued_at: string;
  attempts: number;
  next_try_at: number;
  last_error?: string;
}

export interface EngineStatus {
  running: boolean;
  cursor: number;
  pendingCount: number;
  lastRunAt: number | null;
  lastError: string | null;
  epochConflict: boolean;
  /** 已放弃条目数（本地删除停复建或服务器已删除，A21）。 */
  suppressedCount: number;
  /** 事件积压超过单轮页数上限，还有更多待拉取（下次同步继续，审查 C-33）。 */
  moreEvents?: boolean;
}

// ---- 本地整理模型配置（docs/08 §8.2） ----

/** 本地整理使用哪个 Key：跟随云端默认 / 固定本人线上配置 / 本机独立配置。 */
export type LocalModelMode = "follow_cloud" | "cloud_profile" | "local_profile";

export interface LocalModelConfig {
  mode: LocalModelMode;
  /** mode=cloud_profile 时固定使用的线上配置 ID。 */
  cloudProfileId: string;
  /** mode=local_profile 时的本机独立配置。 */
  local: {
    name: string;
    /** API Base URL，例如 https://api.deepseek.com/v1 */
    baseUrl: string;
    model: string;
    /** 秘密存储中的引用名；明文 Key 不进入本结构。 */
    secretRef: string;
  };
  /** 整理批次开始时固定的线上配置版本（docs/08 §8.2「同一配置」以一次已同步版本为准）。 */
  pinnedProfileVersion: number | null;
  pinnedCredentialVersion: number | null;
  pinnedEndpoint: string;
  pinnedModel: string;
  /** 等待配置同步：无法确认线上配置时不静默换 Key。 */
  awaitingSync: boolean;
}

/** 整理任务状态（docs/08 §8.1、§7.2）。 */
export type OrganizeTaskState =
  | "pending" | "running" | "ready" | "accepted" | "applied"
  | "kept_digest" | "skipped" | "deferred" | "failed" | "unknown_outcome" | "stale";

/** 本地整理任务（持久化在 99 System/KnowledgeInbox/organize/）。 */
export interface OrganizeTask {
  task_id: string;
  item_id: string;
  /** 输入 Digest 的路径与版本基线。 */
  digest_path: string;
  digest_source_revision: number;
  digest_cloud_hash: string;
  /** 目标主题（可能为 null：需要新建节点建议）。 */
  target_knowledge_id: string | null;
  /** 融合时读取的主题正文基线哈希（写入前校验，docs/08 §7.2 第 4 条）。 */
  base_hash: string | null;
  state: OrganizeTaskState;
  /** 固定的模型配置版本（Key 不写进任务文件）。 */
  model_config_ref: string;
  rule_version: string;
  /** 幂等键：同一次提交重试沿用，相同 ID 不重复应用。 */
  idempotency_key: string;
  created_at: string;
  updated_at: string;
  attempts: number;
  last_error: string | null;
  /** 结果落盘位置（候选 JSON）。 */
  proposal_path: string | null;
}

/** 观点级晋升判断（docs/08 §4）。 */
export interface PromotionDecision {
  claim_id: string;
  decision: "review" | "keep_digest" | "deferred" | "skipped";
  target_knowledge_id: string | null;
  /** 无匹配主题时建议新建的节点。 */
  new_topic: { name: string; scope: string } | null;
  relation: "duplicate" | "supports" | "adds" | "revises" | "conflicts" | "none";
  reason: string;
  evidence_refs: string[];
  /** 五个维度的离散等级与理由（docs/08 §4 表）。 */
  dimensions: {
    novelty: { level: string; reason: string };
    utility: { level: string; reason: string };
    credibility: { level: string; reason: string };
    reusability: { level: string; reason: string };
    increment: { level: string; reason: string };
  };
}

/** 融合候选（docs/08 §7.1 输出契约）。 */
export interface FusionProposal {
  proposal_id: string;
  task_id: string;
  knowledge_id: string | null;
  knowledge_title: string | null;
  base_hash: string;
  proposed_managed_body: string;
  added_claims: Array<Record<string, unknown>>;
  updated_claims: Array<Record<string, unknown>>;
  retired_claims: Array<Record<string, unknown>>;
  evidence_map: Record<string, unknown>;
  conflicts: Array<Record<string, unknown>>;
  change_summary: string;
  promotion_decisions: PromotionDecision[];
  /** 状态：ready（待采纳）/ accepted / applied / skipped / stale。 */
  state: string;
  created_at: string;
  applied_at: string | null;
  /** 应用时的主题版本（回滚依据）。 */
  applied_knowledge_revision: number | null;
  no_op: boolean;
}

/** 本地知识索引条目（docs/08 §5；可重建，不随 Vault 同步）。 */
export interface KnowledgeIndexEntry {
  kb_id: string;
  title: string;
  path: string;
  aliases: string[];
  scope: string;
  keywords: string[];
  revision: number;
  reviewed_at: string | null;
  /** 正文机器区哈希，用于索引失效判断。 */
  body_hash: string;
}

export interface KnowledgeIndex {
  schema_version: string;
  built_at: string;
  entries: KnowledgeIndexEntry[];
}

export interface KbSettings {
  serverUrl: string;
  tokenRef: string;
  deviceName: string;
  inboxFolder: string;
  sourcesFolder: string;
  digestsFolder: string;
  knowledgeFolder: string;
  assetsFolder: string;
  systemFolder: string;
  autoSync: boolean;
  deviceId: string;
  userId: string;
  /** 云端提炼使用的线上配置（默认值来自服务端 settings.default_profile_id）。 */
  cloudProfileId: string;
  /** 本地整理模型配置（docs/08 §8.2）。 */
  localModel: LocalModelConfig;
  /** 本地整理总开关；未启用时云端提炼与投递照常工作（docs/08 §1）。 */
  localOrganizeEnabled: boolean;
  /** 新 Digest 入库后自动准备整理候选（不代表自动写入第三层）。 */
  autoPrepareOnSync: boolean;
  /** 每个 Vault 指定一台本地整理设备（docs/08 §7.2）。 */
  organizeDeviceId: string;
  /** 本地分析 Schema 版本（docs/08 §9）。 */
  analysisSchemaVersion: string;
  /** 布局版本：旧库为 1，执行迁移后为 2。 */
  layoutVersion: number;
}

export const ANALYSIS_SCHEMA_VERSION = "2.0";
export const LAYOUT_VERSION = 2;
export const ORGANIZE_RULE_VERSION = "promotion-v1";
