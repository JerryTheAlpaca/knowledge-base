/**
 * 整理知识库面板（docs/08 §8.1）。
 *
 * 布局与文档一致：
 * ```
 * 整理知识库
 * 本地整理模型：当前配置名称 / 模型名       [修改]
 * 范围：当前 Digest / 未整理与已过期条目 / 手动选择
 * [开始整理]  [暂停]
 *
 * 待处理 → 正在整理 → 待采纳 / 留在 Digest / 失败待重试
 * 候选：目标主题、认知增量、前后差异、原文依据
 * [采纳]  [编辑后采纳]  [跳过]
 * ```
 *
 * 后台准备不等于自动写入第三层；初期模型请求串行运行，暂停后不发新请求。
 */

import { ItemView, Modal, Notice, Setting, WorkspaceLeaf } from "obsidian";
import type { OrganizeService } from "../knowledge/organize";
import { renderDiff } from "../knowledge/organize";
import type { FusionProposal, OrganizeTask } from "../types";

export const VIEW_TYPE_ORGANIZE = "knowledge-inbox-organize";

export interface OrganizePanelHooks {
  service: () => OrganizeService;
  /** 当前本地整理模型描述（配置名 / 模型名）；未配置时给出原因。 */
  modelLabel: () => Promise<string>;
  onOpenSettings: () => void;
  /** 重建 00 Inbox/知识更新候选.md。 */
  refreshProposalIndex: () => Promise<void>;
  /** 当前 Digest 路径（编辑器聚焦的笔记，可能为 null）。 */
  activeDigestPath: () => string | null;
  /** 手动选择范围：弹出候选列表。 */
  pickDigests: () => Promise<string[]>;
}

export class OrganizePanelView extends ItemView {
  constructor(leaf: WorkspaceLeaf, private hooks: OrganizePanelHooks) {
    super(leaf);
    this.navigation = false;
  }

  getViewType(): string { return VIEW_TYPE_ORGANIZE; }
  getDisplayText(): string { return "整理知识库"; }
  getIcon(): string { return "sparkles"; }

  async onOpen(): Promise<void> {
    this.render();
    this.registerInterval(window.setInterval(() => void this.render(), 5_000));
  }

  async onClose(): Promise<void> {
    this.contentEl.empty();
  }

  async render(): Promise<void> {
    const c = this.contentEl;
    c.empty();
    c.createEl("h3", { text: "整理知识库" });

    // 模型配置
    let label: string;
    try {
      label = await this.hooks.modelLabel();
    } catch (err) {
      label = `不可用：${err instanceof Error ? err.message : String(err)}`;
    }
    const modelRow = c.createEl("div");
    modelRow.createEl("strong", { text: "本地整理模型：" });
    modelRow.createEl("span", { text: label });
    const editBtn = c.createEl("button", { text: "修改" });
    editBtn.addClass("kb-btn");
    editBtn.addEventListener("click", () => this.hooks.onOpenSettings());

    c.createEl("p", {
      text: "插件直接调用所选模型服务；必要的 Digest／Knowledge 内容会发送给模型供应商，本系统云端不接收。",
    }).addClass("kb-muted");

    // 范围与操作
    const service = this.hooks.service();
    const scopeRow = c.createEl("div");
    scopeRow.createEl("strong", { text: "范围：" });
    scopeRow.createEl("span", {
      text: service.isPaused ? "已暂停" : "未整理与已过期条目",
    });

    const actions = c.createEl("div");
    actions.addClass("kb-actions");

    const startBtn = actions.createEl("button", { text: "开始整理" });
    startBtn.addClass("kb-btn", "kb-btn-wide");
    startBtn.addEventListener("click", () => void this.run(startBtn));

    const pauseBtn = actions.createEl("button", {
      text: service.isPaused ? "继续" : "暂停",
    });
    pauseBtn.addClass("kb-btn", "kb-btn-mid");
    pauseBtn.addEventListener("click", () => {
      if (service.isPaused) service.resume();
      else service.pause();
      void this.render();
    });

    const currentBtn = actions.createEl("button", { text: "整理当前 Digest" });
    currentBtn.addClass("kb-btn");
    currentBtn.addEventListener("click", () => void this.runCurrentDigest());

    // 任务分组
    const tasks = await service.listPending();
    c.createEl("h4", { text: "待处理 / 正在整理 / 失败待重试" });
    if (!tasks.length) {
      c.createEl("p", { text: "没有待整理的条目。" }).addClass("kb-muted");
    }
    for (const task of tasks) this.renderTask(c, task);

    // 候选
    const proposals = await service.listProposals();
    const pending = proposals.filter((p) => p.state === "ready");
    c.createEl("h4", { text: `待采纳候选（${pending.length}）` });
    if (!pending.length) {
      c.createEl("p", { text: "暂无待采纳候选。" }).addClass("kb-muted");
    }
    for (const p of pending) this.renderProposal(c, p);
  }

  private renderTask(host: HTMLElement, task: OrganizeTask): void {
    const row = host.createEl("div");
    row.addClass("kb-list-row");
    row.createEl("div", {
      text: `${task.item_id} · ${task.state}${task.last_error ? `：${task.last_error}` : ""}`,
    });
    if (task.state === "unknown_outcome") {
      const btn = row.createEl("button", { text: "显式重试" });
      btn.addClass("kb-btn");
      btn.addEventListener("click", () => void this.retry(task));
    }
  }

