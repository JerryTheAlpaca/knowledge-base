/**
 * 与 Obsidian VaultAdapter 同语义的 Node 实现（vault 相对路径 → vaultRoot 下文件）。
 * 供 recovery-headless / acceptance-headless 共用（真机验收沉淀工具，docs/06）。
 */

import * as fs from "node:fs";
import * as fsp from "node:fs/promises";
import * as path from "node:path";
import type { VaultFs } from "../src/vault/vaultfs";

export class NodeFs implements VaultFs {
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
  async listDirs(p: string): Promise<string[]> {
    try {
      const dir = this.p(p);
      const entries = await fsp.readdir(dir, { withFileTypes: true });
      return entries.filter((e) => e.isDirectory()).map((e) => `${p.replace(/\/$/, "")}/${e.name}`);
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
