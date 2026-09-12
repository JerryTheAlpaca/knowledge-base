/**
 * 插件入口：生命周期、命令、状态栏与轮询
 * （docs/02 §13.3；docs/08 §8.1、§8.2、§8.3、§9）。
 *
 * 三层：01 Sources（原始证据）／02 Digests（单篇提炼）／03 Knowledge（主题认知）。
 * 本地整理全部在本机执行，模型直连用户选定的服务；本系统云端不接收 Knowledge、
 * 主题索引或本地整理任务。
 */

import { ItemView, Notice, Plugin, TFile, WorkspaceLeaf } from "obsidian";
import { KbClient } from "./api";
import { KbSettingTab, SecretBridge, DEFAULT_SETTINGS, defaultLocalModel } from "./settings";
import { SyncEngine, type SyncState } from "./sync/engine";
import { VaultFs } from "./vault/vaultfs";
import { CommitStore, Suppression } from "./vault/records";
import type { EngineStatus, KbSettings } from "./types";
import { OrganizeService } from "./knowledge/organize";
import { KnowledgeIndexStore } from "./knowledge/index";
import { ObsidianLocalTransport } from "./providers/transport";
import {
  bindingSecretRef,
  generateLocal,
  pinConfig,
  resolveLocalModel,
  testLocalConnection,
  type CloudProfileSource,
  type ModelCallerLike,
} from "./providers/local";
import { OrganizePanelView, VIEW_TYPE_ORGANIZE } from "./views/organizePanel";

const VIEW_TYPE_STATUS = "knowledge-inbox-status";
const POLL_INTERVAL_MS = 60_000;
const MAX_BACKOFF_MS = 10 * 60_000;

interface PluginData extends Partial<KbSettings> {
  syncState?: SyncState;
  tokenFallback?: string;
  /** 由线上绑定导入的 Key 的秘密引用名（解绑/断开账号时清理）。 */
  importedSecretRefs?: string[];
}

class StatusView extends ItemView {
  constructor(leaf: WorkspaceLeaf, private plugin: KbPlugin) {
    super(leaf);
    this.navigation = false;
  }

  getViewType(): string { return VIEW_TYPE_STATUS; }
  getDisplayText(): string { return "Knowledge Inbox 状态"; }
  getIcon(): string { return "inbox"; }

  async onOpen(): Promise<void> {
    this.render();
    this.registerInterval(window.setInterval(() => this.render(), 5_000));
  }

  private render(): void {
    const c = this.contentEl;
    c.empty();
    c.createEl("h3", { text: "Knowledge Inbox" });
    const st = this.plugin.lastStatus;
    // lastStatus（onStatus 推送）优先，尚未跑过同步时回退到 data.json 里的
    // 持久化 syncState，避免重启后面板一直显示占位值（审查 C-02/U-04）
    const sync = this.plugin.syncState;
    const lines: Array<[string, string]> = [
      ["状态", st?.running ? "同步中…" : (st?.lastError ? `出错：${st.lastError}` : "就绪")],
      ["待入库", String(st?.pendingCount ?? Object.keys(sync.pending).length)],
      ["上次同步", st?.lastRunAt
        ? new Date(st.lastRunAt).toLocaleString()
        : sync.lastRunAt ? new Date(sync.lastRunAt).toLocaleString() : "—"],
      ["游标", String(st?.cursor ?? sync.cursor)],
    ];
    if (st?.epochConflict) {
      lines.push(["设备", "已不是主要写入设备；请在服务器切换后重新同步"]);
    }
    if (st?.moreEvents) {
      lines.push(["事件", "还有更多事件，将在下次同步继续"]);
    }
    if ((st?.suppressedCount ?? 0) > 0) {
      lines.push(["已放弃条目", `${st?.suppressedCount} 条（本地删除停复建或服务器已删除）`]);
    }
    for (const [k, v] of lines) {
      const row = c.createEl("div");
      row.createEl("strong", { text: `${k}：` });
      row.createEl("span", { text: v });
    }
    const btn = c.createEl("button", { text: "立即同步" });
    btn.addClass("kb-btn-wide");
    btn.addEventListener("click", () => void this.plugin.manualSync());
  }

  async onClose(): Promise<void> {
    this.contentEl.empty();
  }
}

