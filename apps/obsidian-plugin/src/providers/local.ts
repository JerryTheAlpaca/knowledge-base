/**
 * 本地整理模型直连（docs/08 §8.2、§8.3）。
 *
 * 关键约束：
 * - 插件直接调用用户选定的模型服务，**不经过本系统云端**；本系统服务器
 *   不接收 Knowledge、主题索引或本地整理任务。
 * - 本地配置 `mode=follow_cloud|cloud_profile|local_profile`：
 *   `follow_cloud` 跟随云端默认配置；`cloud_profile` 固定一个本人线上配置 ID；
 *   `local_profile` 固定本机配置 ID。切换本地模式不会修改云端默认值。
 * - 「同一配置」以一次已同步版本为准：批次开始时固定 `pinned*` 字段，进行中的任务不切换。
 * - 无法确认线上配置时显示「等待配置同步」（`awaitingSync`），不静默换 Key。
 * - Key 失效只暂停该配置的任务，不回退到另一配置或另一用户。
 * - 明文 Key 只存在于单次请求对象；不写进任务文件、笔记或日志。
 *
 * 纯逻辑模块：HTTP 通过注入的 `LocalTransport` 发送，可独立测试。
 */

import type { CloudProfile, LocalBindingSecret } from "../api";
import type { KbSettings, LocalModelConfig } from "../types";

/** 线上配置同步结果：Key 之外还必须确定 endpoint、model 和能力参数（docs/08 §8.2）。 */
export interface ResolvedLocalModel {
  mode: LocalModelConfig["mode"];
  /** 配置来源描述，用于面板展示与任务记录。 */
  label: string;
  baseUrl: string;
  model: string;
  capabilities: Record<string, unknown>;
  /** 明文 Key；只在本次调用内使用，不落盘。 */
  apiKey: string;
  /** 固定下来的配置版本（本地独立配置为 null）。 */
  profileVersion: number | null;
  credentialVersion: number | null;
  /** 线上配置 ID；本地独立配置为空。 */
  cloudProfileId: string;
}

/** 无法解析可用配置时抛出；调用方据此暂停该配置的任务（不换 Key）。 */
export class LocalModelUnavailableError extends Error {
  constructor(
    message: string,
    readonly reason: "awaiting_sync" | "not_configured" | "no_credential" | "no_binding" | "no_secret",
  ) {
    super(message);
    this.name = "LocalModelUnavailableError";
  }
}

/** 本地模型调用错误分类（与 docs/02 §8.3 一致，供任务重试策略使用）。 */
export class LocalModelError extends Error {
  constructor(
    message: string,
    readonly kind: "auth" | "retryable" | "unknown_outcome" | "invalid",
  ) {
    super(message);
    this.name = "LocalModelError";
  }
}

export interface LocalGenerateRequest {
  system: string;
  user: string;
  maxOutputTokens?: number;
  jsonMode?: boolean;
}

export interface LocalGenerateResult {
  outputText: string;
  finishReason: string | null;
}

/** 本地整理调用接口（organize 通过它调用模型，便于测试替换）。 */
export interface ModelCallerLike {
  call(system: string, user: string): Promise<LocalGenerateResult>;
}

/** HTTP 传输抽象：Obsidian 用 requestUrl，无头测试注入替身。 */
export interface LocalTransport {
  post(url: string, headers: Record<string, string>, body: string): Promise<{
    status: number;
    text: string;
    /** 连接未建立（请求未发出，可安全重试）。 */
    connectFailed?: boolean;
    /** 已发出但未收到响应（结果未知，不盲目重发）。 */
    timedOut?: boolean;
  }>;
}

