/**
 * 设置、秘密存储与设置面板
 * （docs/02 §13.3；docs/24 §8）。
 *
 * 本插件只做同步：服务器地址、账号、同步开关、Vault 目录。
 * 服务 Token 允许沿用旧的 data.json 降级（历史行为，会明确提示）。
 */

import { App, Notice, PluginSettingTab, Setting } from "obsidian";
import type { KbSettings } from "./types";
import { CONTENT_FORMAT_VERSION, LAYOUT_VERSION } from "./types";

export const DEFAULT_SETTINGS: KbSettings = {
  serverUrl: "",
  tokenRef: "kb-service-token",
  deviceName: "Obsidian 桌面",
  inboxFolder: "00 Inbox",
  sourcesFolder: "01 Sources",
  digestsFolder: "02 Digests",
  knowledgeFolder: "03 Knowledge",
  assetsFolder: "01 Sources/_assets",
  systemFolder: "99 System",
  autoSync: true,
  deviceId: "",
  userId: "",
  cloudProfileId: "",
  contentFormatVersion: CONTENT_FORMAT_VERSION,
  layoutVersion: LAYOUT_VERSION,
};

/** Obsidian SecretStorage 特性检测；不可用时服务 Token 降级保存在插件数据里（仅本机）。 */
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
    return (await this.fallbackLoader()) ?? {};
  }

  private async saveFallback(data: Record<string, unknown>): Promise<void> {
    if (this.fallbackSaver) await this.fallbackSaver(data);
  }
}

/** 线上配置摘要（设置面板展示用；不含 Key）。 */
export interface CloudProfileOption {
  id: string;
  kind: string;
  model: string;
  endpoint: string;
  version: number;
  configured: boolean;
}

export interface SettingsTabHooks {
  onSave: () => Promise<void>;
  onLogin: () => Promise<void>;
  onDisconnect: () => Promise<void>;
  onRenameDevice: (name: string) => Promise<void>;
  loadCloudProfiles: () => Promise<CloudProfileOption[]>;
}

export class KbSettingTab extends PluginSettingTab {
  constructor(
    app: App,
    plugin: object,
    private settings: KbSettings,
    private hooks: SettingsTabHooks,
  ) {
    super(app, plugin as never);
  }

