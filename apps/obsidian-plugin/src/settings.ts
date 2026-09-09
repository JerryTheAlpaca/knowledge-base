/** 设置、秘密存储与设置面板
 * （docs/02 §13.3；docs/08 §8.1、§8.2、§8.3、§9）。
 *
 * 秘密存储规则（docs/08 §8.3）：
 * - 服务 Token 可沿用旧的 data.json 降级（历史行为，已明确提示）。
 * - 模型 API Key（线上绑定导入的与本机独立配置的）**不得**套用该明文降级：
 *   秘密存储不可用时只提供会话内使用方式，重启后重新配置，不阻塞原始资料同步。
 */

import { App, Notice, PluginSettingTab, Setting } from "obsidian";
import type { KbSettings, LocalModelConfig, LocalModelMode } from "./types";
import { ANALYSIS_SCHEMA_VERSION, LAYOUT_VERSION } from "./types";

export const DEFAULT_LOCAL_MODEL: LocalModelConfig = {
  mode: "follow_cloud",
  cloudProfileId: "",
  local: { name: "", baseUrl: "", model: "", secretRef: "kb-local-llm-key" },
  pinnedProfileVersion: null,
  pinnedCredentialVersion: null,
  pinnedEndpoint: "",
  pinnedModel: "",
  awaitingSync: false,
};

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
  localModel: DEFAULT_LOCAL_MODEL,
  localOrganizeEnabled: false,
  autoPrepareOnSync: false,
  organizeDeviceId: "",
  analysisSchemaVersion: ANALYSIS_SCHEMA_VERSION,
  layoutVersion: LAYOUT_VERSION,
};

/** Obsidian SecretStorage 特性检测。
 *
 * 服务 Token 允许 data.json 降级；模型 Key 走 `getSecretStrict`，
 * 无 SecretStorage 时返回 null（调用方改用会话内方式），绝不写明文。
 */
export class SecretBridge {
  readonly available: boolean;
  /** 会话内秘密：SecretStorage 不可用时的临时通道，插件重启即失效。 */
  private sessionSecrets = new Map<string, string>();
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

  // ---- 服务 Token：允许降级（历史行为） ----

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

  // ---- 模型 API Key：禁止明文降级（docs/08 §8.3） ----

  /** 读取模型 Key：秘密存储优先，其次会话内临时值；两者都没有返回 null。 */
  async getSecretStrict(ref: string): Promise<string | null> {
    if (this.available && this.store.getSecret) {
      try {
        const v = await this.store.getSecret(ref);
        if (v) return v;
      } catch {
        // 落到会话内通道
      }
    }
    return this.sessionSecrets.get(ref) ?? null;
  }

  /** 写入模型 Key。返回 true 表示持久化成功；false 表示仅会话内可用。 */
  async setSecretStrict(ref: string, value: string): Promise<boolean> {
    if (this.available && this.store.setSecret) {
      await this.store.setSecret(ref, value);
      this.sessionSecrets.set(ref, value);
      return true;
    }
    this.sessionSecrets.set(ref, value);
    new Notice("当前 Obsidian 版本无 SecretStorage：该 API Key 仅在本次会话内可用，重启后需要重新配置。");
    return false;
  }

  async clearSecretStrict(ref: string): Promise<void> {
    if (this.available && this.store.deleteSecret) {
      await this.store.deleteSecret(ref).catch(() => undefined);
    }
    this.sessionSecrets.delete(ref);
  }