/** 线上配置读取来源：由 main 注入 KbClient 的读取方法，便于测试替换。 */
export interface CloudProfileSource {
  /** GET /v1/provider-profiles：不含 Key。 */
  listProfiles(): Promise<CloudProfile[]>;
  /** GET /v1/settings：云端默认配置 ID。 */
  getDefaultProfileId(): Promise<string | null>;
  /** GET /v1/provider-profiles/{id}/local-binding：不含 Key。 */
  bindingStatus(profileId: string): Promise<{
    bound: boolean;
    profile_version: number | null;
    credential_version: number | null;
  }>;
  /** POST /v1/provider-profiles/{id}/local-binding：用户明确绑定后领取一次。 */
  bind(profileId: string): Promise<LocalBindingSecret>;
}

export interface SecretReader {
  /** 读取模型 Key；禁止明文降级，秘密存储不可用时只返回会话内值。 */
  getSecretStrict(ref: string): Promise<string | null>;
}

/** 本机独立配置的 secret 引用名（绑定导入的线上 Key 用 bindingSecretRef）。 */
export function bindingSecretRef(profileId: string): string {
  return `kb-local-binding-${profileId}`;
}

/** 从线上配置与绑定结果得到可直连的配置（版本固定，供任务记录）。 */
function pinFromBinding(binding: LocalBindingSecret, fallback: CloudProfile | null): ResolvedLocalModel {
  return {
    mode: fallback ? "cloud_profile" : "follow_cloud",
    label: `${binding.model}（线上 v${binding.profile_version}）`,
    baseUrl: binding.endpoint,
    model: binding.model,
    capabilities: binding.capabilities ?? {},
    apiKey: binding.secret,
    profileVersion: binding.profile_version,
    credentialVersion: binding.credential_version,
    cloudProfileId: binding.profile_id,
  };
}

/** 把「已固定的配置」与当前线上配置比对：版本变了要重新同步（docs/08 §8.2）。 */
export function pinnedStillValid(cfg: LocalModelConfig, profile: CloudProfile): boolean {
  if (!cfg.pinnedProfileVersion) return false;
  if (cfg.pinnedProfileVersion !== profile.version) return false;
  if (cfg.pinnedCredentialVersion !== profile.credential_version) return false;
  return cfg.pinnedEndpoint === profile.endpoint && cfg.pinnedModel === profile.model;
}

/**
 * 解析本地整理实际使用的模型配置。
 *
 * 顺序：本机独立配置 → 线上固定/跟随配置（需要已绑定到本设备）。
 * 任何一步无法确定都抛出 `LocalModelUnavailableError`，由调用方暂停任务而不是换 Key。
 */
