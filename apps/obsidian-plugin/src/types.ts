/** 与服务端契约对应的类型（docs/02 §6.3、§10.1、§13；apps/server/kbserver/api/routes_sync.py）。 */

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

export interface ReceiptResult {
  item_id: string;
  bundle_revision: number;
  manifest_sha256: string;
  device_id: string;
  consumer_epoch: number;
  local_commit_id: string;
  status: string;
}

/** 99 System/KnowledgeInbox/commits/<item_id>--<rev>.json（docs/02 §13.2 可恢复完成点）。 */
export interface CommitRecord {
  item_id: string;
  bundle_revision: number;
  manifest_sha256: string;
  note_path: string;
  /** 生成区最后写入内容的 SHA-256（冲突检测基线）。 */
  generated_digest: string | null;
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
}

export interface KbSettings {
  serverUrl: string;
  tokenRef: string;
  deviceName: string;
  inboxFolder: string;
  sourcesFolder: string;
  assetsFolder: string;
  systemFolder: string;
  autoSync: boolean;
  deviceId: string;
  userId: string;
}