export class KbPlugin extends Plugin {
  /** 初始值用副本，loadSettings 会覆盖；不直接引用模块常量，避免运行时污染。 */
  settings: KbSettings = { ...DEFAULT_SETTINGS, localModel: defaultLocalModel() };
  secrets!: SecretBridge;
  engine!: SyncEngine;
  organize!: OrganizeService;
  syncState: SyncState = { cursor: 0, pending: {}, lastRunAt: null };
  lastStatus: EngineStatus | null = null;

  private timer: number | null = null;
  private backoffMs = POLL_INTERVAL_MS;
  private statusBarEl: HTMLElement | null = null;
  private tokenCache: string | null = null;
  private transport = new ObsidianLocalTransport();

  async onload(): Promise<void> {
    try {
      await this.doLoad();
    } catch (err) {
      const detail = err instanceof Error ? (err.stack || err.message) : String(err);
      console.error("[kb-inbox] onload threw:", err);
      new Notice("KB Inbox 加载错误：" + detail, 0);
      throw err;
    }
  }

  private async doLoad(): Promise<void> {
    await this.loadSettings();
    this.secrets = new SecretBridge(this.app);
    this.secrets.registerFallback(
      () => this.loadData() as Promise<Record<string, unknown>>,
      (d) => this.saveData(d),
    );

    const fs = new VaultFs(this.app);
    this.engine = new SyncEngine({
      fs,
      getClient: () => this.getClient(),
      settings: () => this.settings,
      loadState: async () => {
        const data = ((await this.loadData()) ?? {}) as PluginData;
        this.syncState = data.syncState ?? { cursor: 0, pending: {}, lastRunAt: null };
        return this.syncState;
      },
      saveState: async (s) => {
        this.syncState = s;
        const data = ((await this.loadData()) ?? {}) as PluginData;
        data.syncState = s;
        await this.saveData(data);
      },
      onStatus: (st) => {
        this.lastStatus = st;
        this.updateStatusBar();
        // 失败指数退避，成功重置为默认轮询（docs/02 §13.3）
        this.backoffMs = st.lastError && !st.running
          ? Math.min(Math.max(this.backoffMs * 2, POLL_INTERVAL_MS * 2), MAX_BACKOFF_MS)
          : POLL_INTERVAL_MS;
      },
      log: (msg) => console.log(`[knowledge-inbox] ${msg}`),
      // 新 Digest 入库后按开关准备整理候选（docs/08 §8.1）；后台准备不等于自动写入
      onDigestWritten: async (itemId, digestPath) => {
        if (!this.settings.localOrganizeEnabled || !this.settings.autoPrepareOnSync) return;
        await this.organize.enqueue(itemId, digestPath);
      },
    });

    this.organize = new OrganizeService({
      fs,
      settings: () => this.settings,
      model: () => this.localModel(),
      log: (msg) => console.log(`[knowledge-inbox] ${msg}`),
      onProposalsChanged: () => this.refreshProposalIndex(),
    });

    this.statusBarEl = this.addStatusBarItem();
    this.addRibbonIcon("inbox", "Knowledge Inbox 状态", () => void this.openStatusView());
    this.addRibbonIcon("sparkles", "整理知识库", () => void this.openOrganizePanel());

    const tab = new KbSettingTab(this.app, this, this.settings, {
      onSave: () => this.saveSettings(),
      onLogin: () => this.loginWithBrowser(),
      onDisconnect: () => this.disconnectDevice(),
      loadCloudProfiles: () => this.loadCloudProfiles(),
      onBindLocalKey: (profileId) => this.bindLocalKey(profileId),
      onUnbindLocalKey: (profileId) => this.unbindLocalKey(profileId),
      onTestLocalModel: () => this.testLocalModel(),
      onOpenOrganizePanel: () => void this.openOrganizePanel(),
    });
    tab.registerSecrets(() => this.secrets);
    this.addSettingTab(tab);

    this.addCommand({ id: "sync-now", name: "立即同步", callback: () => void this.manualSync() });
    this.addCommand({ id: "show-status", name: "显示同步状态", callback: () => void this.openStatusView() });
    this.addCommand({
      id: "restore-suppressed",
      name: "恢复被删除条目的自动重建",
      callback: () => void this.restoreSuppressed(),
    });
    // 整理知识库命令（docs/08 §8.1）
    this.addCommand({
      id: "organize-current-digest",
      name: "整理当前 Digest",
      checkCallback: (checking) => {
        const path = this.activeDigestPath();
        if (!path) return false;
        if (!checking) void this.organizeCurrentDigest(path);
        return true;
      },
    });
    this.addCommand({
      id: "organize-pending",
      name: "整理待处理内容",
      callback: () => void this.openOrganizePanel(true),
    });
    this.addCommand({
      id: "show-proposals",
      name: "查看知识更新候选",
      callback: () => void this.openProposalIndex(),
    });

    this.registerView(VIEW_TYPE_STATUS, (leaf) => new StatusView(leaf, this));
    this.registerView(VIEW_TYPE_ORGANIZE, (leaf) => new OrganizePanelView(leaf, {
      service: () => this.organize,
      modelLabel: () => this.localModelLabel(),
      onOpenSettings: () => this.openSettings(),
      refreshProposalIndex: () => this.refreshProposalIndex(),
      activeDigestPath: () => this.activeDigestPath(),
      pickDigests: async () => [],
    }));

    // onLayoutReady 后再开始恢复与拉取，不阻塞编辑器启动（docs/02 §13.3）
    this.app.workspace.onLayoutReady(() => {
      void (async () => {
        // 先刷新 Token 再补发回执：recoverReceipts 依赖 getClient()，
        // tokenCache 未加载时它拿不到客户端、启动补发会静默跳过（真机验收 A10 发现）
        if (this.settings.autoSync && (await this.refreshToken())) {
          const n = await this.engine.recoverReceipts();
          if (n > 0) new Notice(`Knowledge Inbox：补发了 ${n} 条回执`);
          await this.engine.runOnce("startup");
        }
        await this.refreshProposalIndex();
        this.startTimer();
      })();
    });
  }