export async function resolveLocalModel(opts: {
  settings: KbSettings;
  secrets: SecretReader;
  cloud: CloudProfileSource | null;
}): Promise<ResolvedLocalModel> {
  const cfg = opts.settings.localModel;

  if (cfg.mode === "local_profile") {
    const loc = cfg.local;
    if (!loc.baseUrl.trim() || !loc.model.trim()) {
      throw new LocalModelUnavailableError(
        "本机独立配置缺少 API Base URL 或模型名。", "not_configured");
    }
    const key = await opts.secrets.getSecretStrict(loc.secretRef);
    if (!key) {
      throw new LocalModelUnavailableError(
        "本机独立配置的 API Key 不在本机秘密存储中；请重新填写（重启后需重新配置）。", "no_secret");
    }
    return {
      mode: "local_profile",
      label: loc.name.trim() || loc.model,
      baseUrl: loc.baseUrl.trim(),
      model: loc.model.trim(),
      capabilities: {},
      apiKey: key,
      profileVersion: null,
      credentialVersion: null,
      cloudProfileId: "",
    };
  }

  if (!opts.cloud) {
    throw new LocalModelUnavailableError(
      "尚未登录，无法读取线上配置；可切换为本地独立配置继续整理。", "awaiting_sync");
  }

  // 目标线上配置：follow_cloud 取服务器默认值，cloud_profile 取用户固定项
  let profileId = cfg.cloudProfileId;
  if (cfg.mode === "follow_cloud") {
    profileId = (await opts.cloud.getDefaultProfileId()) ?? "";
    if (!profileId) {
      throw new LocalModelUnavailableError(
        "线上还没有默认模型配置；请在网页收件箱设置后再整理。", "not_configured");
    }
  } else if (!profileId) {
    throw new LocalModelUnavailableError("尚未选择要使用的线上配置。", "not_configured");
  }

  const profiles = await opts.cloud.listProfiles();
  const profile = profiles.find((p) => p.id === profileId) ?? null;
  if (!profile) {
    throw new LocalModelUnavailableError(
      "无法确认线上配置（可能已被删除）；不静默换 Key，请重新选择。", "awaiting_sync");
  }
  if (profile.kind !== "llm") {
    throw new LocalModelUnavailableError("本地整理只支持 llm 类型的配置。", "not_configured");
  }
  if (!profile.configured) {
    throw new LocalModelUnavailableError(
      "该线上配置还没有可用 Key；请先在网页收件箱中填写。", "no_credential");
  }

  // 已固定且线上未变：直接复用本机秘密存储中的副本（Key 不重复下发）
  if (pinnedStillValid(cfg, profile)) {
    const key = await opts.secrets.getSecretStrict(bindingSecretRef(profile.id));
    if (key) {
      return {
        mode: cfg.mode,
        label: `${profile.model}（线上 v${profile.version}）`,
        baseUrl: cfg.pinnedEndpoint,
        model: cfg.pinnedModel,
        capabilities: profile.capabilities ?? {},
        apiKey: key,
        profileVersion: cfg.pinnedProfileVersion,
        credentialVersion: cfg.pinnedCredentialVersion,
        cloudProfileId: profile.id,
      };
    }
    // 秘密存储里没有副本：需要用户重新绑定（不静默换 Key）
    throw new LocalModelUnavailableError(
      "本机没有该线上配置的 Key 副本；请在设置中「配置到本设备」。", "no_binding");
  }

  // 版本不一致或尚未固定：以本次读取的版本为准，要求已绑定
  const status = await opts.cloud.bindingStatus(profile.id).catch(() => null);
  if (!status || !status.bound) {
    throw new LocalModelUnavailableError(
      "该线上配置尚未绑定到本设备；请在设置中「配置到本设备」。", "no_binding");
  }
  const key = await opts.secrets.getSecretStrict(bindingSecretRef(profile.id));
  if (!key) {
    throw new LocalModelUnavailableError(
      "本机没有该线上配置的 Key 副本；请重新「配置到本设备」。", "no_binding");
  }
  return {
    mode: cfg.mode,
    label: `${profile.model}（线上 v${profile.version}）`,
    baseUrl: profile.endpoint,
    model: profile.model,
    capabilities: profile.capabilities ?? {},
    apiKey: key,
    profileVersion: profile.version,
    credentialVersion: profile.credential_version,
    cloudProfileId: profile.id,
  };
}

/** 批次开始时固定配置版本（docs/08 §8.2：进行中的任务不切换）。 */
export function pinConfig(cfg: LocalModelConfig, resolved: ResolvedLocalModel): LocalModelConfig {
  return {
    ...cfg,
    pinnedProfileVersion: resolved.profileVersion,
    pinnedCredentialVersion: resolved.credentialVersion,
    pinnedEndpoint: resolved.baseUrl,
    pinnedModel: resolved.model,
    awaitingSync: false,
  };
}

function chatCompletionsUrl(baseUrl: string): string {
  const base = baseUrl.trim().replace(/\/+$/, "");
  if (base.endsWith("/chat/completions")) return base;
  return `${base}/chat/completions`;
}

/**
 * 直连所选模型服务（OpenAI-compatible）。
 *
 * 错误分类与云端一致：401/403 → auth；429/5xx/连接失败 → retryable；
 * 已发出但超时 → unknown_outcome（不盲目重发）；其余 4xx → invalid。
 */
