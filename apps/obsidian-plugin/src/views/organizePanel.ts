/**
 * 整理知识库面板（docs/08 §8.1；docs/23 §6）。
 *
 * 布局与文档一致：
 * ```
 * 整理知识库
 * 本地整理模型：当前配置名称 / 模型名       [修改]
 * 范围：当前 Digest / 未整理与已过期条目 / 手动选择
 * [开始整理]  [暂停]
 *
 * 待处理 → 正在整理 → 待采纳 / 留在 Digest / 失败待重试
 * 主题候选：目标主题、变化摘要、前后差异、原文依据、冲突
 * [整篇采纳]  [按块选择采纳]  [跳过]  [回滚]
 * ```
 *
 * 后台准备不等于自动写入第三层；模型请求串行运行，暂停后不发新请求。
 * 部分采纳是真的按块勾选后由程序重组正文与引用；不提供把自由文本编辑回写成候选的
 * “逐段合并”，也不假装它能可靠保留结构。
 */

import { ItemView, Modal, Notice, Setting, TFile, WorkspaceLeaf } from "obsidian";
import type { OrganizeService } from "../knowledge/organize";
import { itemIdOfDigestNote, renderDiff } from "../knowledge/organize";
import { renderContentMarkdown } from "../vault/content";
import type { ContentBlockKindV3, LegacyProposal, OrganizeTask, TopicProposal } from "../types";

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
  /** 读当前笔记正文以取 frontmatter 的 item_id（不靠文件名）。 */
  readNote: (path: string) => Promise<string>;
}

const KIND_LABEL: Record<ContentBlockKindV3, string> = {
  claim: "来源主张",
  quote: "原文摘录",
  suggestion: "AI 建议（待验证）",
  text: "导语",
};

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
    c.createEl("h4", { text: `待采纳主题候选（${pending.length}）` });
    c.createEl("p", {
      text: "部分采纳是真的按块勾选，由程序重组正文并清除不再使用的引用，摘录重新做逐字校验；"
        + "不提供把自由文本编辑回写成候选的逐段合并。",
    }).addClass("kb-muted");
    if (!pending.length) {
      c.createEl("p", { text: "暂无待采纳候选。" }).addClass("kb-muted");
    }
    for (const p of pending) this.renderProposal(c, p);

    const applied = proposals.filter((p) => p.state === "applied").slice(0, 5);
    if (applied.length) {
      c.createEl("h4", { text: "最近已应用（可回滚）" });
      for (const p of applied) this.renderApplied(c, p);
    }

    const legacy = await service.listLegacyProposals();
    if (legacy.length) {
      c.createEl("h4", { text: `旧版逐观点候选（${legacy.length}，已归档只读）` });
      c.createEl("p", {
        text: "旧候选不强行转换成主题修改规则：这里仅供查看，需要新的修改请基于当前材料重新生成。",
      }).addClass("kb-muted");
      for (const item of legacy.slice(0, 10)) this.renderLegacy(c, item);
    }
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

  private renderProposal(host: HTMLElement, p: TopicProposal): void {
    const box = host.createEl("div");
    box.addClass("kb-list-row", "kb-list-row-roomy");
    box.createEl("strong", { text: p.knowledge_title ?? "（新建主题）" });
    if (p.no_op) box.createEl("span", { text: " · 判定无需修改" });
    box.createEl("div", { text: `变化：${p.change_summary || "—"}` });
    if (p.target_reason) box.createEl("div", { text: `选择该主题的原因：${p.target_reason}` }).addClass("kb-muted");
    const doc = p.candidate_document;
    if (doc) {
      const blocks = doc.sections.reduce((n, s) => n + s.blocks.length, 0);
      box.createEl("div", { text: `候选内容 ${doc.sections.length} 节 / ${blocks} 块，依据 ${Object.keys(doc.references).length} 条原文` });
      if (doc.completeness.state !== "complete") {
        box.createEl("div", { text: `注意：候选为部分结果（丢弃 ${doc.completeness.dropped_blocks} 块）` });
      }
    }
    if (p.conflicts.length) {
      const ul = box.createEl("ul");
      for (const conflict of p.conflicts) {
        ul.createEl("li", { text: `${conflict.topic}：${conflict.description}` });
      }
    }

    const actions = box.createEl("div");
    actions.addClass("kb-actions");

    if (doc) {
      const accept = actions.createEl("button", { text: "整篇采纳" });
      accept.addClass("kb-btn", "mod-cta");
      accept.addEventListener("click", () => void this.accept(p));

      const partial = actions.createEl("button", { text: "按块选择采纳" });
      partial.addClass("kb-btn");
      partial.addEventListener("click", () => {
        new BlockSelectionModal(this.app, p, (keep) => void this.accept(p, keep)).open();
      });
    }

    const diff = actions.createEl("button", { text: "查看差异" });
    diff.addClass("kb-btn");
    diff.addEventListener("click", () => {
      new CandidateDiffModal(this.app, p).open();
    });

    const skip = actions.createEl("button", { text: "跳过" });
    skip.addClass("kb-btn");
    skip.addEventListener("click", () => void this.skip(p));
  }

  private renderApplied(host: HTMLElement, p: TopicProposal): void {
    const row = host.createEl("div");
    row.addClass("kb-list-row");
    row.createEl("div", { text: `${p.knowledge_title ?? "（新建主题）"} · rev ${p.applied_knowledge_revision ?? "?"}` });
    const btn = row.createEl("button", { text: "回滚到上一版" });
    btn.addClass("kb-btn");
    btn.addEventListener("click", () => void this.rollback(p));
  }

  private renderLegacy(host: HTMLElement, item: LegacyProposal): void {
    const row = host.createEl("div");
    row.addClass("kb-list-row");
    row.createEl("div", {
      text: `${item.knowledge_title ?? item.knowledge_id ?? "（旧候选）"} · ${item.state} · ${item.change_summary || "—"}`,
    });
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
    // 身份在 frontmatter：文件名已是可读标题，不再从名字里解析 item_id
    const itemId = itemIdOfDigestNote(await this.hooks.readNote(path));
    if (!itemId) {
      new Notice("当前笔记缺少 kb_item_id，无法加入整理队列。");
      return;
    }
    const task = await this.hooks.service().enqueue(itemId, path);
    new Notice(task ? "已加入整理队列。" : "该 Digest 没有可整理的 v3 内容文档。");
    await this.render();
  }

  private async retry(task: OrganizeTask): Promise<void> {
    await this.hooks.service().retry(task.task_id);
    new Notice("已重新排入队列；下次「开始整理」会处理该条目。");
    await this.render();
  }

  private async accept(p: TopicProposal, keep?: boolean[][]): Promise<void> {
    try {
      const res = await this.hooks.service().accept(p.proposal_id, { keep });
      new Notice(res.note, res.applied ? 6000 : 8000);
      await this.hooks.refreshProposalIndex();
    } catch (err) {
      new Notice(`采纳失败：${err instanceof Error ? err.message : String(err)}`, 8000);
    }
    await this.render();
  }

  private async skip(p: TopicProposal): Promise<void> {
    await this.hooks.service().skip(p.proposal_id);
    await this.hooks.refreshProposalIndex();
    await this.render();
  }

  private async rollback(p: TopicProposal): Promise<void> {
    const res = await this.hooks.service().rollback(p.proposal_id);
    new Notice(res.note + (res.diff ? `
${res.diff.slice(0, 200)}` : ""), 8000);
    await this.render();
  }
}

