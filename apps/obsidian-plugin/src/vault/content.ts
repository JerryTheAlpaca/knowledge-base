/**
 * ContentDocument v3 的解析、程序组装与渲染（docs/24 §1–§5；docs/23 §3、§4.3）。
 *
 * 边界（docs/24 §3）：模型只写内容主体 `{title, summary, sections[], limitations[]}`
 * 并选择任务内的 `R` 引用；文档身份、`e` 引用表、哈希、完整性与 provenance 全部由
 * 程序填写。渲染器同时服务预览（preview.md 同构）与笔记正文：Markdown 是可阅读产物，
 * 机器回读只看 content.json，绝不从固定中文标题反解字段。
 *
 * 纯逻辑模块：不依赖 obsidian，可独立测试。
 */

import { sha256Hex } from "./template";
import {
  CONTENT_FORMAT_VERSION,
  type CompletenessGapV3,
  type CompletenessV3,
  type ContentBlockKindV3,
  type ContentBlockV3,
  type ContentDocumentV3,
  type ContentLocatorV3,
  type ContentRefV3,
  type ContentSectionV3,
} from "../types";

/** docs/24 §1 硬性边界。 */
export const LIMITS = {
  title: 200,
  summary: 500,
  sections: 40,
  blocks: 200,
  blockText: 2000,
  heading: 200,
  limitations: 10,
  limitation: 1000,
  references: 60,
  bytes: 512 * 1024,
} as const;

const BLOCK_KINDS: ContentBlockKindV3[] = ["claim", "quote", "suggestion", "text"];

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

/** 逐字摘录校验用的规范化：去掉全部空白，不改文字（docs/24 §2）。 */
export function normalizeForQuote(text: string): string {
  return text.replace(/\s+/g, "");
}

/** 引用范围原文：按 segment 顺序以 `\n` 连接；哈希对同一算法计算（docs/24 §2）。 */
export function refTextOf(segmentTexts: string[]): string {
  return segmentTexts.join("\n");
}

export async function refTextHashOf(segmentTexts: string[]): Promise<string> {
  return sha256Hex(refTextOf(segmentTexts));
}

/** 程序分配的任务内引用表：`R1`…`Rn`，按材料顺序，多来源互不覆盖（docs/23 §4.1）。 */
export function buildRefTable(
  sources: Array<{ item_id: string; source_revision: number; segment_ids: string[]; source_text_hash: string; locator?: ContentLocatorV3 | null }>,
): Record<string, ContentRefV3> {
  const table: Record<string, ContentRefV3> = {};
  sources.forEach((s, i) => {
    table[`R${i + 1}`] = {
      item_id: s.item_id,
      source_revision: s.source_revision,
      segment_ids: [...s.segment_ids],
      source_text_hash: s.source_text_hash,
      locator: s.locator ?? null,
    };
  });
  return table;
}

