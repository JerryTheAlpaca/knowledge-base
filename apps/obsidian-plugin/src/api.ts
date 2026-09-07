/**
 * 服务端 API 客户端（docs/02 §10.1）：Bearer 服务 Token。
 * 使用 Obsidian requestUrl（Electron 环境下 fetch 会被 CORS 拦截）。
 */

import { requestUrl } from "obsidian";
import type { EventsPage, KbManifest, PairResult, ReceiptResult } from "./types";

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
  constructor(readonly serverUrl: string, readonly token: string) {}

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

  static async pair(serverUrl: string, code: string, deviceName: string): Promise<PairResult> {
    const res = await requestUrl({
      url: `${baseUrlOf(serverUrl)}/v1/pairing/exchange`,
      method: "POST",
      contentType: "application/json",
      body: JSON.stringify({ code, device_name: deviceName }),
      throw: false,
    });
    if (res.status < 200 || res.status >= 300) throw toApiError(res.status, res.text);
    return JSON.parse(res.text) as PairResult;
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
