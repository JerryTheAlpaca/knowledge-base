/**
 * Obsidian requestUrl 实现的本地模型传输（docs/08 §8.3）。
 *
 * 与 providers/local.ts 的纯逻辑分离：本文件负责把 requestUrl 的异常
 * 归类为「连接未建立」或「已发出但超时」，供错误分类使用。
 */

import { requestUrl } from "obsidian";
import type { LocalTransport } from "./local";

export class ObsidianLocalTransport implements LocalTransport {
  async post(url: string, headers: Record<string, string>, body: string): Promise<{
    status: number;
    text: string;
    connectFailed?: boolean;
    timedOut?: boolean;
    error?: string;
  }> {
    try {
      const res = await requestUrl({
        url,
        method: "POST",
        contentType: "application/json",
        headers,
        body,
        throw: false,
      });
      return { status: res.status, text: res.text };
    } catch (err) {
      const message = err instanceof Error ? `${err.name}: ${err.message}` : String(err);
      // 连接阶段失败（DNS/拒绝连接/网络不可达）→ 请求未发出，可安全重试；
      // 其余一律按「已发出但结果未知」处理（docs/08 §8.2 不盲目重发）。
      // 只认明确的连接建立失败信号（审查 C-04）：宽泛的 connect 子串会把
      // `connect ETIMEDOUT` 这类「已送达后读超时」误判为可安全重发，
      // 导致同一次模型调用重复计费。
      if (/ECONNREFUSED|ENOTFOUND|EAI_AGAIN|ECONNRESET|ENETUNREACH|EHOSTUNREACH|ERR_CONNECTION_REFUSED/i.test(message)) {
        return { status: 0, text: "", connectFailed: true, error: message };
      }
      // requestUrl 的超时文案通常含 timeout/timed out；无论文案如何，
      // 非连接类失败都保守标记 timedOut（结果未知），原文带回供日志排查。
      return { status: 0, text: "", timedOut: true, error: message };
    }
  }
}
