/** 设置与 SecretStorage 桥接（docs/02 §13.3）：Token 存 SecretStorage，配置只存引用。 */

import { App, Notice, PluginSettingTab, Setting } from "obsidian";
import type { KbSettings } from "./types";

export const DEFAULT_SETTINGS: KbSettings = {
  serverUrl: "",
  tokenRef: "kb-service-token",
  deviceName: "Obsidian 桌面",
  inboxFolder: "00 Inbox",
  sourcesFolder: "10 Sources",
  assetsFolder: "90 Assets",
  systemFolder: "99 System",
  autoSync: true,
  deviceId: "",
  userId: "",
};

/** Obsidian SecretStorage 特性检测；旧版本降级 data.json 并给出警告。 */
export class SecretBridge {
  readonly available: boolean;
  private store: {
    getSecret?: (n: string) => Promise<string | null>;
    setSecret?: (n: string, v: string) => Promise<void>;
    deleteSecret?: (n: string) => Promise<void>;
  };

  constructor(private app: App) {
    const candidate = (app as unknown as { secretStorage?: unknown }).secretStorage;
    this.store = (candidate && typeof candidate === "object" ? candidate : {}) as typeof this.store;
    this.available = typeof this.store.getSecret === "function" && typeof this.store.setSecret === "function";
  }

  async getToken(ref: string): Promise<string | null> {
    if (this.available && this.store.getSecret) {
      try {
        return await this.store.getSecret(ref);
      } catch {
        return null;
      }
    }
    const data = await this.loadFallback();
    return (data.tokenFallback as string | undefined) ?? null;
  }

  async setToken(ref: string, value: string): Promise<boolean> {
    if (this.available && this.store.setSecret) {
      await this.store.setSecret(ref, value);
      return true;
    }
    const data = await this.loadFallback();
    data.tokenFallback = value;
    await this.saveFallback(data);
    new Notice("当前 Obsidian 版本无 SecretStorage，服务 Token 已降级保存在本插件数据中（仅本机）。");
    return false;
  }

  async clearToken(ref: string): Promise<void> {
    if (this.available && this.store.deleteSecret) {
      await this.store.deleteSecret(ref).catch(() => undefined);
    }
    const data = await this.loadFallback();
    if (data.tokenFallback !== undefined) {
      delete data.tokenFallback;
      await this.saveFallback(data);
    }
  }

  // 降级通道：借用插件 data.json 的未注册键，由 main 提供 loadData/saveData
  private fallbackLoader: (() => Promise<Record<string, unknown>>) | null = null;
  private fallbackSaver: ((d: Record<string, unknown>) => Promise<void>) | null = null;

  registerFallback(loader: () => Promise<Record<string, unknown>>, saver: (d: Record<string, unknown>) => Promise<void>): void {
    this.fallbackLoader = loader;
    this.fallbackSaver = saver;
  }

  private async loadFallback(): Promise<Record<string, unknown>> {
    if (!this.fallbackLoader) return {};
    return this.fallbackLoader();
  }

  private async saveFallback(data: Record<string, unknown>): Promise<void> {
    if (this.fallbackSaver) await this.fallbackSaver(data);
  }
}

export class KbSettingTab extends PluginSettingTab {
  constructor(
    app: App,
    plugin: object,
    private settings: KbSettings,
    private onSave: () => Promise<void>,
    private onPair: (code: string, deviceName: string) => Promise<void>,
  ) {
    super(app, plugin as never);
  }

  display(): void {
    const { containerEl } = this;
    containerEl.empty();
    containerEl.createEl("h2", { text: "Knowledge Inbox 设置" });

    new Setting(containerEl)
      .setName("服务器地址")
      .setDesc("例如 https://kb.example.com")
      .addText((t) => t.setValue(this.settings.serverUrl).onChange(async (v) => {
        this.settings.serverUrl = v.trim();
        await this.onSave();
      }));

    new Setting(containerEl)
      .setName("配对")
      .setDesc("输入服务器 CLI 生成的一次性配对码（桌面设备），换取服务 Token 存入本机密钥存储。")
      .addText((t) => t.setPlaceholder("配对码").setValue(""))
      .addText((t) => t.setPlaceholder("设备名称").setValue(this.settings.deviceName).onChange(async (v) => {
        this.settings.deviceName = v || "Obsidian 桌面";
        await this.onSave();
      }))
      .addButton((b) => b.setButtonText("配对").onClick(async () => {
        const code = (containerEl.querySelector("input[placeholder='配对码']") as HTMLInputElement | null)?.value ?? "";
        if (!code) {
          new Notice("请先输入配对码");
          return;
        }
        await this.onPair(code.trim(), this.settings.deviceName);
        this.display();
      }));

    const pairInfo = containerEl.createEl("p", {
      text: this.settings.deviceId
        ? `已配对：device ${this.settings.deviceId.slice(0, 8)}…（Token 引用：${this.settings.tokenRef}）`
        : "尚未配对。",
    });
    pairInfo.addClass("kb-muted");

    new Setting(containerEl).setName("自动同步").setDesc("启动后自动拉取增量（默认开启；手动同步随时可用）。")
      .addToggle((t) => t.setValue(this.settings.autoSync).onChange(async (v) => {
        this.settings.autoSync = v;
        await this.onSave();
      }));

    const folders = containerEl.createEl("div");
    folders.createEl("h3", { text: "Vault 目录" });
    for (const [key, label] of [
      ["inboxFolder", "收件箱索引目录"],
      ["sourcesFolder", "Source 笔记目录"],
      ["assetsFolder", "原始材料目录"],
      ["systemFolder", "系统状态目录"],
    ] as const) {
      new Setting(folders)
        .setName(label)
        .addText((t) => t.setValue(this.settings[key]).onChange(async (v) => {
          this.settings[key] = v.replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
          await this.onSave();
        }));
    }
  }
}
