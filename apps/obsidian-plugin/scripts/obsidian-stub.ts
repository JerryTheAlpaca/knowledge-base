/**
 * 无头环境的 obsidian 模块替身：只实现 api.ts 用到的 requestUrl。
 * 用 Node 原生 fetch 实现 Obsidian requestUrl 语义（throw:false 时返回状态不抛错）。
 */

export interface RequestUrlParam {
  url: string;
  method?: string;
  body?: string | ArrayBuffer;
  contentType?: string;
  headers?: Record<string, string>;
  throw?: boolean;
}

export interface RequestUrlResponse {
  status: number;
  text: string;
  headers: Record<string, string>;
  /** 与官方 API 一致：属性而非方法（api.ts 直接 return res.arrayBuffer）。 */
  arrayBuffer: ArrayBuffer;
  json: unknown;
}

export async function requestUrl(param: RequestUrlParam): Promise<RequestUrlResponse> {
  const headers: Record<string, string> = { ...(param.headers ?? {}) };
  if (param.contentType) headers["Content-Type"] = param.contentType;
  const res = await fetch(param.url, {
    method: param.method ?? "GET",
    headers,
    body: param.body as string | undefined,
  });
  const headersObj: Record<string, string> = {};
  res.headers.forEach((v, k) => { headersObj[k] = v; });
  const twin = res.clone();
  const text = await res.text();
  return {
    status: res.status,
    text,
    headers: headersObj,
    arrayBuffer: await twin.arrayBuffer(),
    json: (() => { try { return JSON.parse(text); } catch { return null; } })(),
  };
}

/** 设置界面在 node 里不渲染：桩只保证能被 import，弹条消息当作记录。 */
export class Notice {
  constructor(public message: string) {
    console.log("[notice]", message);
  }
}

export class Setting {
  setName(): this { return this; }
  setDesc(): this { return this; }
  setPlaceholder(): this { return this; }
  setHeading(): this { return this; }
  addText(): this { return this; }
  addTextArea(): this { return this; }
  addToggle(): this { return this; }
  addDropdown(): this { return this; }
  addButton(): this { return this; }
  addExtraButton(): this { return this; }
  setDisabled(): this { return this; }
  then(cb: (s: this) => unknown): this { cb(this); return this; }
}

export class PluginSettingTab {
  constructor(public app: unknown, public plugin: unknown) {}
  containerEl: unknown = { empty() {}, createEl() {}, addClass() {}, removeClass() {} };
  display(): void {}
  hide(): void {}
}