  onunload(): void {
    if (this.timer !== null) window.clearInterval(this.timer);
    this.timer = null;
  }

  private startTimer(): void {
    if (this.timer !== null) window.clearInterval(this.timer);
    this.timer = window.setInterval(() => {
      if (!this.settings.autoSync || this.lastStatus?.running) return;
      if (this.lastStatus?.epochConflict) return;
      if (!this.tokenCache && this.settings.serverUrl) {
        void this.refreshToken();
        return;
      }
      void this.engine.runOnce("timer");
    }, this.backoffMs);
  }

  getClient(): KbClient | null {
    if (!this.settings.serverUrl || !this.tokenCache) return null;
    return new KbClient(this.settings.serverUrl, this.tokenCache, this.settings.deviceId);
  }

  private async refreshToken(): Promise<boolean> {
    this.tokenCache = await this.secrets.getToken(this.settings.tokenRef);
    return this.tokenCache !== null;
  }

  // ---- 本地整理模型（docs/08 §8.2、§8.3） ----

  /** 线上配置读取通道；未登录时为 null（调用方据此提示「等待配置同步」）。 */
  private cloudSource(): CloudProfileSource | null {
    const client = this.getClient();
    if (!client) return null;
    return {
      listProfiles: () => client.listProfiles(),
      getDefaultProfileId: async () => (await client.getSettings()).default_profile_id,
      bindingStatus: async (profileId) => {
        const s = await client.localBindingStatus(profileId);
        return { bound: s.bound, profile_version: s.profile_version, credential_version: s.credential_version };
      },
      bind: (profileId) => client.bindLocalKey(profileId),
    };
  }

  private async localModel(): Promise<{ caller: ModelCallerLike; configRef: string }> {
    const resolved = await resolveLocalModel({
      settings: this.settings,
      secrets: this.secrets,
      cloud: this.cloudSource(),
    });
    // 批次开始时固定配置版本：进行中的任务不切换（docs/08 §8.2）
    const pinned = pinConfig(this.settings.localModel, resolved);
    if (JSON.stringify(pinned) !== JSON.stringify(this.settings.localModel)) {
      this.settings.localModel = pinned;
      await this.saveSettings();
    }
    const caller: ModelCallerLike = {
      call: (system, user) => generateLocal(this.transport, resolved, {
        system,
        user,
        maxOutputTokens: Number(resolved.capabilities.max_output_tokens ?? 4000),
        jsonMode: true,
      }),
    };
    const configRef = resolved.profileVersion
      ? `${resolved.cloudProfileId}@v${resolved.profileVersion}/cred${resolved.credentialVersion}`
      : `local:${resolved.model}`;
    return { caller, configRef };
  }

