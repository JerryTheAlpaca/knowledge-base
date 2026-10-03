/**
 * ContentDocument v3 的解析与渲染（docs/24 §1–§5）。
 *
 * 边界（docs/24 §3）：模型只写内容主体
 * `{title, summary, sections[], limitations[]}`，文档身份、引用表、
 * 哈希与完整性由程序填写。渲染器服务于笔记正文：
 * Markdown 是可阅读产物，机器回读只看 content.json。
 *
 * 本插件只消费云端已组装的文档，不再本地调模型组装。
 *
 * 纯逻辑模块：不依赖 obsidian，可独立测试。
 */

import {
  CONTENT_FORMAT_VERSION,
  type CompletenessV3,
  type ContentBlockKindV3,
  type ContentBlockV3,
  type ContentDocumentV3,
  type ContentLocatorV3,
  type ContentRefV3,
  type ContentSectionV3,
} from "../types";

const BLOCK_KINDS: ContentBlockKindV3[] = ["claim", "quote", "suggestion", "text"];

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

/** 界面显示的自然位置文案；绝不显示 R/e/s 编号（docs/24 §2）。 */
export function locatorLabel(locator: ContentLocatorV3 | null | undefined): string {
  if (!locator || typeof locator !== "object") return "";
  if (locator.kind === "time") {
    const from = formatMs(locator.start_ms);
    const to = formatMs(locator.end_ms);
    if (from && to) return `${from}–${to}`;
    return from || to;
  }
  if (locator.kind === "paragraph") {
    // 段落编号只用于算出「第几段」：p0007 → 第 7 段，绝不把 p0007 本身显示给用户
    const m = /^p0*(\d{1,5})$/.exec(String(locator.paragraph_id ?? ""));
    return m ? `第 ${m[1]} 段` : "所在段落";
  }
  if (locator.kind === "line") return `第 ${String(locator.line_no ?? "?")} 行`;
  return "";
}