export async function generateLocal(
  transport: LocalTransport,
  model: ResolvedLocalModel,
  request: LocalGenerateRequest,
): Promise<LocalGenerateResult> {
  const caps = model.capabilities ?? {};
  const body: Record<string, unknown> = {
    model: model.model,
    messages: [
      { role: "system", content: request.system },
      { role: "user", content: request.user },
    ],
    max_tokens: request.maxOutputTokens ?? Number(caps.max_output_tokens ?? 2000),
  };
  if (caps.temperature !== false) body.temperature = 0.2;
  if (request.jsonMode && caps.json_mode !== false) {
    body.response_format = { type: "json_object" };
  }

  let res;
  try {
    res = await transport.post(
      chatCompletionsUrl(model.baseUrl),
      { Authorization: `Bearer ${model.apiKey}`, "Content-Type": "application/json" },
      JSON.stringify(body),
    );
  } catch (err) {
    throw new LocalModelError(
      `连接模型服务失败：${err instanceof Error ? err.name : String(err)}`, "retryable");
  }
  if (res.connectFailed) {
    throw new LocalModelError("连接模型服务失败（请求未发出，可重试）。", "retryable");
  }
  if (res.timedOut) {
    throw new LocalModelError("模型服务响应超时（结果未知，不自动重发）。", "unknown_outcome");
  }
  if (res.status === 401 || res.status === 403) {
    throw new LocalModelError(`模型凭据被拒绝（HTTP ${res.status}）。`, "auth");
  }
  if (res.status === 429 || res.status >= 500) {
    throw new LocalModelError(`模型服务临时错误（HTTP ${res.status}）。`, "retryable");
  }
  if (res.status >= 400) {
    throw new LocalModelError(
      `模型请求被拒绝（HTTP ${res.status}）：${res.text.slice(0, 200)}`, "invalid");
  }

  let data: { choices?: Array<{ message?: { content?: unknown }; finish_reason?: string }> };
  try {
    data = JSON.parse(res.text) as typeof data;
  } catch {
    throw new LocalModelError("模型服务返回非 JSON 响应。", "retryable");
  }
  const choice = (data.choices ?? [])[0];
  const content = choice?.message?.content;
  if (typeof content !== "string" || !content.trim()) {
    throw new LocalModelError("模型服务返回空内容。", "retryable");
  }
  return { outputText: content, finishReason: choice?.finish_reason ?? null };
}

/** 从模型输出解析 JSON 对象；容忍 ```json 代码块包裹。 */
export function parseModelJson(outputText: string): Record<string, unknown> {
  let text = outputText.trim();
  if (text.startsWith("```")) {
    const firstNewline = text.indexOf("\n");
    if (firstNewline !== -1) text = text.slice(firstNewline + 1);
    if (text.trimEnd().endsWith("```")) text = text.trimEnd().slice(0, -3);
  }
  const start = text.indexOf("{");
  const end = text.lastIndexOf("}");
  if (start === -1 || end === -1 || end <= start) throw new Error("输出中未找到 JSON 对象");
  const doc = JSON.parse(text.slice(start, end + 1)) as unknown;
  if (!doc || typeof doc !== "object" || Array.isArray(doc)) throw new Error("JSON 顶层必须是对象");
  return doc as Record<string, unknown>;
}

/** 「测试本地连接」：只验证所选模型服务可达，不发送任何笔记正文（docs/08 §8.2）。 */
export async function testLocalConnection(
  transport: LocalTransport,
  model: ResolvedLocalModel,
): Promise<string> {
  const result = await generateLocal(transport, model, {
    system: "你是连接测试。只回复一个 JSON 对象。",
    user: '{"task":"连接测试","reply":{"ok":true}}',
    maxOutputTokens: 64,
    jsonMode: true,
  });
  const text = result.outputText.trim().slice(0, 80);
  return `连接成功：${model.label} · ${model.model}；模型返回「${text}」。`;
}
