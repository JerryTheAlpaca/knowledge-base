/**
 * A09 无头恢复驱动：不依赖 Obsidian UI，直接驱动 SyncEngine 处理 pending 条目。
 * 复用真实 Vault 的 data.json（与插件同格式），恢复后状态对插件完全透明。
 *
 * 前置条件：Obsidian 未运行（避免双写者）。
 * 用法：esbuild bundle 后 node recovery-headless.cjs <vaultRoot>
 */

import * as fs from "node:fs";
import * as fsp from "node:fs/promises";
import * as path from "node:path";
import { SyncEngine, EMPTY_STATE } from "../src/sync/engine";
import { KbClient } from "../src/api";
import { NodeFs } from "./nodefs";
import type { KbSettings, SyncState } from "../src/types";

const vaultRoot = process.argv[2];
if (!vaultRoot) {
  console.error("用法：node recovery-headless.cjs <vaultRoot>");
  process.exit(1);
}

const dataFile = path.join(vaultRoot, ".obsidian", "plugins", "kb-inbox", "data.json");

function loadPluginData(): Record<string, unknown> {
  return JSON.parse(fs.readFileSync(dataFile, "utf-8")) as Record<string, unknown>;
}

async function savePluginData(data: Record<string, unknown>): Promise<void> {
  await fsp.writeFile(dataFile, JSON.stringify(data, null, 2), "utf-8");
}

async function main(): Promise<void> {
  const data = loadPluginData();
  const settings = data as unknown as KbSettings;
  const token = (data["tokenFallback"] as string) ?? "";
  if (!token) throw new Error("data.json 无 tokenFallback，无法认证");

  console.log("== A09 无头恢复驱动 ==");
  console.log("server:", settings.serverUrl, "| device:", settings.deviceName);
  console.log("恢复前 syncState.cursor:", (data["syncState"] as SyncState | undefined)?.cursor);

  const client = new KbClient(settings.serverUrl, token);
  const engine = new SyncEngine({
    fs: new NodeFs(vaultRoot),
    getClient: () => client,
    settings: () => settings,
    loadState: async () => {
      const d = loadPluginData();
      return (d["syncState"] as SyncState | undefined) ?? EMPTY_STATE;
    },
    saveState: async (s) => {
      const d = loadPluginData();
      d["syncState"] = s;
      await savePluginData(d);
    },
    onStatus: (st) => console.log("status:", JSON.stringify(st)),
    log: (msg) => console.log("[engine]", msg),
  });

  await engine.runOnce("a09-headless-recovery");
  console.log("== runOnce 完成，核对落盘 ==");
}

void main().catch((err) => {
  console.error("恢复失败:", err);
  process.exit(1);
});