export function refTableKeys(table: Record<string, ContentRefV3>): string[] {
  return Object.keys(table);
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

// ---- 程序组装（本地主题融合用；与服务端同一职责） ----

export interface AssembleOptions {
  documentId: string;
  kind: string;
  revision: number;
  task: string;
  recipeVersion: string;
  /** 本次任务内引用表：模型只能从中选择 `R` 键。 */
  refTable: Record<string, ContentRefV3>;
  /** 允许写入 provenance 的输入文档与实际来源修订。 */
  inputDocuments?: Array<{ document_id: string; kind: string; revision: number }>;
  sourceRevisions?: Array<{ item_id: string; source_revision: number }>;
  createdAt?: string;
  /** 已知的失败块编号（`chunk:<n>`），用于 missing_stages。 */
  missingStages?: string[];
  repairCalls?: number;
  /** 调用方已知的事实性缺口（如某个分块加工失败）：只补说明，不由本函数推断。 */
  extraGaps?: CompletenessGapV3[];
  /** 固定原文快照读取：quote 逐字校验与哈希复核都只认真实文本，缺失即不通过。 */
  segmentTexts: (itemId: string, sourceRevision: number, segmentId: string) => string | null;
}

export interface AssembleReport {
  document: ContentDocumentV3 | null;
  /** docs/24 §3 固定分类码。 */
  errors: string[];
  completeness: CompletenessV3;
  dropped_blocks: number;
}

/**
 * 组装 v3 文档：类型与体积检查 → 引用去空白精确去重、只接受给定 R → 查表得真实范围
 * → quote 在相邻原文内逐字匹配 → 生成 e 键并改写 refs → 写身份与来源版本（docs/24 §3）。
 *
 * 模型多返回的 format_version/document_id/references 等一律忽略（只读主体字段）。
 */
/** 引用身份：同一来源版本的同一段范围在文档里只对应一个 `e` 键（docs/24 §2）。 */
function refIdentity(ref: ContentRefV3): string {
  return `${ref.item_id}@${ref.source_revision}|${ref.segment_ids.join(",")}`;
}

/** 区分「输出被截断」与「根本不是 JSON」：两者的修复提示不同（docs/24 §3）。 */
function classifyParseFailure(raw: string): "truncated" | "json_unparsable" {
  const tail = raw.trim();
  if (tail.startsWith("{") && !tail.endsWith("}")) return "truncated";
  return "json_unparsable";
}

/** `missing_stages`：调用方给的分块事实在前，再按固定阶段顺序补本函数发现的（docs/24 §4）。 */
function finalizeMissingStages(
  stageCodes: Set<string>,
  extra: string[],
): string[] {
  const order = ["summary", "quote_verification", "evidence_resolution", "legacy_evidence_unresolved"];
  const chunks = extra.filter((s) => s.startsWith("chunk:"))
    .sort((a, b) => Number(a.replace(/\D/g, "")) - Number(b.replace(/\D/g, "")));
  const others = extra.filter((s) => !s.startsWith("chunk:") && !stageCodes.has(s));
  return [...chunks, ...order.filter((s) => stageCodes.has(s)), ...others];
}

/**
 * 组装 v3 文档：类型与体积检查 → 引用去空白精确去重、只接受给定 R → 查表得真实范围
 * → quote 在相邻原文内逐字匹配 → 生成 e 键并改写 refs → 写身份与来源版本（docs/24 §3）。
 *
 * 模型多返回的 format_version/document_id/references 等一律忽略（只读主体字段）。
 * 两遍处理：先判定哪些块可发布，再按**保留下来的块**分配 `e` 键，被挡下的块不得留下
 * 引用；被挡块带走的依据若不再出现在文档里，摘要由程序清空（docs/24 §1、§4）。
 */
export async function assembleContentDocument(
  subject: unknown,
  opts: AssembleOptions,
): Promise<AssembleReport> {
  const errors: string[] = [];
  const gaps: CompletenessGapV3[] = [];
  const stageCodes = new Set<string>();
  let dropped = 0;
  const missingStages = [...(opts.missingStages ?? [])];
  /** 被挡下的块仍然引用到的依据：用于判断摘要是否可能在描述已消失的原文。 */
  const held: ContentRefV3[] = [];
  const note = (code: string, message: string, extra?: Partial<CompletenessGapV3>, stage?: string): void => {
    errors.push(code);
    gaps.push({ code, message, ...extra });
    if (stage) stageCodes.add(stage);
  };

  let modelOutput: unknown = subject;
  if (typeof subject === "string") {
    try {
      modelOutput = JSON.parse(subject);
    } catch {
      const code = classifyParseFailure(subject);
      return failedReport(code, "模型响应无法解析为 JSON 对象", opts, missingStages, gaps, dropped);
    }
  }
  if (!isRecord(modelOutput)) {
    return failedReport("json_unparsable", "模型输出不是 JSON 对象", opts, missingStages, gaps, dropped);
  }
  if (!Array.isArray(modelOutput.sections) || modelOutput.sections.length === 0) {
    return failedReport("missing_subject", "模型未返回 sections 主体", opts, missingStages, gaps, dropped);
  }
  if (modelOutput.truncated === true) errors.push("truncated");

  const rawSummary = str(modelOutput.summary).trim();
  let summary = rawSummary;
  if (rawSummary.length > LIMITS.summary) {
    summary = "";
    note("limit_exceeded", "摘要超出长度限制，已清空");
  }

  const refTextOfKey = (key: string): string => {
    const ref = opts.refTable[key];
    return refTextOf(ref.segment_ids.map((sid) =>
      opts.segmentTexts(ref.item_id, ref.source_revision, sid) ?? ""));
  };

  interface KeptBlock { kind: ContentBlockKindV3; text: string; tokens: string[]; entries: ContentRefV3[] }
  const prepared: Array<{ heading: string; blocks: KeptBlock[] }> = [];
  let blockCount = 0;

  const rawSections = (modelOutput.sections as unknown[]).slice(0, LIMITS.sections);
  if ((modelOutput.sections as unknown[]).length > LIMITS.sections) {
    dropped += (modelOutput.sections as unknown[]).length - LIMITS.sections;
    note("limit_exceeded", `章节数超过 ${LIMITS.sections}，后面的章节未采纳`, { block: [LIMITS.sections, 0] });
  }

  rawSections.forEach((rawSection, si) => {
    if (!isRecord(rawSection)) {
      dropped += 1;
      note("missing_subject", `第 ${si + 1} 个章节不是对象，未采纳`, { block: [si, 0] });
      return;
    }
    const heading = str(rawSection.heading).trim().slice(0, LIMITS.heading);
    const rawBlocks = Array.isArray(rawSection.blocks) ? rawSection.blocks : [];
    const blocks: KeptBlock[] = [];
    rawBlocks.forEach((rawBlock, bi) => {
      const at = [si, bi];
      if (!isRecord(rawBlock) || blockCount >= LIMITS.blocks) {
        dropped += 1;
        note("limit_exceeded", `内容块总数达到上限 ${LIMITS.blocks}，后续块未采纳`, { block: at });
        return;
      }
      const kind = str(rawBlock.kind) as ContentBlockKindV3;
      if (!BLOCK_KINDS.includes(kind)) {
        dropped += 1;
        note("bad_ref", `第 ${si + 1} 节第 ${bi + 1} 块缺少有效角色，未采纳`, { block: at });
        return;
      }
      const text = str(rawBlock.text).trim();
      if (!text) { dropped += 1; return; }
      if (text.length > LIMITS.blockText) {
        dropped += 1;
        note("limit_exceeded", `一处内容超过 ${LIMITS.blockText} 字，未采纳`, { block: at });
        return;
      }
      if (text.includes("[[")) {
        dropped += 1;
        note("missing_subject", "一处内容包含 Obsidian 内部链接，未采纳", { block: at });
        return;
      }
      const declared: string[] = [];
      for (const raw of Array.isArray(rawBlock.refs) ? rawBlock.refs : []) {
        const token = str(raw).trim();
        if (token && !declared.includes(token)) declared.push(token);
      }
      if ((kind === "claim" || kind === "quote") && !declared.length) {
        dropped += 1;
        note("bad_ref", kind === "claim"
          ? "一处来源主张没有任何依据，不作为有依据结论发布"
          : "一处摘录没有引用原文范围", { block: at }, "evidence_resolution");
        return;
      }
      const unknown = declared.filter((token) => !opts.refTable[token]);
      if (unknown.length) {
        // 混有有效与无效引用：整块暂不发布，也不留下“少了一份依据”的假象
        dropped += 1;
        held.push(...declared.filter((token) => opts.refTable[token]).map((token) => opts.refTable[token]));
        note("bad_ref", `一处内容引用了本次任务不存在的引用号：${unknown.join("、")}`, { block: at, refs: unknown }, "evidence_resolution");
        return;
      }
      const entries = declared.map((token) => opts.refTable[token]);
      if (kind === "quote") {
        const joined = normalizeForQuote(declared.map(refTextOfKey).join("\n"));
        if (!joined || !joined.includes(normalizeForQuote(text))) {
          dropped += 1;
          held.push(...entries);
          note("quote_not_verbatim", "一处摘录未能在所引原文中逐字核实，未采纳", {
            block: at, refs: declared,
            segment_ids: [...new Set(entries.flatMap((e) => e.segment_ids))].sort(),
          }, "quote_verification");
          return;
        }
      }
      blockCount += 1;
      blocks.push({ kind, text, tokens: declared, entries });
    });
    if (blocks.length || heading) prepared.push({ heading, blocks });
  });

  /** 只剩空架子：没有带依据的观点或摘录就没有可发布的内容，摘要不算证据（docs/24 §4）。 */
  const hasEvidence = (sections: Array<{ blocks: Array<{ kind: string }> }>): boolean =>
    sections.some((s) => s.blocks.some((b) => b.kind === "claim" || b.kind === "quote"));
  if (!hasEvidence(prepared)) {
    return failedReport("empty_document", "模型输出只剩空架子：没有任何带依据的观点或摘录",
      opts, missingStages, gaps, dropped);
  }

  const byIdentity = new Map<string, string>();
  const orderedRefs: ContentRefV3[] = [];
  const usedIdentities = new Set<string>();
  const sections: ContentSectionV3[] = [];
  prepared.forEach(({ heading, blocks }) => {
    const out: ContentBlockV3[] = [];
    for (const block of blocks) {
      const refs: string[] = [];
      let overflow = false;
      for (const [i, entry] of block.entries.entries()) {
        const ident = refIdentity(entry);
        let key = byIdentity.get(ident);
        if (!key) {
          if (orderedRefs.length >= LIMITS.references) { overflow = true; break; }
          key = `e${orderedRefs.length + 1}`;
          byIdentity.set(ident, key);
          orderedRefs.push(entry);
        }
        refs.push(key);
        usedIdentities.add(ident);
        void i;
      }
      if (overflow) {
        dropped += 1;
        held.push(...block.entries);
        note("limit_exceeded", `文档引用数已达 ${LIMITS.references} 条上限，该块未采纳`,
          { block: undefined, refs: block.tokens });
        continue;
      }
      out.push({ kind: block.kind, text: block.text, refs });
    }
    if (out.length || heading) sections.push({ heading, blocks: out });
  });
  if (!hasEvidence(sections)) {
    return failedReport("empty_document", "有效内容在引用解析后全部落空", opts, missingStages, gaps, dropped);
  }

  const references: Record<string, ContentRefV3> = {};
  orderedRefs.forEach((ref, i) => { references[`e${i + 1}`] = ref; });

  // 摘要只在“被丢的块带走了文档里不再出现的依据”时清空：程序不猜摘要说了什么，
  // 但那块原文范围已从文档消失（docs/23 §5.3）。
  if (summary && held.some((ref) => !usedIdentities.has(refIdentity(ref)))) {
    summary = "";
    stageCodes.add("summary");
  }

  // 缺口里的引用尽量写成文档内 `e` 键，读者与界面才能据此跳转；解析不出留原词。
  for (const gap of gaps) {
    if (!gap.refs?.length) continue;
    gap.refs = gap.refs.map((token) => {
      const ref = opts.refTable[token];
      return ref ? (byIdentity.get(refIdentity(ref)) ?? token) : token;
    });
  }
  for (const gap of opts.extraGaps ?? []) {
    if (gap && typeof gap.code === "string" && gap.code) {
      gaps.push({ ...gap, message: gap.message || "部分内容缺失" });
    }
  }

  const limitations = (Array.isArray(modelOutput.limitations) ? modelOutput.limitations : [])
    .map((x) => str(x).trim())
    .filter(Boolean)
    .slice(0, LIMITS.limitations)
    .map((x) => (x.length > LIMITS.limitation ? `${x.slice(0, LIMITS.limitation)}…` : x));

  const sourceRevisions = opts.sourceRevisions ?? deriveSourceRevisions(opts, orderedRefs);
  const state: CompletenessV3["state"] =
    dropped > 0 || gaps.length > 0 || missingStages.length > 0 || stageCodes.size > 0 ? "partial" : "complete";
  const completeness: CompletenessV3 = {
    state,
    missing_stages: finalizeMissingStages(stageCodes, missingStages),
    gaps,
    dropped_blocks: dropped,
    repair_calls: opts.repairCalls ?? 0,
  };
  const document: ContentDocumentV3 = {
    format_version: CONTENT_FORMAT_VERSION,
    document_id: opts.documentId,
    kind: opts.kind,
    revision: opts.revision,
    created_at: opts.createdAt ?? new Date().toISOString(),
    title: str(modelOutput.title).trim().slice(0, LIMITS.title),
    summary,
    sections,
    references,
    limitations,
    completeness,
    provenance: {
      recipe_version: opts.recipeVersion,
      task: opts.task,
      input_documents: opts.inputDocuments ?? [],
      source_revisions: sourceRevisions,
    },
  };
  const bytes = new TextEncoder().encode(JSON.stringify(document)).length;
  if (bytes > LIMITS.bytes) {
    return failedReport("limit_exceeded", "文档序列化后超过 512 KiB 上限", opts, missingStages, gaps, dropped);
  }
  return { document, errors, completeness, dropped_blocks: dropped };
}

/** provenance 的来源修订：按引用表与文档身份如实登记，不采信模型自报（docs/24 §1）。 */
function deriveSourceRevisions(
  opts: AssembleOptions,
  usedRefs: ContentRefV3[],
): Array<{ item_id: string; source_revision: number }> {
  const out: Array<{ item_id: string; source_revision: number }> = [];
  const seen = new Set<string>();
  const add = (itemId: string, revision: number): void => {
    if (!itemId || !Number.isInteger(revision) || revision <= 0) return;
    const key = `${itemId}@${revision}`;
    if (seen.has(key)) return;
    seen.add(key);
    out.push({ item_id: itemId, source_revision: revision });
  };
  for (const ref of Object.values(opts.refTable)) add(ref.item_id, ref.source_revision);
  for (const ref of usedRefs) add(ref.item_id, ref.source_revision);
  return out;
}

function failedReport(
  code: string,
  message: string,
  opts: AssembleOptions,
  missingStages: string[],
  gaps: CompletenessGapV3[] = [],
  dropped = 0,
): AssembleReport {
  return {
    document: null,
    errors: [code],
    completeness: {
      state: "failed",
      missing_stages: missingStages,
      gaps: [{ code, message }, ...gaps],
      dropped_blocks: dropped,
      repair_calls: opts.repairCalls ?? 0,
    },
    dropped_blocks: dropped,
  };
}

/** 从本地固定原文快照构建引用表：哈希与写入/下载/迁移同一算法（docs/24 §2）。 */
export async function buildRefTableFromSegments(
  sources: Array<{ item_id: string; source_revision: number; segment_ids: string[]; locator?: ContentLocatorV3 | null }>,
  segmentTexts: (itemId: string, sourceRevision: number, segmentId: string) => string | null,
): Promise<Record<string, ContentRefV3>> {
  const entries: Array<{ item_id: string; source_revision: number; segment_ids: string[]; source_text_hash: string; locator?: ContentLocatorV3 | null }> = [];
  for (const s of sources) {
    const texts = s.segment_ids.map((sid) => segmentTexts(s.item_id, s.source_revision, sid) ?? "");
    entries.push({
      item_id: s.item_id,
      source_revision: s.source_revision,
      segment_ids: [...s.segment_ids],
      source_text_hash: await refTextHashOf(texts),
      locator: s.locator ?? null,
    });
  }
  return buildRefTable(entries);
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

/** 「依据与原文」表：按真实来源分组，多来源同号不合并（docs/23 §8.2）。 */
export function renderReferenceTable(doc: ContentDocumentV3, opts: RenderOptions = {}): string {
  const entries = Object.entries(doc.references);
  if (!entries.length) return "";
  const lines: string[] = ["## 依据与原文", ""];
  for (const [, ref] of entries) {
    const link = opts.sourceLinkOf?.(ref) ?? null;
    const title = opts.sourceTitleOf?.(ref) ?? "原始资料";
    const loc = locatorLabel(ref.locator);
    const where = loc ? ` · ${loc}` : "";
    lines.push(link
      ? `- ${title}${where}：[[${link}|查看原文]]（${ref.segment_ids.length} 段连续原文）`
      : `- ${title}${where}：（本地原文快照缺失，暂无法打开）`);
  }
  return lines.join("\n");
}

/** 该文档实际用到的独立来源数（真实来源记录，不按引用条数推断，docs/23 §6.2）。 */
export function independentSourceCount(doc: ContentDocumentV3): number {
  return new Set(Object.values(doc.references).map((r) => `${r.item_id}@${r.source_revision}`)).size;
}

export function blockCount(doc: ContentDocumentV3): number {
  return doc.sections.reduce((n, s) => n + s.blocks.length, 0);
}

// ---- 部分采纳：按块选择后重新组装正文与引用（docs/23 §6.3） ----

/**
 * 依用户勾选重建候选文档：保留的块按原顺序重组，未再使用的 `e` 引用被清除，
 * `quote` 块在重组后重新做逐字校验。选择只属于本次候选，不产生永久观点编号。
 */
export function selectBlocksForAdoption(
  doc: ContentDocumentV3,
  keep: boolean[][],
): { document: ContentDocumentV3; dropped_refs: number } {
  const used = new Set<string>();
  const sections: ContentSectionV3[] = [];
  doc.sections.forEach((section, si) => {
    const blocks: ContentBlockV3[] = [];
    section.blocks.forEach((block, bi) => {
      if (!keep[si]?.[bi]) return;
      blocks.push({ ...block, refs: [...block.refs] });
      for (const key of block.refs) used.add(key);
    });
    if (blocks.length || section.heading) sections.push({ heading: section.heading, blocks });
  });
  const references: Record<string, ContentRefV3> = {};
  const remap = new Map<string, string>();
  let n = 0;
  for (const [key, ref] of Object.entries(doc.references)) {
    if (!used.has(key)) continue;
    n += 1;
    remap.set(key, `e${n}`);
    references[`e${n}`] = ref;
  }
  for (const section of sections) {
    for (const block of section.blocks) {
      block.refs = block.refs.map((k) => remap.get(k) ?? k).filter((k) => Boolean(references[k]));
    }
  }
  const droppedRefs = Object.keys(doc.references).length - n;
  return {
    document: { ...doc, sections, references },
    dropped_refs: droppedRefs,
  };
}

/** 编辑或裁剪后重新核验：quote 必须仍在所引原文范围内逐字出现。 */
export function quoteVerificationErrors(
  doc: ContentDocumentV3,
  segmentTexts: (itemId: string, sourceRevision: number, segmentId: string) => string | null,
): string[] {
  const errors: string[] = [];
  doc.sections.forEach((section, si) => {
    section.blocks.forEach((block, bi) => {
      if (block.kind !== "quote") return;
      if (!block.refs.length) {
        errors.push(`第 ${si + 1} 节第 ${bi + 1} 块摘录没有依据`);
        return;
      }
      const joined = normalizeForQuote(block.refs.map((key) => {
        const ref = doc.references[key];
        if (!ref) return "";
        return refTextOf(ref.segment_ids.map((sid) =>
          segmentTexts(ref.item_id, ref.source_revision, sid) ?? ""));
      }).join("\n"));
      if (!joined || !joined.includes(normalizeForQuote(block.text))) {
        errors.push(`第 ${si + 1} 节第 ${bi + 1} 块摘录与所引原文不再逐字一致`);
      }
    });
  });
  return errors;
}

/** 校验引用表里的 source_text_hash 与本地固定原文一致（docs/23 §6.4 第 5、9 条）。 */
export async function referenceHashErrors(
  doc: ContentDocumentV3,
  segmentTexts: (itemId: string, sourceRevision: number, segmentId: string) => string | null,
): Promise<string[]> {
  const errors: string[] = [];
  for (const [key, ref] of Object.entries(doc.references)) {
    const texts = ref.segment_ids.map((sid) => segmentTexts(ref.item_id, ref.source_revision, sid));
    if (texts.some((t) => t === null || t === "")) {
      errors.push(`${key} 引用的原文在本地快照中不完整，暂缓采纳`);
      continue;
    }
    const hash = await refTextHashOf(texts as string[]);
    if (ref.source_text_hash && ref.source_text_hash !== hash) {
      errors.push(`${key} 的原文摘要与固定版本不一致（来源版本可能已变化）`);
    }
  }
  return errors;
}
