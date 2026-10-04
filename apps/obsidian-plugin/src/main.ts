/**
 * 插件入口：生命周期、命令、状态栏与轮询
 * （docs/02 §13.3；docs/24 §8）。
 *
 * 两层：01 Sources（原始证据）／02 Digests（云端提炼）。
 * 本插件只做同步——拉事件、校验下载、写两篇笔记、发回执。
 * 第三层主题整理（本地模型直连、主题候选、03 Knowledge 写入）已移除。
 */

import { ItemView, Notice, Plugin, WorkspaceLeaf } from "obsidian";
import { KbClient } from "./api";
import { KbSettingTab, SecretBridge, DEFAULT_SETTINGS } from "./settings";
import { SyncEngine, type SyncState } from "./sync/engine";
import { VaultFs } from "./vault/vaultfs";
import { CommitStore, Suppression } from "./vault/records";
import type { EngineStatus, KbSettings } from "./types";
import { runRenameMigration } from "./vault/rename";
import { DocumentIndex } from "./vault/documents";
import { documentsIndexPath } from "./vault/paths";

const VIEW_TYPE_STATUS = "golden-rose-inbox-status";
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
  getDisplayText(): string { return "Golden-Rose-Inbox 状态"; }
  getIcon(): string { return "inbox"; }

  async onOpen(): Promise<void> {
    this.render();
    this.registerInterval(window.setInterval(() => this.render(), 5_000));
  }

  private render(): void {
    const c = this.contentEl;
    c.empty();
    c.createEl("h3", { text: "Golden-Rose-Inbox" });
    const st = this.plugin.lastStatus;
    // lastStatus（onStatus 推送）优先，尚未跑过同步时回退到 data.json 里的
    // 持久化 syncState，避免重启后面板一直显示占位值
    const sync = this.plugin.syncState;
    const lines: Array<[string, string]> = this.plugin.settings.deviceId
      ? [
        ["状态", st?.running ? "同步中…" : (st?.lastError ? `出错：${st.lastError}` : "就绪")],
        ["待入库", String(st?.pendingCount ?? Object.keys(sync.pending).length)],
        ["上次同步", st?.lastRunAt
          ? new Date(st.lastRunAt).toLocaleString()
          : sync.lastRunAt ? new Date(sync.lastRunAt).toLocaleString() : "—"],
        ["游标", String(st?.cursor ?? sync.cursor)],
      ]
      : [
        ["状态", "未登录：请在设置中点「登录账号」，用统一账号在网页上批准本设备"],
        ["待入库", "—"],
        ["上次同步", "—"],
        ["游标", "—"],
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
    if ((st?.pausedForUpgrade ?? 0) > 0) {
      lines.push(["暂停导入",
        `${st?.pausedForUpgrade} 条内容格式不受支持：请升级插件后同步（未落盘空白笔记、未发成功回执）`]);
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
  settings: KbSettings = { ...DEFAULT_SETTINGS };
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
      console.error("[golden-rose-inbox] onload threw:", err);
      new Notice("Golden-Rose-Inbox 加载错误：" + detail, 0);
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
      log: (msg) => console.log(`[golden-rose-inbox] ${msg}`),
    });

    this.statusBarEl = this.addStatusBarItem();
    this.addRibbonIcon("inbox", "Golden-Rose-Inbox 状态", () => void this.openStatusView());

    const tab = new KbSettingTab(this.app, this, this.settings, {
      onSave: () => this.saveSettings(),
      onLogin: () => this.loginWithBrowser(),
      onDisconnect: () => this.disconnectDevice(),
      onRenameDevice: (name) => this.renameDevice(name),
      loadCloudProfiles: () => this.loadCloudProfiles(),
    });
    this.addSettingTab(tab);

    this.addCommand({ id: "sync-now", name: "立即同步", callback: () => void this.manualSync() });
    this.addCommand({ id: "show-status", name: "显示同步状态", callback: () => void this.openStatusView() });
    this.addCommand({
      id: "restore-suppressed",
      name: "恢复被删除条目的自动重建",
      callback: () => void this.restoreSuppressed(),
    });
    this.addCommand({
      id: "migrate-note-names",
      name: "迁移笔记文件名为可读名称",
      callback: () => void this.migrateNoteNames(),
    });
    this.addCommand({
      id: "rebuild-document-index",
      name: "重建文档索引（ID→路径）",
      callback: () => void this.rebuildDocumentIndex(),
    });

    this.registerView(VIEW_TYPE_STATUS, (leaf) => new StatusView(leaf, this));

    // onLayoutReady 后再开始恢复与拉取，不阻塞编辑器启动（docs/02 §13.3）
    this.app.workspace.onLayoutReady(() => {
      void (async () => {
        // 先刷新 Token 再补发回执：recoverReceipts 依赖 getClient()，
        // tokenCache 未加载时它拿不到客户端、启动补发会静默跳过
        if (this.settings.autoSync && (await this.refreshToken())) {
          const n = await this.engine.recoverReceipts();
          if (n > 0) new Notice(`Golden-Rose-Inbox：补发了 ${n} 条回执`);
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
    return new KbClient(this.settings.serverUrl, this.tokenCache, this.settings.deviceId);
  }

  private async refreshToken(): Promise<boolean> {
    this.tokenCache = await this.secrets.getToken(this.settings.tokenRef);
    return this.tokenCache !== null;
  }

  private async loadCloudProfiles() {
    const client = this.getClient();
    if (!client) throw new Error("尚未登录，无法读取线上配置。");
    return (await client.listProfiles()).map((p) => ({
      id: p.id,
      kind: p.kind,
      model: p.model,
      endpoint: p.endpoint,
      version: p.version,
      configured: p.configured,
    }));
  }

  // ---- 本地迁移（docs/23 §8.3） ----

  /** 经 Obsidian 的重命名能力改名；不可用时退回 adapter.rename 并保证目录存在。 */
  private async renameNote(from: string, to: string): Promise<void> {
    const fs = new VaultFs(this.app);
    const abstract = this.app.vault.getAbstractFileByPath(from);
    const fileManager = this.app.fileManager as unknown as {
      renameFile?: (f: unknown, path: string) => Promise<void>;
    };
    if (abstract && typeof fileManager?.renameFile === "function") {
      await fileManager.renameFile(abstract, to);
      return;
    }
    await fs.rename(from, to);
  }

  private async migrateNoteNames(): Promise<void> {
    const fs = new VaultFs(this.app);
    const result = await runRenameMigration(fs, this.settings, {
      rename: (from, to) => this.renameNote(from, to),
    });
    if (result.completed && result.renamed.length) {
      this.settings.layoutVersion = 3;
      await this.saveSettings();
    }
    new Notice(
      result.failed.length
        ? `文件名迁移未完成：成功 ${result.renamed.length}，失败 ${result.failed.length}，`
          + `保持原样 ${result.blocked.length}；恢复记录 ${result.record_path}`
        : `文件名迁移完成：改名 ${result.renamed.length} 篇，保持原样 ${result.blocked.length} 篇；`
          + `恢复记录 ${result.record_path}`,
      10000,
    );
    await this.engine.rebuildIndex();
  }

  private async rebuildDocumentIndex(): Promise<void> {
    const fs = new VaultFs(this.app);
    const docs = new DocumentIndex(fs, documentsIndexPath(this.settings.systemFolder));
    const doc = await docs.rebuild([
      { folder: this.settings.sourcesFolder, kind: "source" as const, skipDirs: ["_assets"] },
      { folder: this.settings.digestsFolder, kind: "digest" as const },
    ]);
    const conflicts = doc.conflicts.length
      ? `；身份冲突 ${doc.conflicts.length} 个（两份都保留，请人工确认）`
      : "";
    new Notice(`文档索引已重建：${Object.keys(doc.docs).length} 篇${conflicts}`);
  }

  // ---- 账号与同步 ----

  async manualSync(): Promise<void> {
    if (!(await this.refreshToken())) {
      new Notice("Golden-Rose-Inbox：尚未登录或 Token 缺失，请先在设置中登录账号。");
      return;
    }
    if (this.lastStatus?.epochConflict) this.engine.resetEpochConflict();
    await this.engine.runOnce("manual");
    const st = this.lastStatus;
    if (st?.lastError) {
      new Notice(`Golden-Rose-Inbox 同步出错：${st.lastError}`);
    } else if ((st?.pendingCount ?? 0) === 0) {
      new Notice("Golden-Rose-Inbox：已同步，暂无待入库条目。");
    } else {
      new Notice(`Golden-Rose-Inbox：同步完成，还有 ${st?.pendingCount} 条待重试。`);
    }
  }

  private async loginWithBrowser(): Promise<void> {
    if (!this.settings.serverUrl) {
      new Notice("请先填写服务器地址。");
      return;
    }
    try {
      const start = await KbClient.deviceStart(this.settings.serverUrl, this.settings.deviceName);
      new Notice("Golden-Rose-Inbox：已在浏览器打开授权页，请在网页上确认登录本设备。");
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
        await this.saveSettings();
        new Notice(`Golden-Rose-Inbox：登录成功，设备 ${poll.device_id!.slice(0, 8)}…`);
        this.updateStatusBar();
        await this.engine.runOnce("logged-in");
        return;
      }
      new Notice("Golden-Rose-Inbox：授权超时或已取消，请重新点击「登录账号」。");
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
      console.log(`[golden-rose-inbox] disconnect: ${err instanceof Error ? err.message : String(err)}`);
    }
    await this.secrets.clearToken(this.settings.tokenRef);
    this.tokenCache = null;
    this.settings.deviceId = "";
    this.settings.userId = "";
    await this.saveSettings();
    new Notice("Golden-Rose-Inbox：设备已断开，已导入的笔记保留。");
    this.updateStatusBar();
  }

  /** 设备名已存本地，再同步到服务端，使网页端设备列表与本机一致。 */
  private async renameDevice(name: string): Promise<void> {
    if (!this.settings.deviceId) return; // 未登录：名称已存本地，下次登录时生效
    const client = this.getClient();
    if (!client) {
      new Notice("设备名已保存在本机；登录后才会同步到网页端。");
      return;
    }
    try {
      await client.renameDevice(name);
      new Notice(`Golden-Rose-Inbox：设备名已同步为「${name}」。`);
    } catch (err) {
      // 本地已存：服务端失败不丢用户输入，如实告知未同步
      console.log(`[golden-rose-inbox] renameDevice: ${err instanceof Error ? err.message : String(err)}`);
      new Notice("设备名已保存在本机，但同步到服务器失败；恢复网络后可重新修改。");
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
    // 立即再次抑制，恢复命令永远无法触发重建
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
    // 未登录时不报「就绪」：没在同步却显示就绪，会让人以为一切正常
    if (!this.settings.deviceId) {
      this.statusBarEl.setText("Golden-Rose-Inbox：未登录");
    } else if (st?.running) {
      this.statusBarEl.setText("Golden-Rose-Inbox：同步中…");
    } else if (st?.epochConflict) {
      this.statusBarEl.setText("Golden-Rose-Inbox：设备已过期");
    } else if (st?.lastError) {
      this.statusBarEl.setText("Golden-Rose-Inbox：出错");
    } else if ((st?.pendingCount ?? 0) > 0) {
      this.statusBarEl.setText(`Golden-Rose-Inbox：${st?.pendingCount} 待入库`);
    } else {
      this.statusBarEl.setText("Golden-Rose-Inbox：就绪");
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
