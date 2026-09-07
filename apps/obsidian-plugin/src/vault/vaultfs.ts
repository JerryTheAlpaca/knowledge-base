/** Obsidian Vault 之上的文件辅助：ensureFolder、原子化 rename、二进制读写。 */

import { App, normalizePath } from "obsidian";
import type { FsLike } from "./records";

function dirnameOf(path: string): string {
  const idx = path.lastIndexOf("/");
  return idx > 0 ? path.slice(0, idx) : "";
}

export class VaultFs implements FsLike {
  constructor(private app: App) {}

  private p(path: string): string {
    return normalizePath(path);
  }

  async exists(path: string): Promise<boolean> {
    try {
      return await this.app.vault.adapter.exists(this.p(path));
    } catch {
      return false;
    }
  }

  async read(path: string): Promise<string> {
    return this.app.vault.adapter.read(this.p(path));
  }

  async write(path: string, data: string): Promise<void> {
    await this.ensureFolder(dirnameOf(path));
    await this.app.vault.adapter.write(this.p(path), data);
  }

  async writeBinary(path: string, data: ArrayBuffer): Promise<void> {
    await this.ensureFolder(dirnameOf(path));
    await this.app.vault.adapter.writeBinary(this.p(path), data);
  }

  async readBinary(path: string): Promise<ArrayBuffer> {
    return this.app.vault.adapter.readBinary(this.p(path));
  }

  async remove(path: string): Promise<void> {
    try {
      await this.app.vault.adapter.remove(this.p(path));
    } catch {
      // 文件可能已不存在
    }
  }

  async list(path: string): Promise<string[]> {
    try {
      const res = await this.app.vault.adapter.list(this.p(path));
      return res.files ?? [];
    } catch {
      return [];
    }
  }

  async rename(from: string, to: string): Promise<void> {
    await this.ensureFolder(dirnameOf(to));
    await this.app.vault.adapter.rename(this.p(from), this.p(to));
  }

  async ensureFolder(dir: string): Promise<void> {
    if (!dir) return;
    const segments = this.p(dir).split("/");
    let cur = "";
    for (const seg of segments) {
      cur = cur ? `${cur}/${seg}` : seg;
      if (!cur) continue;
      if (!(await this.exists(cur))) {
        await this.app.vault.adapter.mkdir(cur).catch(() => undefined);
      }
    }
  }

  /** 读-改-写同一回调，避免读取后用户编辑造成覆盖（docs/02 §12.3）。 */
  async processNote(path: string, fn: (data: string) => string): Promise<void> {
    const vault = this.app.vault as unknown as {
      process?: (p: string, f: (d: string) => string) => Promise<string>;
    };
    const p = this.p(path);
    if (typeof vault.process === "function") {
      await vault.process.call(this.app.vault, p, fn);
    } else {
      const data = await this.read(p);
      await this.write(p, fn(data));
    }
  }
}
