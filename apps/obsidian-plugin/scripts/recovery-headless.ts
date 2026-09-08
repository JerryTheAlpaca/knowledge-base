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
import type { VaultFs } from "../src/vault/vaultfs";
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

/** 与 Obsidian VaultAdapter 同语义的 Node 实现（vault 相对路径 → vaultRoot 下文件）。 */
class NodeFs implements VaultFs {
  constructor(private root: string) {}

  private p(p: string): string {
    const norm = p.replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
    return path.join(this.root, ...norm.split("/"));
  }

  async exists(p: string): Promise<boolean> {
    try { return fs.existsSync(this.p(p)); } catch { return false; }
  }
  async read(p: string): Promise<string> {
    return fsp.readFile(this.p(p), "utf-8");
  }
  async write(p: string, data: string): Promise<void> {
    await this.ensureFolder(p.split("/").slice(0, -1).join("/"));
    await fsp.writeFile(this.p(p), data, "utf-8");
  }
  async writeBinary(p: string, data: ArrayBuffer): Promise<void> {
    await this.ensureFolder(p.split("/").slice(0, -1).join("/"));
    await fsp.writeFile(this.p(p), Buffer.from(data));
  }
  async readBinary(p: string): Promise<ArrayBuffer> {
    const buf = await fsp.readFile(this.p(p));
    return buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength) as ArrayBuffer;
  }
  async remove(p: string): Promise<void> {
    try { await fsp.rm(this.p(p), { recursive: true, force: true }); } catch { /* 已不存在 */ }
  }
  async list(p: string): Promise<string[]> {
    try {
      const dir = this.p(p);
      const entries = await fsp.readdir(dir, { withFileTypes: true });
      return entries.filter((e) => e.isFile()).map((e) => `${p.replace(/\/$/, "")}/${e.name}`);
    } catch { return []; }
  }
  async rename(from: string, to: string): Promise<void> {
    await this.ensureFolder(to.split("/").slice(0, -1).join("/"));
    await fsp.rename(this.p(from), this.p(to));
  }
  async ensureFolder(dir: string): Promise<void> {
    if (!dir) return;
    await fsp.mkdir(this.p(dir), { recursive: true });
  }
  async processNote(p: string, fn: (data: string) => string): Promise<void> {
    const data = await this.read(p);
    await this.write(p, fn(data));
  }
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