  display(): void {
    const { containerEl } = this;
    // 每次打开设置重新拉一次线上配置（本页所有消费方共享同一次请求）
    this.cloudProfilesLoad = null;
    containerEl.empty();
    containerEl.createEl("h2", { text: "Golden-Rose-Inbox 设置" });

    // ---- 账号与服务器 ----
    containerEl.createEl("h3", { text: "账号与服务器" });
    new Setting(containerEl)
      .setName("服务器地址")
      .setDesc("例如 https://kb.example.com")
      .addText((t) => t.setValue(this.settings.serverUrl).onChange(async (v) => {
        this.settings.serverUrl = v.trim();
        await this.hooks.onSave();
      }));

    // 登录与断开互斥：同时显示两个按钮会让人搞不清当前状态
    const account = new Setting(containerEl).setName("账号");
    if (this.settings.deviceId) {
      account
        .setDesc("本设备已授权：网页端的设备列表里能看到它，同步自动进行。")
        .addButton((b) => b.setButtonText("断开设备").setWarning().onClick(async () => {
          await this.hooks.onDisconnect();
          this.display();
        }));
    } else {
      account
        .setDesc("点击登录后打开系统浏览器：使用统一账号在网页上批准本设备。")
        .addButton((b) => b.setButtonText("登录账号").setCta().onClick(async () => {
          await this.hooks.onLogin();
          this.display();
        }));
    }

    // 设备名在失焦或按回车时提交一次：TextComponent.onChange 绑在 input 上，
    // 每次按键都会触发，不能拿它打服务端。
    new Setting(containerEl)
      .setName("设备名称")
      .setDesc("登录时展示给网页确认的名称；改名后同步到网页端的设备列表。")
      .addText((t) => {
        t.setValue(this.settings.deviceName);
        const commit = async () => {
          const next = t.getValue().trim() || "Obsidian 桌面";
          t.setValue(next);
          if (next === this.settings.deviceName) return;
          this.settings.deviceName = next;
          await this.hooks.onSave();
          await this.hooks.onRenameDevice(next);
        };
        t.inputEl.addEventListener("change", () => void commit());
        t.inputEl.addEventListener("blur", () => void commit());
        t.inputEl.addEventListener("keydown", (ev) => {
          if (ev.key === "Enter") {
            ev.preventDefault();
            void commit();
            t.inputEl.blur();
          }
        });
      });

    // ---- 云端提炼配置（模型 Key 由服务器保管，docs/24 §8） ----
    containerEl.createEl("h3", { text: "云端提炼" });
    const cloudHost = containerEl.createEl("div");
    cloudHost.createEl("p", {
      text: "云端提炼在服务器完成：服务器保存 Key，用于关机时的单篇提炼。",
    }).addClass("kb-muted");
    const cloudSelectHost = cloudHost.createEl("div");
    void this.renderCloudProfiles(cloudSelectHost);

    // ---- 同步 ----
    containerEl.createEl("h3", { text: "同步" });
    new Setting(containerEl).setName("自动同步").setDesc("启动后自动拉取增量（默认开启；手动同步随时可用）。")
      .addToggle((t) => t.setValue(this.settings.autoSync).onChange(async (v) => {
        this.settings.autoSync = v;
        await this.hooks.onSave();
      }));

    // ---- Vault 目录 ----
    const folders = containerEl.createEl("div");
    folders.createEl("h3", { text: "Vault 目录" });
    for (const [key, label] of [
      ["inboxFolder", "00 收件箱目录"],
      ["sourcesFolder", "01 Sources 目录"],
      ["digestsFolder", "02 Digests 目录"],
      ["systemFolder", "99 系统状态目录"],
    ] as const) {
      new Setting(folders)
        .setName(label)
        .addText((t) => t.setValue(this.settings[key]).onChange(async (v) => {
          this.settings[key] = v.replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
          await this.hooks.onSave();
        }));
    }
    folders.createEl("p", {
      text: `附件按来源版本保存在 ${this.settings.sourcesFolder}/_assets/<item_id>/source-000001/ 下。`,
    }).addClass("kb-muted");
  }

  private _cloudProfiles: CloudProfileOption[] = [];
  /** 本页共享的配置列表加载：一次 display 只发一次请求。 */
  private cloudProfilesLoad: Promise<CloudProfileOption[]> | null = null;

  private loadCloudProfilesOnce(): Promise<CloudProfileOption[]> {
    if (!this.cloudProfilesLoad) {
      this.cloudProfilesLoad = this.hooks.loadCloudProfiles().then((profiles) => {
        this._cloudProfiles = profiles;
        return profiles;
      }).catch((err) => {
        this.cloudProfilesLoad = null; // 失败后允许下次重试
        throw err;
      });
    }
    return this.cloudProfilesLoad;
  }

  private async renderCloudProfiles(host: HTMLElement): Promise<void> {
    host.empty();
    let profiles: CloudProfileOption[];
    try {
      profiles = await this.loadCloudProfilesOnce();
    } catch (err) {
      host.createEl("p", {
        text: `无法读取线上配置：${err instanceof Error ? err.message : String(err)}`,
      }).addClass("kb-muted");
      return;
    }
    const llm = profiles.filter((p) => p.kind === "llm");
    new Setting(host)
      .setName("云端提炼配置")
      .setDesc("由服务器 /v1/settings.default_profile_id 决定；此处可单独覆盖。")
      .addDropdown((d) => {
        d.addOption("", "（跟随服务器默认）");
        for (const p of llm) d.addOption(p.id, `${p.model}（v${p.version}${p.configured ? "" : "，无 Key"}）`);
        d.setValue(this.settings.cloudProfileId);
        d.onChange(async (v) => {
          this.settings.cloudProfileId = v;
          await this.hooks.onSave();
        });
      });
    if (!llm.length) {
      host.createEl("p", { text: "服务器上还没有 llm 配置；可在网页收件箱的「模型与账号」中创建。" })
        .addClass("kb-muted");
    }
  }
}
