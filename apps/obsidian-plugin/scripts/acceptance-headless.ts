/**
 * M3 验收无头驱动（docs/06）：在真实服务器 + 临时 Vault 上驱动 SyncEngine，
 * 复现真机插件的关键动作，用于 A16/A21/#7 的故障注入与断言。
 *
 * 动作与 main.ts 保持同一调用序列：
 *   sync    → engine.runOnce（事件拉取 + 落盘 + 回执）
 *   restore → 恢复命令（unsuppressAll + removeForItem + rebuildIndex）
 *   status  → 打印 pending/commits/suppression
 *
 * 前置：vaultRoot/.obsidian/plugins/kb-inbox/data.json 已含 serverUrl 与 tokenFallback。
 * 用法：node acceptance-headless.cjs <vaultRoot> <sync|restore|status>
 */

import * as fs from "node:fs";
import { SyncEngine, EMPTY_STATE } from "../src/sync/engine";
import { KbClient } from "../src/api";
import { CommitStore, Suppression } from "../src/vault/records";
import { NodeFs } from "./nodefs";
import type { KbSettings, SyncState } from "../src/types";

const vaultRoot = process.argv[2];
const action = process.argv[3] ?? "status";
if (!vaultRoot) {
  console.error("用法：node acceptance-headless.cjs <vaultRoot> <sync|restore|status>");
  process.exit(1);
}

const dataFile = `${vaultRoot.replace(/\/+$/, "")}/.obsidian/plugins/kb-inbox/data.json`
  .replace(/\\/g, "/");

function loadPluginData(): Record<string, unknown> {
  return JSON.parse(fs.readFileSync(dataFile, "utf-8")) as Record<string, unknown>;
}

async function savePluginData(data: Record<string, unknown>): Promise<void> {
  fs.writeFileSync(dataFile, JSON.stringify(data, null, 2), "utf-8");
}

function buildEngine() {
  const data = loadPluginData();
  const settings = data as unknown as KbSettings;
  const token = (data["tokenFallback"] as string) ?? "";
  if (!token) throw new Error("data.json 无 tokenFallback，无法认证");
  const client = new KbClient(settings.serverUrl, token);
  const fsImpl = new NodeFs(vaultRoot);
  const engine = new SyncEngine({
    fs: fsImpl,
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
  return { engine, settings, fsImpl };
}

async function main(): Promise<void> {
  if (action === "sync") {
    const { engine } = buildEngine();
    await engine.runOnce("acceptance-headless");
    console.log("== sync 完成 ==");
  } else if (action === "restore") {
    // 与 main.ts restoreSuppressed 相同序列（真机验收遗留 #7 修复逻辑）
    const { engine, settings, fsImpl } = buildEngine();
    const suppression = new Suppression(fsImpl, `${settings.systemFolder}/KnowledgeInbox/suppression.json`);
    const removed = await suppression.unsuppressAll();
    if (removed.length === 0) {
      console.log("当前没有被抑制的条目。");
      return;
    }
    const commits = new CommitStore(fsImpl, `${settings.systemFolder}/KnowledgeInbox/commits`);
    let cleared = 0;
    for (const itemId of removed) cleared += await commits.removeForItem(itemId);
    await engine.rebuildIndex();
    console.log(`已恢复 ${removed.length} 条（清理 ${cleared} 个本地提交标记）：${removed.join(", ")}`);
  } else {
    const { settings, fsImpl } = buildEngine();
    const data = loadPluginData();
    const suppression = new Suppression(fsImpl, `${settings.systemFolder}/KnowledgeInbox/suppression.json`);
    const commits = new CommitStore(fsImpl, `${settings.systemFolder}/KnowledgeInbox/commits`);
    console.log("syncState:", JSON.stringify(data["syncState"]));
    console.log("suppressed:", JSON.stringify(await suppression.list()));
    for (const r of await commits.all()) {
      console.log(`commit ${r.item_id} r${r.bundle_revision} ack=${r.ack_sent} note=${r.note_path}`);
    }
  }
}

void main().catch((err) => {
  console.error("执行失败:", err);
  process.exit(1);
});
