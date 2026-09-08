/** 插件入口：生命周期、命令、状态栏与轮询（docs/02 §13.3）。 */

import { ItemView, Notice, Plugin, WorkspaceLeaf } from "obsidian";
import { KbClient } from "./api";
import { KbSettingTab, SecretBridge, DEFAULT_SETTINGS } from "./settings";
import { SyncEngine, type SyncState } from "./sync/engine";
import { VaultFs } from "./vault/vaultfs";
import { CommitStore, Suppression } from "./vault/records";
import type { EngineStatus, KbSettings } from "./types";

const VIEW_TYPE_STATUS = "knowledge-inbox-status";
const POLL_INTERVAL_MS = 60_000;
const MAX_BACKOFF_MS = 10 * 60_000;

interface PluginData extends Partial<KbSettings> {
  syncState?: SyncState;
  tokenFallback?: string;
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
    const lines: Array<[string, string]> = [
      ["状态", st?.running ? "同步中…" : (st?.lastError ? `出错：${st.lastError}` : "就绪")],
      ["待入库", String(st?.pendingCount ?? 0)],
      ["上次同步", st?.lastRunAt ? new Date(st.lastRunAt).toLocaleString() : "—"],
      ["游标", String(this.plugin.syncState?.cursor ?? 0)],
    ];
    if (st?.epochConflict) {
      lines.push(["设备", "已不是主要写入设备；请在服务器切换后重新同步"]);
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
    btn.style.minWidth = "120px";
    btn.style.minHeight = "44px";
    btn.addEventListener("click", () => void this.plugin.manualSync());
  }

  async onClose(): Promise<void> {
    this.contentEl.empty();
  }
}

export class KbPlugin extends Plugin {
  settings: KbSettings = DEFAULT_SETTINGS;
  secrets!: SecretBridge;
  engine!: SyncEngine;
  syncState: SyncState = { cursor: 0, pending: {}, lastRunAt: null };
  lastStatus: EngineStatus | null = null;

  private timer: number | null = null;
  private backoffMs = POLL_INTERVAL_MS;
  private statusBarEl: HTMLElement | null = null;
  private tokenCache: string | null = null;

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
    });

    this.statusBarEl = this.addStatusBarItem();
    this.addRibbonIcon("inbox", "Knowledge Inbox 状态", () => void this.openStatusView());

    this.addSettingTab(new KbSettingTab(
      this.app, this, this.settings,
      () => this.saveSettings(),
      (code, deviceName) => this.pair(code, deviceName),
    ));

    this.addCommand({ id: "sync-now", name: "立即同步", callback: () => void this.manualSync() });
    this.addCommand({ id: "show-status", name: "显示同步状态", callback: () => void this.openStatusView() });
    this.addCommand({
      id: "restore-suppressed",
      name: "恢复被删除条目的自动重建",
      callback: () => void this.restoreSuppressed(),
    });

    this.registerView(VIEW_TYPE_STATUS, (leaf) => new StatusView(leaf, this));

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
    return new KbClient(this.settings.serverUrl, this.tokenCache);
  }

  private async refreshToken(): Promise<boolean> {
    this.tokenCache = await this.secrets.getToken(this.settings.tokenRef);
    return this.tokenCache !== null;
  }

  async manualSync(): Promise<void> {
    if (!(await this.refreshToken())) {
      new Notice("Knowledge Inbox：尚未配对或 Token 缺失，请先在设置中配对。");
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

  private async pair(code: string, deviceName: string): Promise<void> {
    if (!this.settings.serverUrl) {
      new Notice("请先填写服务器地址。");
      return;
    }
    try {
      const result = await KbClient.pair(this.settings.serverUrl, code, deviceName);
      await this.secrets.setToken(this.settings.tokenRef, result.token);
      this.tokenCache = result.token;
      this.settings.deviceId = result.device_id;
      this.settings.userId = result.user_id;
      await this.saveSettings();
      new Notice(`配对成功：设备 ${result.device_id.slice(0, 8)}…`);
      await this.engine.runOnce("paired");
    } catch (err) {
      new Notice(`配对失败：${err instanceof Error ? err.message : String(err)}`);
    }
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
    this.syncState = data.syncState ?? { cursor: 0, pending: {}, lastRunAt: null };
  }

  private async saveSettings(): Promise<void> {
    const data = ((await this.loadData()) ?? {}) as PluginData;
    Object.assign(data, this.settings);
    await this.saveData(data);
  }
}

export default KbPlugin;