  private async localModelLabel(): Promise<string> {
    try {
      const resolved = await resolveLocalModel({
        settings: this.settings,
        secrets: this.secrets,
        cloud: this.cloudSource(),
      });
      return `${resolved.label} · ${resolved.model}`;
    } catch (err) {
      return err instanceof Error ? err.message : String(err);
    }
  }

  private async testLocalModel(): Promise<string> {
    const resolved = await resolveLocalModel({
      settings: this.settings,
      secrets: this.secrets,
      cloud: this.cloudSource(),
    });
    return testLocalConnection(this.transport, resolved);
  }

  private async loadCloudProfiles() {
    const client = this.getClient();
    if (!client) throw new Error("尚未登录，无法读取线上配置。");
    // 只取一次列表，再按同一顺序查询各自的本机绑定状态（避免两次调用顺序错位）
    const profiles = await client.listProfiles();
    const statuses = await Promise.all(
      profiles.map((p) => client.localBindingStatus(p.id).catch(() => null)));
    return profiles.map((p, i) => ({
      id: p.id,
      kind: p.kind,
      model: p.model,
      endpoint: p.endpoint,
      version: p.version,
      configured: p.configured,
      credentialVersion: p.credential_version,
      boundLocally: statuses[i]?.bound ?? false,
    }));
  }

  /** 用户明确点击「配置到本设备」时领取一次 Key（docs/08 §8.3）。 */
  private async bindLocalKey(profileId: string): Promise<string> {
    const client = this.getClient();
    if (!client) throw new Error("尚未登录。");
    const binding = await client.bindLocalKey(profileId);
    const ref = bindingSecretRef(profileId);
    const persisted = await this.secrets.setSecretStrict(ref, binding.secret);
    const data = ((await this.loadData()) ?? {}) as PluginData;
    data.importedSecretRefs = [...new Set([...(data.importedSecretRefs ?? []), ref])];
    await this.saveData(data);
    if (!persisted) {
      // 只在会话内可用：不固定版本，避免重启后 pinned 值在而密钥丢失
      return `已配置到本设备，但无法写入本机秘密存储：Key 仅本次会话可用，重启后需重新绑定。`;
    }
    // 绑定成功即固定该配置版本，供后续批次复用（Key 不写进任务文件）
    this.settings.localModel = pinConfig(this.settings.localModel, {
      mode: this.settings.localModel.mode,
      label: binding.model,
      baseUrl: binding.endpoint,
      model: binding.model,
      capabilities: binding.capabilities ?? {},
      apiKey: "",
      profileVersion: binding.profile_version,
      credentialVersion: binding.credential_version,
      cloudProfileId: binding.profile_id,
    });
    await this.saveSettings();
    return `已把 ${binding.model} 的 Key 配置到本设备（v${binding.profile_version}）。`;
  }

  /** 解绑：只删本机绑定与秘密副本，不替用户撤销线上或供应商 Key。 */
  private async unbindLocalKey(profileId: string): Promise<string> {
    const client = this.getClient();
    if (!client) throw new Error("尚未登录。");
    const res = await client.unbindLocalKey(profileId);
    await this.secrets.clearSecretStrict(bindingSecretRef(profileId));
    const data = ((await this.loadData()) ?? {}) as PluginData;
    data.importedSecretRefs = (data.importedSecretRefs ?? []).filter((r) => r !== bindingSecretRef(profileId));
    await this.saveData(data);
    return res.note || "已解绑本机；线上或供应商 Key 未撤销。";
  }

  // ---- 整理知识库（docs/08 §8.1） ----

  private activeDigestPath(): string | null {
    const file = this.app.workspace.getActiveFile();
    if (!(file instanceof TFile) || file.extension !== "md") return null;
    const prefix = `${this.settings.digestsFolder.replace(/\/+$/, "")}/`;
    return file.path.startsWith(prefix) ? file.path : null;
  }

  private async organizeCurrentDigest(path: string): Promise<void> {
    if (!this.settings.localOrganizeEnabled) {
      new Notice("尚未启用本地整理；请在设置中打开「启用本地整理」。");
      return;
    }
    const itemId = /--([A-Za-z0-9_-]+)\.md$/.exec(path)?.[1];
    if (!itemId) {
      new Notice("无法从文件名解析 item_id。");
      return;
    }
    await this.organize.enqueue(itemId, path);
    new Notice("已加入整理队列；打开「整理知识库」面板开始处理。");
    await this.openOrganizePanel();
  }