  /** 断开账号时清理该账号导入的线上 Key（本地独立配置保持独立，docs/08 §8.3）。 */
  async clearImportedCloudSecrets(refs: string[]): Promise<void> {
    for (const ref of refs) await this.clearSecretStrict(ref);
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

/** 线上配置摘要（设置面板展示与本地整理配置选择用）。 */
export interface CloudProfileOption {
  id: string;
  kind: string;
  model: string;
  endpoint: string;
  version: number;
  configured: boolean;
  credentialVersion: number | null;
  boundLocally: boolean;
}

export interface SettingsTabHooks {
  onSave: () => Promise<void>;
  onLogin: () => Promise<void>;
  onDisconnect: () => Promise<void>;
  loadCloudProfiles: () => Promise<CloudProfileOption[]>;
  /** 绑定线上 Key 到本设备（docs/08 §8.3）；失败时抛出可展示的错误。 */
  onBindLocalKey: (profileId: string) => Promise<string>;
  onUnbindLocalKey: (profileId: string) => Promise<string>;
  /** 测试本地整理连接（直连所选模型服务）。 */
  onTestLocalModel: () => Promise<string>;
  onOpenOrganizePanel: () => void;
}

const MODE_LABELS: Record<LocalModelMode, string> = {
  follow_cloud: "与云端提炼使用同一配置",
  cloud_profile: "选择另一线上配置",
  local_profile: "使用本地独立配置",
};

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
    containerEl.empty();
    containerEl.createEl("h2", { text: "Knowledge Inbox 设置" });

    // ---- 账号与服务器 ----
    containerEl.createEl("h3", { text: "账号与服务器" });
    new Setting(containerEl)
      .setName("服务器地址")
      .setDesc("例如 https://kb.example.com")
      .addText((t) => t.setValue(this.settings.serverUrl).onChange(async (v) => {
        this.settings.serverUrl = v.trim();
        await this.hooks.onSave();
      }));

    const account = new Setting(containerEl)
      .setName("账号")
      .setDesc("点击登录后打开系统浏览器：使用统一账号（与记账/火车足迹相同）在网页上批准本设备。")
      .addButton((b) => b.setButtonText("登录账号").setCta().onClick(async () => {
        await this.hooks.onLogin();
        this.display();
      }));
    if (this.settings.deviceId) {
      account.addButton((b) => b.setButtonText("断开设备").setWarning().onClick(async () => {
        await this.hooks.onDisconnect();
        this.display();
      }));
    }

    new Setting(containerEl)
      .setName("设备名称")
      .setDesc("登录时展示给网页确认的名称。")
      .addText((t) => t.setValue(this.settings.deviceName).onChange(async (v) => {
        this.settings.deviceName = v || "Obsidian 桌面";
        await this.hooks.onSave();
      }));

    const pairInfo = containerEl.createEl("p", {
      text: this.settings.deviceId
        ? `已登录：device ${this.settings.deviceId.slice(0, 8)}…（退出网站不影响本设备；点「断开设备」撤销其凭据）`
        : "尚未登录。",
    });
    pairInfo.addClass("kb-muted");

    // ---- 模型设置（docs/08 §8.2） ----
    containerEl.createEl("h3", { text: "模型设置" });
    const cloudHost = containerEl.createEl("div");
    cloudHost.createEl("p", {
      text: "云端提炼：服务器保存 Key，用于关机时的单篇提炼。",
    }).addClass("kb-muted");
    const cloudSelectHost = cloudHost.createEl("div");
    void this.renderCloudProfiles(cloudSelectHost);

    const localHost = containerEl.createEl("div");
    localHost.createEl("p", {
      text: "本地整理：插件直接调用所选模型服务，必要的 Digest／Knowledge 内容会发送给模型供应商；"
        + "本系统云端不接收这些内容。使用远程 API 时仍需联网，不是完全离线。",
    }).addClass("kb-muted");

    new Setting(localHost)
      .setName("本地整理模型")
      .setDesc("整理知识库时使用哪个 Key。切换本地模式不会修改云端默认值。")
      .addDropdown((d) => {
        for (const [value, label] of Object.entries(MODE_LABELS)) d.addOption(value, label);
        d.setValue(this.settings.localModel.mode);
        d.onChange(async (v) => {
          this.settings.localModel.mode = v as LocalModelMode;
          this.settings.localModel.awaitingSync = false;
          await this.hooks.onSave();
          this.display();
        });
      });

    if (this.settings.localModel.mode === "cloud_profile") {
      new Setting(localHost)
        .setName("使用的线上配置")
        .setDesc("固定一个本人线上配置；该配置的 Key 需要绑定到本设备才能本地调用。")
        .addDropdown((d) => {
          d.addOption("", "（请选择）");
          for (const p of this._cloudProfiles.filter((x) => x.kind === "llm")) {
            d.addOption(p.id, `${p.model}（v${p.version}${p.configured ? "" : "，无 Key"}）`);
          }
          d.setValue(this.settings.localModel.cloudProfileId);
          d.onChange(async (v) => {
            this.settings.localModel.cloudProfileId = v;
            this.settings.localModel.awaitingSync = false;
            await this.hooks.onSave();
            this.display();
          });
        });
    }

    if (this.settings.localModel.mode === "local_profile") {
      const loc = this.settings.localModel.local;
      new Setting(localHost)
        .setName("配置名称")
        .addText((t) => t.setValue(loc.name).onChange(async (v) => {
          loc.name = v.trim();
          await this.hooks.onSave();
        }));
      new Setting(localHost)
        .setName("API Base URL")
        .setDesc("例如 https://api.deepseek.com/v1")
        .addText((t) => t.setValue(loc.baseUrl).onChange(async (v) => {
          loc.baseUrl = v.trim();
          await this.hooks.onSave();
        }));
      new Setting(localHost)
        .setName("模型名")
        .addText((t) => t.setValue(loc.model).onChange(async (v) => {
          loc.model = v.trim();
          await this.hooks.onSave();
        }));
      new Setting(localHost)
        .setName("API Key")
        .setDesc("只保存在本机秘密存储，不进入 Vault 笔记、任务文件或日志。")
        .addText((t) => {
          t.inputEl.type = "password";
          t.setPlaceholder("粘贴后失焦保存");
          t.onChange(async (v) => {
            if (!v.trim()) return;
            const persisted = await this.pluginSecrets().setSecretStrict(loc.secretRef, v.trim());
            new Notice(persisted ? "API Key 已保存到本机秘密存储。" : "API Key 仅本次会话可用。");
          });
        })
        .addButton((b) => b.setButtonText("清除").setWarning().onClick(async () => {
          await this.pluginSecrets().clearSecretStrict(loc.secretRef);
          new Notice("已清除本机保存的 API Key。");
        }));
    }

    new Setting(localHost)
      .setName("测试本地连接")
      .setDesc("只验证所选模型服务可达；不发送任何笔记正文。")
      .addButton((b) => b.setButtonText("测试").onClick(async () => {
        b.setDisabled(true);
        try {
          const result = await this.hooks.onTestLocalModel();
          new Notice(result, 6000);
        } catch (err) {
          new Notice(`测试失败：${err instanceof Error ? err.message : String(err)}`, 8000);
        } finally {
          b.setDisabled(false);
        }
      }));

    const boundNote = containerEl.createEl("p");
    boundNote.addClass("kb-muted");
    boundNote.setText(
      this.settings.localModel.awaitingSync
        ? "等待配置同步：无法确认线上配置时不静默换 Key；可主动切到独立本地配置。"
        : "复用线上配置时，需把该 Key 绑定到本设备（见下方「线上 Key 绑定」）。",
    );

    // ---- 线上 Key 绑定（docs/08 §8.3） ----
    containerEl.createEl("h3", { text: "线上 Key 绑定" });
    const bindHost = containerEl.createEl("div");
    void this.renderBindings(bindHost);

    // ---- 整理知识库 ----
    containerEl.createEl("h3", { text: "整理知识库" });
    new Setting(containerEl)
      .setName("启用本地整理")
      .setDesc("未启用时，云端提炼、网页阅读与 Source/Digest 投递照常工作。")
      .addToggle((t) => t.setValue(this.settings.localOrganizeEnabled).onChange(async (v) => {
        this.settings.localOrganizeEnabled = v;
        await this.hooks.onSave();
      }));
    new Setting(containerEl)
      .setName("新 Digest 入库后自动准备整理候选")
      .setDesc("后台准备不等于自动写入第三层；是否自动应用仍按指定主题的设置执行。")
      .addToggle((t) => t.setValue(this.settings.autoPrepareOnSync).onChange(async (v) => {
        this.settings.autoPrepareOnSync = v;
        await this.hooks.onSave();
      }));
    new Setting(containerEl)
      .setName("本机整理设备")
      .setDesc("每个 Vault 指定一台本地整理设备；换机后先停止旧机整理并检查版本。")
      .addButton((b) => b.setButtonText("打开整理面板").setCta().onClick(() => this.hooks.onOpenOrganizePanel()));

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
      ["knowledgeFolder", "03 Knowledge 目录"],
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

  /** 由 main 注入的秘密桥（避免设置面板直接依赖插件实例）。 */
  private secretsGetter: (() => SecretBridge) | null = null;
  registerSecrets(getter: () => SecretBridge): void {
    this.secretsGetter = getter;
  }

  private pluginSecrets(): SecretBridge {
    if (!this.secretsGetter) throw new Error("SecretBridge 未注册");
    return this.secretsGetter();
  }

  private async renderCloudProfiles(host: HTMLElement): Promise<void> {
    host.empty();
    let profiles: CloudProfileOption[] = [];
    try {
      profiles = await this.hooks.loadCloudProfiles();
    } catch (err) {
      host.createEl("p", {
        text: `无法读取线上配置：${err instanceof Error ? err.message : String(err)}`,
      }).addClass("kb-muted");
      return;
    }
    this._cloudProfiles = profiles;
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

  private async renderBindings(host: HTMLElement): Promise<void> {
    host.empty();
    host.createEl("p", {
      text: "把某个线上配置的 Key 配置到本设备，用于本地直接调用模型服务。"
        + "服务端撤销绑定会阻止再次领取，但无法远程收回已下发的供应商 Key；"
        + "彻底失效需在供应商处撤销。",
    }).addClass("kb-muted");
    if (!this.settings.deviceId) {
      host.createEl("p", { text: "请先登录账号后再绑定。" }).addClass("kb-muted");
      return;
    }
    let profiles: CloudProfileOption[];
    try {
      profiles = await this.hooks.loadCloudProfiles();
    } catch (err) {
      host.createEl("p", { text: `读取失败：${err instanceof Error ? err.message : String(err)}` }).addClass("kb-muted");
      return;
    }
    this._cloudProfiles = profiles;
    for (const p of profiles.filter((x) => x.kind === "llm")) {
      new Setting(host)
        .setName(p.model)
        .setDesc(`v${p.version} · ${p.endpoint}${p.configured ? "" : " · 服务器上没有可用 Key"}`)
        .addButton((b) => {
          b.setButtonText(p.boundLocally ? "重新绑定" : "配置到本设备");
          if (!p.configured) b.setDisabled(true);
          b.onClick(async () => {
            b.setDisabled(true);
            try {
              const msg = await this.hooks.onBindLocalKey(p.id);
              new Notice(msg, 6000);
              this.display();
            } catch (err) {
              new Notice(`绑定失败：${err instanceof Error ? err.message : String(err)}`, 8000);
              b.setDisabled(false);
            }
          });
        })
        .addButton((b) => {
          if (!p.boundLocally) return;
          b.setButtonText("解绑本机").setWarning().onClick(async () => {
            try {
              const msg = await this.hooks.onUnbindLocalKey(p.id);
              new Notice(msg, 6000);
              this.display();
            } catch (err) {
              new Notice(`解绑失败：${err instanceof Error ? err.message : String(err)}`, 8000);
            }
          });
        });
    }
  }
}
