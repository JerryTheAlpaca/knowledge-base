/** 与服务端契约及本地笔记契约对应的类型
 * （docs/24 契约冻结；docs/23 方案；docs/02 §6.3、§10.1、§13；
 * apps/server/kbserver/api/routes_sync.py）。
 *
 * 本插件只做同步：接收 Bundle、写 Source 与 Digest 两篇笔记、发回执。
 * 第三层主题整理（本地模型直连、主题候选、03 Knowledge 写入）已移除。
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
    /** v3：内容文档格式版本，只接受 "3.0"（docs/24 §5）。 */
    format_version?: string | null;
    /** v3：complete | partial | failed。 */
    completeness?: string | null;
    /** v3：content.json 的 file_id。 */
    content_file_id?: string | null;
  };
  files: KbFileEntry[];
  missing_materials: string[];
  warnings: string[];
  expires_at: string;
}

// ---- ContentDocument v3（docs/24 §1–§4；Bundle 里的 content.json） ----

/** 引用表条目：指向某条目某固定来源修订中的一段连续原文。 */
export interface ContentRefV3 {
  item_id: string;
  source_revision: number;
  segment_ids: string[];
  source_text_hash: string;
  locator?: ContentLocatorV3 | null;
}

/** 界面显示用的自然位置（禁止把 R1/e1/s0001 显示给用户）。 */
export type ContentLocatorV3 =
  | { kind: "time"; start_ms?: number | null; end_ms?: number | null }
  | { kind: "paragraph"; paragraph_id?: string | number | null }
  | { kind: "line"; line_no?: number | null };

export type ContentBlockKindV3 = "claim" | "quote" | "suggestion" | "text";

export interface ContentBlockV3 {
  kind: ContentBlockKindV3;
  text: string;
  /** 文档内引用表键（e1、e2…），不是永久身份。 */
  refs: string[];
}

export interface ContentSectionV3 {
  heading: string;
  blocks: ContentBlockV3[];
}

export interface CompletenessGapV3 {
  code: string;
  message: string;
  refs?: string[];
  segment_ids?: string[];
  /** 受影响块位置：[sectionIndex, blockIndex]。 */
  block?: number[];
}

/** 完整 / 部分 / 失败（docs/24 §4）。 */
export interface CompletenessV3 {
  state: "complete" | "partial" | "failed";
  missing_stages: string[];
  gaps: CompletenessGapV3[];
  dropped_blocks: number;
  repair_calls: number;
}

export interface ContentProvenanceV3 {
  recipe_version: string;
  task: string;
  input_documents: Array<{ document_id: string; kind: string; revision: number }>;
  source_revisions: Array<{ item_id: string; source_revision: number }>;
}

export interface ContentDocumentV3 {
  format_version: string;
  document_id: string;
  kind: string;
  revision: number;
  created_at: string;
  title: string;
  summary: string;
  sections: ContentSectionV3[];
  references: Record<string, ContentRefV3>;
  limitations: string[];
  completeness: CompletenessV3;
  provenance: ContentProvenanceV3;
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
  /** 因内容格式不受支持而暂停导入的条目数（docs/24 §8；不发回执、不落空白笔记）。 */
  pausedForUpgrade?: number;
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
  /** 消费的内容文档格式版本（docs/24 §1）。 */
  contentFormatVersion: string;
  /** 布局版本：1/2 为带 item_id 后缀的旧文件名，3 起为可读文件名 + 文档索引。 */
  layoutVersion: number;
}

export const CONTENT_FORMAT_VERSION = "3.0";
export const LAYOUT_VERSION = 3;