  private async openProposalIndex(): Promise<void> {
    const path = `${this.settings.inboxFolder}/知识更新候选.md`;
    const file = this.app.vault.getAbstractFileByPath(path);
    if (file instanceof TFile) {
      await this.app.workspace.getLeaf(false).openFile(file);
      return;
    }
    new Notice("暂无候选索引；请先运行「整理待处理内容」。");
  }

  /** 重建 00 Inbox/知识更新候选.md（普通 Markdown，无需 Dataview）。 */
  async refreshProposalIndex(): Promise<void> {
    try {
      const md = await this.organize.renderProposalIndexMd();
      const path = `${this.settings.inboxFolder}/知识更新候选.md`;
      const fs = new VaultFs(this.app);
      const current = (await fs.exists(path)) ? await fs.read(path) : null;
      if (current !== md) await fs.write(path, md);
    } catch (err) {
      console.log(`[knowledge-inbox] 候选索引更新失败：${err instanceof Error ? err.message : String(err)}`);
    }
  }

  async openOrganizePanel(start = false): Promise<void> {
    const { workspace } = this.app;
    let leaf = workspace.getLeavesOfType(VIEW_TYPE_ORGANIZE)[0] ?? null;
    if (!leaf) {
      const newLeaf = workspace.getRightLeaf(false);
      if (newLeaf) {
        await newLeaf.setViewState({ type: VIEW_TYPE_ORGANIZE, active: true });
        leaf = newLeaf;
      }
    }
    if (leaf) workspace.revealLeaf(leaf);
    if (start) new Notice("点面板中的「开始整理」运行未整理条目。");
  }

  private openSettings(): void {
    const setting = (this.app as unknown as {
      setting?: { open: () => void; openTabById: (id: string) => void };
    }).setting;
    if (setting) {
      setting.open();
      setting.openTabById(this.manifest.id);
    }
  }

  // ---- 账号与同步 ----

  async manualSync(): Promise<void> {
    if (!(await this.refreshToken())) {
      new Notice("Knowledge Inbox：尚未登录或 Token 缺失，请先在设置中登录账号。");
      return;
    }
    if (this.lastStatus?.epochConflict) this.engine.resetEpochConflict();
    await this.engine.runOnce("manual");
    const st = this.lastStatus;
    if (st?.lastError) {
      new Notice(`Knowledge Inbox 同步出错：${st.lastError}`);
    } else if ((st?.pendingCount ?? 0) === 0) {
      new Notice("Knowledge Inbox：已同步，暂无待入库条目。");
    } else {
      new Notice(`Knowledge Inbox：同步完成，还有 ${st?.pendingCount} 条待重试。`);
    }
  }