/** 差异预览：程序计算的正文新增／改动／删除（模型漏掉旧内容时删除会显式出现）。 */
class CandidateDiffModal extends Modal {
  constructor(app: ConstructorParameters<typeof Modal>[0], private proposal: TopicProposal) {
    super(app);
  }

  onOpen(): void {
    const { contentEl } = this;
    contentEl.createEl("h3", { text: `主题修改候选：${this.proposal.knowledge_title ?? "新建主题"}` });
    contentEl.createEl("p", { text: this.proposal.change_summary || "—" });
    const after = this.proposal.candidate_document
      ? renderContentMarkdown(this.proposal.candidate_document, { hideCitations: true })
      : "";
    const diff = renderDiff(this.proposal.baseline_body, after);
    const pre = contentEl.createEl("pre");
    pre.setText(diff || "（无内容变化）");
    pre.addClass("kb-diff-pre");
    if (this.proposal.conflicts.length) {
      contentEl.createEl("h4", { text: "冲突与未解决的问题" });
      for (const c of this.proposal.conflicts) {
        contentEl.createEl("p", { text: `${c.topic}：${c.description}` });
      }
    }
  }

  onClose(): void {
    this.contentEl.empty();
  }
}

/** 按块选择采纳：勾选后由程序重组正文与引用，并重新校验逐字摘录。 */
class BlockSelectionModal extends Modal {
  private keep: boolean[][];

  constructor(app: ConstructorParameters<typeof Modal>[0], private proposal: TopicProposal,
              private onSubmit: (keep: boolean[][]) => void) {
    super(app);
    const doc = proposal.candidate_document!;
    this.keep = doc.sections.map((s) => s.blocks.map(() => true));
  }

  onOpen(): void {
    const { contentEl } = this;
    const doc = this.proposal.candidate_document!;
    contentEl.createEl("h3", { text: `选择要采纳的内容块：${this.proposal.knowledge_title ?? "新建主题"}` });
    contentEl.createEl("p", {
      text: "取消勾选的块不会写入主题；未再使用的原文依据会被自动清除，含逐字摘录的块会重新校验。",
    }).addClass("kb-muted");
    doc.sections.forEach((section, si) => {
      contentEl.createEl("h4", { text: section.heading || "（无标题小节）" });
      section.blocks.forEach((block, bi) => {
        const row = contentEl.createEl("label");
        row.addClass("kb-list-row");
        const box = row.createEl("input", { attr: { type: "checkbox" } });
        box.checked = true;
        box.addEventListener("change", () => { this.keep[si][bi] = box.checked; });
        row.createEl("span", { text: ` ${KIND_LABEL[block.kind]}｜${block.text.slice(0, 160)}` });
      });
    });
    new Setting(contentEl)
      .addButton((b) => b.setButtonText("按勾选采纳").setCta().onClick(() => {
        this.close();
        this.onSubmit(this.keep);
      }))
      .addButton((b) => b.setButtonText("取消").onClick(() => this.close()));
  }

  onClose(): void {
    this.contentEl.empty();
  }
}