function formatMs(ms: number | null | undefined): string {
  if (typeof ms !== "number" || !Number.isFinite(ms) || ms < 0) return "";
  const total = Math.round(ms / 1000);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

// ---- 解析已组装的 v3 文档（Bundle 里的 content.json） ----

export interface ParseResult {
  document: ContentDocumentV3 | null;
  /** 给用户看的中文说明；非空表示该文档不可消费。 */
  errors: string[];
}

/** 严格读取一个 v3 文档：格式版本不符或结构缺失时如实拒绝，不做静默降级。 */
export function parseContentDocument(raw: unknown): ParseResult {
  const errors: string[] = [];
  if (!isRecord(raw)) return { document: null, errors: ["content.json 顶层必须是对象"] };
  const version = str(raw.format_version);
  if (version !== CONTENT_FORMAT_VERSION) {
    return { document: null, errors: [`内容格式版本 ${version || "（缺失）"} 不受支持，本插件只读取 "${CONTENT_FORMAT_VERSION}"`] };
  }
  if (!str(raw.document_id)) errors.push("缺少 document_id");
  if (!str(raw.kind)) errors.push("缺少 kind");
  if (typeof raw.revision !== "number") errors.push("revision 必须是整数");
  const sections = Array.isArray(raw.sections) ? raw.sections : null;
  if (!sections) errors.push("sections 必须是数组");
  const references = isRecord(raw.references) ? raw.references : null;
  if (!references) errors.push("references 必须是对象");
  const completeness = isRecord(raw.completeness) ? raw.completeness : null;
  if (!completeness) errors.push("缺少 completeness");
  if (errors.length) return { document: null, errors };

  const refs: Record<string, ContentRefV3> = {};
  for (const [key, value] of Object.entries(references!)) {
    if (!isRecord(value)) { errors.push(`引用表 ${key} 不是对象`); continue; }
    const ids = Array.isArray(value.segment_ids) ? value.segment_ids.map(String) : [];
    if (!str(value.item_id) || typeof value.source_revision !== "number" || !ids.length) {
      errors.push(`引用表 ${key} 缺少 item_id/source_revision/segment_ids`);
      continue;
    }
    refs[key] = {
      item_id: String(value.item_id),
      source_revision: Number(value.source_revision),
      segment_ids: ids,
      source_text_hash: str(value.source_text_hash),
      locator: isRecord(value.locator) ? (value.locator as ContentLocatorV3) : null,
    };
  }
  const parsedSections: ContentSectionV3[] = [];
  (sections as unknown[]).forEach((item, si) => {
    if (!isRecord(item)) { errors.push(`sections[${si}] 不是对象`); return; }
    const blocks: ContentBlockV3[] = [];
    const rawBlocks = Array.isArray(item.blocks) ? item.blocks : [];
    rawBlocks.forEach((b, bi) => {
      if (!isRecord(b)) { errors.push(`sections[${si}].blocks[${bi}] 不是对象`); return; }
      const kind = str(b.kind) as ContentBlockKindV3;
      if (!BLOCK_KINDS.includes(kind)) { errors.push(`sections[${si}].blocks[${bi}] kind 未知：${str(b.kind) || "（空）"}`); return; }
      const declaredRefs = Array.isArray(b.refs) ? b.refs.map(String) : [];
      for (const key of declaredRefs) {
        if (!refs[key]) errors.push(`sections[${si}].blocks[${bi}] 引用了不存在的 ${key}`);
      }
      blocks.push({ kind, text: str(b.text), refs: declaredRefs.filter((k) => Boolean(refs[k])) });
    });
    parsedSections.push({ heading: str(item.heading), blocks });
  });
  if (errors.length) return { document: null, errors };

  const prov = isRecord(raw.provenance) ? raw.provenance : {};
  const state = str(completeness!.state);
  return {
    document: {
      format_version: CONTENT_FORMAT_VERSION,
      document_id: str(raw.document_id),
      kind: str(raw.kind),
      revision: Number(raw.revision),
      created_at: str(raw.created_at),
      title: str(raw.title),
      summary: str(raw.summary),
      sections: parsedSections,
      references: refs,
      limitations: (Array.isArray(raw.limitations) ? raw.limitations : []).map(String),
      completeness: {
        state: state === "complete" || state === "partial" ? state : "partial",
        missing_stages: (Array.isArray(completeness!.missing_stages) ? completeness!.missing_stages : []).map(String),
        gaps: (Array.isArray(completeness!.gaps) ? completeness!.gaps : []).filter(isRecord).map((g) => ({
          code: str(g.code),
          message: str(g.message),
          refs: Array.isArray(g.refs) ? g.refs.map(String) : undefined,
          segment_ids: Array.isArray(g.segment_ids) ? g.segment_ids.map(String) : undefined,
          block: Array.isArray(g.block) ? g.block.map(Number) : undefined,
        })),
        dropped_blocks: Number(completeness!.dropped_blocks ?? 0) || 0,
        repair_calls: Number(completeness!.repair_calls ?? 0) || 0,
      },
      provenance: {
        recipe_version: str(prov.recipe_version),
        task: str(prov.task),
        input_documents: (Array.isArray(prov.input_documents) ? prov.input_documents : []).filter(isRecord).map((d) => ({
          document_id: str(d.document_id), kind: str(d.kind), revision: Number(d.revision ?? 0) || 0,
        })),
        source_revisions: (Array.isArray(prov.source_revisions) ? prov.source_revisions : []).filter(isRecord).map((d) => ({
          item_id: str(d.item_id), source_revision: Number(d.source_revision ?? 0) || 0,
        })),
      },
    },
    errors: [],
  };
}

// ---- 渲染：笔记正文与管理区共用同一文档 ----

export interface RenderOptions {
  /** 一条引用 → 用户可点开的固定原文锚点链接；无本地快照时返回 null。 */
  sourceLinkOf?: (ref: ContentRefV3) => string | null;
  /** 一条引用 → 来源标题（引用表展示用）。 */
  sourceTitleOf?: (ref: ContentRefV3) => string | null;
  /** 隐藏完整度提示（Knowledge 管理区里已经单独展示时可用）。 */
  hideCompleteness?: boolean;
  /** 隐藏「依据」内联链接（正文只放文字时使用）。 */
  hideCitations?: boolean;
}

function evidenceSuffix(doc: ContentDocumentV3, block: ContentBlockV3, opts: RenderOptions): string {
  if (opts.hideCitations || !opts.sourceLinkOf || !block.refs.length) return "";
  const seen = new Set<string>();
  const links: string[] = [];
  for (const key of block.refs) {
    const ref = doc.references[key];
    if (!ref) continue;
    const loc = locatorLabel(ref.locator);
    const link = opts.sourceLinkOf(ref);
    if (!link) continue;
    const label = loc ? `${loc} 查看原文` : "查看原文";
    const tag = `${link}#${loc}`;
    if (seen.has(tag)) continue;
    seen.add(tag);
    links.push(`[[${link}|${label}]]`);
  }
  return links.length ? ` （依据：${links.join("、")}）` : "";
}

/**
 * 渲染内容主体为可阅读 Markdown（与 preview.md 同源，docs/24 §5）。
 *
 * 四种角色的展示区别：claim 纯文字＋查看原文、quote 引用样式、
 * suggestion 明确标为 AI 建议/待验证、text 导语。
 */
export function renderContentMarkdown(doc: ContentDocumentV3, opts: RenderOptions = {}): string {
  const lines: string[] = [];
  if (doc.summary) lines.push(doc.summary.trim(), "");
  for (const section of doc.sections) {
    if (section.heading) lines.push(`## ${section.heading}`, "");
    for (const block of section.blocks) {
      const text = block.text.trim();
      if (!text) continue;
      if (block.kind === "quote") {
        lines.push(`> ${text.replace(/\n/g, "\n> ")}${evidenceSuffix(doc, block, opts)}`, "");
      } else if (block.kind === "suggestion") {
        lines.push(`> [!question] AI 建议（待验证，未经原文证明）`, `> ${text.replace(/\n/g, "\n> ")}`, "");
      } else if (block.kind === "text") {
        lines.push(text, "");
      } else {
        lines.push(`- ${text}${evidenceSuffix(doc, block, opts)}`);
      }
    }
    if (section.blocks.some((b) => b.kind === "claim")) lines.push("");
  }
  if (doc.limitations.length) {
    lines.push("## 限制与未解决的问题", "");
    for (const l of doc.limitations) lines.push(`- ${l}`);
    lines.push("");
  }
  if (!opts.hideCompleteness) lines.push(...renderCompletenessNotice(doc.completeness));
  return lines.join("\n").replace(/\n{3,}/g, "\n\n").trim();
}

/** 部分结果必须看得出来，不能冒充完整成功（docs/24 §4；docs/23 §5.3）。 */
export function renderCompletenessNotice(c: CompletenessV3): string[] {
  if (c.state === "complete") return [];
  const lines = ["", `> [!warning] 部分结果：本次加工未全部成功（已丢弃 ${c.dropped_blocks} 处内容）。`];
  for (const g of c.gaps.slice(0, 5)) {
    if (g.message) lines.push(`> - ${g.message}`);
  }
  if (!c.gaps.length) lines.push("> - 服务端未提供缺口说明。");
  lines.push("> ", "> 以下内容仍可用；缺口修复请重新整理该来源。");
  return lines;
}