  private async loginWithBrowser(): Promise<void> {
    if (!this.settings.serverUrl) {
      new Notice("请先填写服务器地址。");
      return;
    }
    try {
      // 设备授权时申请专用绑定权限（docs/08 §8.3）：不随 profiles:manage 自动获得
      const start = await KbClient.deviceStart(
        this.settings.serverUrl, this.settings.deviceName, ["profiles:bind-local"]);
      new Notice("Knowledge Inbox：已在浏览器打开授权页，请在网页上确认登录本设备。");
      window.open(start.browser_url, "_blank");

      // 按服务端间隔轮询，直到批准/过期（docs/05 §4.5 第 4-6 条）
      const deadline = Date.now() + 6 * 60_000;
      const intervalMs = Math.max(2, start.interval_seconds || 2) * 1000;
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, intervalMs));
        const poll = await KbClient.devicePoll(
          this.settings.serverUrl, start.request_id, start.poll_secret);
        if (poll.status !== "ok") continue;
        await this.secrets.setToken(this.settings.tokenRef, poll.token!);
        this.tokenCache = poll.token!;
        this.settings.deviceId = poll.device_id!;
        this.settings.userId = poll.user_id!;
        if (!this.settings.organizeDeviceId) this.settings.organizeDeviceId = poll.device_id!;
        await this.saveSettings();
        new Notice(`Knowledge Inbox：登录成功，设备 ${poll.device_id!.slice(0, 8)}…`);
        await this.engine.runOnce("logged-in");
        return;
      }
      new Notice("Knowledge Inbox：授权超时或已取消，请重新点击「登录账号」。");
    } catch (err) {
      new Notice(`登录失败：${err instanceof Error ? err.message : String(err)}`);
    }
  }

  private async disconnectDevice(): Promise<void> {
    if (!this.settings.deviceId) {
      new Notice("尚未登录。");
      return;
    }
    const client = this.getClient();
    try {
      if (client) await client.disconnectDevice();
    } catch (err) {
      // 服务端可能已撤销：本地照常清理
      console.log(`[knowledge-inbox] disconnect: ${err instanceof Error ? err.message : String(err)}`);
    }
    await this.secrets.clearToken(this.settings.tokenRef);
    // 清理该账号导入的线上 Key；本地独立配置保持独立（docs/08 §8.3）
    const data = ((await this.loadData()) ?? {}) as PluginData;
    await this.secrets.clearImportedCloudSecrets(data.importedSecretRefs ?? []);
    data.importedSecretRefs = [];
    await this.saveData(data);
    this.tokenCache = null;
    this.settings.deviceId = "";
    this.settings.userId = "";
    await this.saveSettings();
    new Notice("Knowledge Inbox：设备已断开，已导入的笔记保留。");
  }

  private async restoreSuppressed(): Promise<void> {
    const fs = new VaultFs(this.app);
    const s = this.settings;
    const suppression = new Suppression(fs, `${s.systemFolder}/KnowledgeInbox/suppression.json`);
    const removed = await suppression.unsuppressAll();
    if (removed.length === 0) {
      new Notice("当前没有被抑制的条目。");
      return;
    }
    // 同步删除这些条目的本地 commit：否则“commit 在而笔记不在”会让下一次事件
    // 立即再次抑制，恢复命令永远无法触发重建（真机验收遗留 #7）。
    const commits = new CommitStore(fs, `${s.systemFolder}/KnowledgeInbox/commits`);
    let cleared = 0;
    for (const itemId of removed) cleared += await commits.removeForItem(itemId);
    await this.engine.rebuildIndex();
    new Notice(
      `已恢复 ${removed.length} 条（清理 ${cleared} 个本地提交标记）；`
      + "下次该条目有新版本时会重新建立笔记。",
    );
  }

  private async openStatusView(): Promise<void> {
    const { workspace } = this.app;
    let leaf = workspace.getLeavesOfType(VIEW_TYPE_STATUS)[0] ?? null;
    if (!leaf) {
      const newLeaf = workspace.getRightLeaf(false);
      if (newLeaf) {
        await newLeaf.setViewState({ type: VIEW_TYPE_STATUS, active: true });
        leaf = newLeaf;
      }
    }
    if (leaf) workspace.revealLeaf(leaf);
  }

  private updateStatusBar(): void {
    if (!this.statusBarEl) return;
    const st = this.lastStatus;
    if (st?.running) {
      this.statusBarEl.setText("Knowledge Inbox：同步中…");
    } else if (st?.epochConflict) {
      this.statusBarEl.setText("Knowledge Inbox：设备已过期");
    } else if (st?.lastError) {
      this.statusBarEl.setText("Knowledge Inbox：出错");
    } else if ((st?.pendingCount ?? 0) > 0) {
      this.statusBarEl.setText(`Knowledge Inbox：${st?.pendingCount} 待入库`);
    } else {
      this.statusBarEl.setText("Knowledge Inbox：就绪");
    }
  }

  private async loadSettings(): Promise<void> {
    const data = ((await this.loadData()) ?? {}) as PluginData;
    this.settings = { ...DEFAULT_SETTINGS, ...data };
    // 旧数据没有 localModel：补齐默认值，避免深层字段缺失。
    // 每次都构造新对象，防止运行时修改污染模块级 DEFAULT_LOCAL_MODEL。
    this.settings.localModel = {
      ...defaultLocalModel(),
      ...(data.localModel ?? {}),
      local: { ...defaultLocalModel().local, ...(data.localModel?.local ?? {}) },
    };
    this.syncState = data.syncState ?? { cursor: 0, pending: {}, lastRunAt: null };
  }

  private async saveSettings(): Promise<void> {
    const data = ((await this.loadData()) ?? {}) as PluginData;
    Object.assign(data, this.settings);
    await this.saveData(data);
  }
}

export default KbPlugin;
