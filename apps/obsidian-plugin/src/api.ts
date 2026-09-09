/**
 * 服务端 API 客户端（docs/02 §10.1；docs/08 §8.3、§8.4）：Bearer 服务 Token。
 * 使用 Obsidian requestUrl（Electron 环境下 fetch 会被 CORS 拦截）。
 */

import { requestUrl } from "obsidian";
import type {
  DevicePollResult,
  DeviceStartResult,
  EventsPage,
  KbManifest,
  ReceiptResult,
} from "./types";

export class ApiError extends Error {
  constructor(readonly code: string, message: string, readonly status: number) {
    super(message);
    this.name = "ApiError";
  }
}

export class ManifestVerifyError extends Error {
  constructor(msg: string) {
    super(msg);
    this.name = "ManifestVerifyError";
  }
}

/** 线上配置摘要（GET /v1/provider-profiles；不含 Key）。 */
export interface CloudProfile {
  id: string;
  kind: string;
  adapter: string;
  endpoint: string;
  model: string;
  capabilities: Record<string, unknown>;
  version: number;
  configured: boolean;
  credential_version: number | null;
  created_at: string;
}

/** 本地绑定响应（POST /v1/provider-profiles/{id}/local-binding）。 */
export interface LocalBindingSecret {
  binding_id: string;
  profile_id: string;
  profile_version: number;
  credential_version: number;
  endpoint: string;
  model: string;
  capabilities: Record<string, unknown>;
  secret: string;
  bound_at: string;
  note: string;
}

export interface LocalBindingStatus {
  profile_id: string;
  bound: boolean;
  device_id: string | null;
  profile_version: number | null;
  credential_version: number | null;
  bound_at: string | null;
  note: string;
}

function baseUrlOf(serverUrl: string): string {
  return serverUrl.trim().replace(/\/+$/, "");
}

async function sha256HexBytes(data: BufferSource): Promise<string> {
  const buf = await crypto.subtle.digest("SHA-256", data);
  const out = new Uint8Array(buf);
  let hex = "";
  for (const b of out) hex += b.toString(16).padStart(2, "0");
  return hex;
}

export class KbClient {
  private async request(path: string, opts: { method?: string; body?: unknown } = {}): Promise<{ status: number; text: string; headers: Record<string, string> }> {
    const res = await requestUrl({
      url: `${baseUrlOf(this.serverUrl)}${path}`,
      method: opts.method ?? "GET",
      contentType: opts.body !== undefined ? "application/json" : undefined,
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      headers: { Authorization: `Bearer ${this.token}` },
      throw: false,
    });
    const headers: Record<string, string> = {};
    for (const [k, v] of Object.entries(res.headers ?? {})) headers[k.toLowerCase()] = String(v);
    return { status: res.status, text: res.text, headers };
  }

  private async requestJson<T>(path: string, opts: { method?: string; body?: unknown } = {}): Promise<T> {
    const { status, text } = await this.request(path, opts);
    if (status < 200 || status >= 300) throw toApiError(status, text);
    return JSON.parse(text) as T;
  }

  /** 发起浏览器授权（无需凭据）：返回 browser_url 与 poll_secret
   * （docs/05 §4.5；docs/08 §8.3 可申请 profiles:bind-local）。 */
  static async deviceStart(
    serverUrl: string,
    deviceName: string,
    requestedScopes: string[] = [],
  ): Promise<DeviceStartResult> {
    const res = await requestUrl({
      url: `${baseUrlOf(serverUrl)}/v1/auth/device/start`,
      method: "POST",
      contentType: "application/json",
      body: JSON.stringify({ device_name: deviceName, requested_scopes: requestedScopes }),
      throw: false,
    });
    if (res.status < 200 || res.status >= 300) throw toApiError(res.status, res.text);
    return JSON.parse(res.text) as DeviceStartResult;
  }

  /** 轮询授权结果：pending 或（批准后）设备 Token。 */
  static async devicePoll(serverUrl: string, requestId: string, pollSecret: string): Promise<DevicePollResult> {
    const res = await requestUrl({
      url: `${baseUrlOf(serverUrl)}/v1/auth/device/poll`,
      method: "POST",
      contentType: "application/json",
      body: JSON.stringify({ request_id: requestId, poll_secret: pollSecret }),
      throw: false,
    });
    if (res.status === 410) {
      // 过期/取消/已消费：业务上等同「未完成」，由调用方提示重试
      return { status: "pending" };
    }
    if (res.status < 200 || res.status >= 300) throw toApiError(res.status, res.text);
    return JSON.parse(res.text) as DevicePollResult;
  }

