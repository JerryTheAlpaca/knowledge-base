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
      // 连接阶段失败（DNS/拒绝连接）→ 请求未发出，可安全重试；
      // 其余（读超时等）→ 可能已送达并计费，标记结果未知。
      if (/ECONNREFUSED|ENOTFOUND|EAI_AGAIN|ECONNRESET|Connect|connect/i.test(message)) {
        return { status: 0, text: "", connectFailed: true };
      }
      return { status: 0, text: "", timedOut: true };
    }
  }
}
