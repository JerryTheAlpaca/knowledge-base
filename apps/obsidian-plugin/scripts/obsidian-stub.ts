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
  arrayBuffer: () => Promise<ArrayBuffer>;
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
    arrayBuffer: () => twin.arrayBuffer(),
    json: (() => { try { return JSON.parse(text); } catch { return null; } })(),
  };
}