  async listEvents(after: number, limit = 100): Promise<EventsPage> {
    return this.requestJson(`/v1/events?after=${after}&limit=${limit}`);
  }

  /** 下载固定版本清单并校验 X-Manifest-SHA256 响应摘要（docs/02 §13.2 第 2 步）。 */
  async getManifest(itemId: string, revision: number): Promise<{ manifest: KbManifest; sha256: string }> {
    const { status, text, headers } = await this.request(`/v1/items/${itemId}/bundles/${revision}/manifest`);
    if (status === 410) throw new ApiError("GONE", "条目已在服务器删除", status);
    if (status < 200 || status >= 300) throw toApiError(status, text);
    const sha256 = headers["x-manifest-sha256"] ?? "";
    const actual = await sha256HexBytes(new TextEncoder().encode(text));
    if (!sha256 || sha256 !== actual) {
      throw new ManifestVerifyError(`清单摘要不匹配：header=${sha256 || "缺失"} 本地=${actual}`);
    }
    return { manifest: JSON.parse(text) as KbManifest, sha256 };
  }

  async getFile(itemId: string, revision: number, fileId: string): Promise<ArrayBuffer> {
    const res = await requestUrl({
      url: `${baseUrlOf(this.serverUrl)}/v1/items/${itemId}/bundles/${revision}/files/${fileId}`,
      method: "GET",
      headers: { Authorization: `Bearer ${this.token}` },
      throw: false,
    });
    if (res.status < 200 || res.status >= 300) throw toApiError(res.status, res.text);
    return res.arrayBuffer;
  }

  async sendReceipt(itemId: string, revision: number, manifestSha256: string, localCommitId: string): Promise<ReceiptResult> {
    return this.requestJson("/v1/receipts", {
      method: "POST",
      body: {
        item_id: itemId,
        bundle_revision: revision,
        manifest_sha256: manifestSha256,
        local_commit_id: localCommitId,
      },
    });
  }

  /** 断开当前设备：撤销服务端 Token；本地凭据由调用方清理（docs/05 §4.5 第 7 条）。 */
  async disconnectDevice(): Promise<void> {
    if (!this.deviceId) throw new ApiError("NO_DEVICE", "尚未登录或缺少设备 ID", 400);
    await this.requestJson(`/v1/devices/${encodeURIComponent(this.deviceId)}/disconnect`, {
      method: "POST",
    });
  }

  // ---- 模型配置与本地 Key 绑定（docs/08 §8.2、§8.3） ----

  /** 线上配置列表：不含 Key。 */
  async listProfiles(): Promise<CloudProfile[]> {
    return this.requestJson("/v1/provider-profiles");
  }

  /** 服务器默认模型配置 ID（云端提炼默认值）。 */
  async getSettings(): Promise<{ default_profile_id: string | null }> {
    return this.requestJson("/v1/settings");
  }

  async localBindingStatus(profileId: string): Promise<LocalBindingStatus> {
    return this.requestJson(`/v1/provider-profiles/${encodeURIComponent(profileId)}/local-binding`);
  }

  /** 领取线上配置的 Key（仅在用户明确绑定时调用一次）。 */
  async bindLocalKey(profileId: string): Promise<LocalBindingSecret> {
    return this.requestJson(`/v1/provider-profiles/${encodeURIComponent(profileId)}/local-binding`, {
      method: "POST",
    });
  }

  /** 解绑本机：不撤销线上或供应商 Key。 */
  async unbindLocalKey(profileId: string): Promise<{ unbound: boolean; note: string }> {
    return this.requestJson(`/v1/provider-profiles/${encodeURIComponent(profileId)}/local-binding`, {
      method: "DELETE",
    });
  }

  /** 条目阅读视图（原始资料 + 云端提炼）。 */
  async getReading(itemId: string): Promise<Record<string, unknown>> {
    return this.requestJson(`/v1/items/${encodeURIComponent(itemId)}/reading`);
  }

  constructor(readonly serverUrl: string, readonly token: string, readonly deviceId: string = "") {}
}

function toApiError(status: number, text: string): ApiError {
  try {
    const parsed = JSON.parse(text) as { error?: { code?: string; message?: string } };
    if (parsed?.error?.code) {
      return new ApiError(parsed.error.code, parsed.error.message ?? text, status);
    }
  } catch {
    // 非 JSON 错误体
  }
  return new ApiError("HTTP_ERROR", `HTTP ${status}: ${text.slice(0, 200)}`, status);
}