  private renderProposal(host: HTMLElement, p: FusionProposal): void {
    const box = host.createEl("div");
    box.addClass("kb-list-row", "kb-list-row-roomy");
    box.createEl("strong", { text: p.knowledge_title ?? "（新建主题）" });
    if (p.no_op) box.createEl("span", { text: " · 无变化" });
    box.createEl("div", { text: `变化：${p.change_summary || "—"}` });

    const increments = p.promotion_decisions.filter((d) => d.decision === "review");
    if (increments.length) {
      const ul = box.createEl("ul");
      for (const d of increments) {
        ul.createEl("li", {
          text: `${d.claim_id}（${d.dimensions.increment.level}）：${d.reason}`,
        });
      }
    }
    if (p.conflicts.length) {
      box.createEl("div", { text: `冲突 ${p.conflicts.length} 项（保留各自依据，不自动平均）` });
    }
    if (p.retired_claims.length) {
      box.createEl("div", { text: `退休观点 ${p.retired_claims.length} 条（含取代说明）` });
    }

    const actions = box.createEl("div");
    actions.addClass("kb-actions");

    const accept = actions.createEl("button", { text: "采纳" });
    accept.addClass("kb-btn", "mod-cta");
    accept.addEventListener("click", () => void this.accept(p, null));

    const edit = actions.createEl("button", { text: "编辑后采纳" });
    edit.addClass("kb-btn");
    edit.addEventListener("click", () => {
      new EditProposalModal(this.app, p, (body) => void this.accept(p, body)).open();
    });

    const diff = actions.createEl("button", { text: "查看差异" });
    diff.addClass("kb-btn");
    diff.addEventListener("click", () => {
      new DiffModal(this.app, p).open();
    });

    const skip = actions.createEl("button", { text: "跳过" });
    skip.addClass("kb-btn");
    skip.addEventListener("click", () => void this.skip(p));
  }

  private async run(btn: HTMLButtonElement): Promise<void> {
    btn.disabled = true;
    btn.setText("整理中…");
    try {
      const result = await this.hooks.service().runBatch();
      await this.hooks.refreshProposalIndex();
      const summary = [
        `已生成候选 ${result.prepared}`,
        `留在 Digest ${result.keptDigest}`,
        `跳过 ${result.skipped}`,
        `失败 ${result.failed}`,
      ].join("，");
      new Notice(`整理完成：${summary}`);
      for (const msg of result.messages.slice(0, 5)) console.log(`[organize] ${msg}`);
    } catch (err) {
      new Notice(`整理失败：${err instanceof Error ? err.message : String(err)}`, 8000);
    } finally {
      btn.disabled = false;
      btn.setText("开始整理");
      await this.render();
    }
  }

  private async runCurrentDigest(): Promise<void> {
    const path = this.hooks.activeDigestPath();
    if (!path) {
      new Notice("当前笔记不是 02 Digests 下的 Digest，请先打开一篇 Digest。");
      return;
    }
    const itemId = /--([A-Za-z0-9_-]+)\.md$/.exec(path)?.[1];
    if (!itemId) {
      new Notice("无法从文件名解析 item_id。");
      return;
    }
    await this.hooks.service().enqueue(itemId, path);
    new Notice("已加入整理队列。");
    await this.render();
  }

  private async retry(task: OrganizeTask): Promise<void> {
    await this.hooks.service().retry(task.task_id);
    new Notice("已重新排入队列；下次「开始整理」会处理该条目。");
    await this.render();
  }

  private async accept(p: FusionProposal, editedBody: string | null): Promise<void> {
    try {
      const res = await this.hooks.service().accept(p.proposal_id, editedBody ?? undefined);
      new Notice(res.note, res.applied ? 6000 : 8000);
      await this.hooks.refreshProposalIndex();
    } catch (err) {
      new Notice(`采纳失败：${err instanceof Error ? err.message : String(err)}`, 8000);
    }
    await this.render();
  }

  private async skip(p: FusionProposal): Promise<void> {
    await this.hooks.service().skip(p.proposal_id);
    await this.hooks.refreshProposalIndex();
    await this.render();
  }
}

/** 「编辑后采纳」：允许在写入前修改替换稿。 */
class EditProposalModal extends Modal {
  private value: string;
  constructor(app: ConstructorParameters<typeof Modal>[0], private proposal: FusionProposal,
              private onSubmit: (body: string) => void) {
    super(app);
    this.value = proposal.proposed_managed_body;
  }

  onOpen(): void {
    const { contentEl } = this;
    contentEl.createEl("h3", { text: `编辑后采纳：${this.proposal.knowledge_title ?? "新建主题"}` });
    const ta = contentEl.createEl("textarea");
    ta.value = this.value;
    ta.addClass("kb-edit-body");
    ta.addEventListener("input", () => { this.value = ta.value; });
    new Setting(contentEl)
      .addButton((b) => b.setButtonText("确认采纳").setCta().onClick(() => {
        this.close();
        this.onSubmit(this.value);
      }))
      .addButton((b) => b.setButtonText("取消").onClick(() => this.close()));
  }

  onClose(): void {
    this.contentEl.empty();
  }
}

/** 差异预览：新增／删改了什么（docs/08 §6.2、§7.2）。 */
class DiffModal extends Modal {
  constructor(app: ConstructorParameters<typeof Modal>[0], private proposal: FusionProposal) {
    super(app);
  }

  onOpen(): void {
    const { contentEl } = this;
    contentEl.createEl("h3", { text: `差异：${this.proposal.knowledge_title ?? "新建主题"}` });
    contentEl.createEl("p", { text: this.proposal.change_summary || "—" });
    const diff = renderDiff("", this.proposal.proposed_managed_body);
    const pre = contentEl.createEl("pre");
    pre.setText(diff || "（无内容）");
    pre.addClass("kb-diff-pre");
    if (this.proposal.conflicts.length) {
      contentEl.createEl("h4", { text: "冲突与未解决的问题" });
      contentEl.createEl("pre", { text: JSON.stringify(this.proposal.conflicts, null, 2) });
    }
  }

  onClose(): void {
    this.contentEl.empty();
  }
}
