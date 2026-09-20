// workflow.js — 只渲染服务端 WorkflowView，不重新推断业务状态（docs/17 §11）
//
// 状态语义全部来自 /v1/items 返回的 workflow 字段：
// - steps：节点集合按条目动态（提取/语音识别/整理/发布）；「语音识别」节点只在
//   会用到 ASR 的条目出现（录音、网页音轨、B 站无字幕转写），其余条目三节点直达整理
//   每步的 label 由服务端生成，前端直接渲染
// - delivery.status：not_ready | waiting_obsidian | connect_obsidian | published | downloaded
// - reason_code 是机器码，本模块绝不显示它，只显示 message。

import { esc } from "./api.js";

export const STAGE_LABELS = { extract: "提取", process: "加工", organize: "整理", publish: "发布" };

// 主按钮文案：前端按稳定 action code 本地化（§10.2）
// platLabel 来自条目的 source_label：「连接/更新哪个平台」由条目决定，不写死在码里
export function actionLabel(code, platLabel) {
  const plat = platLabel || "该平台";
  const MAP = {
    supplement: "补充内容",
    choose_model: "选择整理模型",
    connect_obsidian: "连接 Obsidian",
    connect_platform: "连接" + plat,
    update_session: "更新" + plat + "登录信息",
    retry: "重试",
    start_organize: "开始整理",
    start_optimize_text: "开始优化文本（纠错与分段）",
    choose_audio: "选择音频",
    refresh: "刷新",
  };
  return MAP[code] || null;
}

// 可用操作 → 更多菜单项文案（§7.5：不存在对应能力时不显示菜单项）
// view_source 不再进菜单（2026-09 走查：原始内容页签已常驻，菜单项冗余）
export function availableActionLabels(platLabel) {
  const plat = platLabel || "该平台";
  return {
    refetch: "重新提取",
    cancel_process: "取消转写",
    retry_process: "重新转写",
    supplement: "补充材料",
    choose_model: "选择整理模型",
    start_organize: "重新整理",
    start_optimize_text: "优化文本（纠错与分段）",
    connect_obsidian: "连接 Obsidian",
    connect_platform: "连接" + plat,
    update_session: "更新" + plat + "登录信息",
  };
}

const STEP_MARKS = { done: "✓", skipped: "—", attention: "!", failed: "!" };

// 四阶段状态图（§6.1/§6.2）：圆点连线；发布节点只在 receipt 到达后点亮
export function stepperHTML(wf) {
  const steps = (wf && wf.steps) || [];
  let html = '<div class="stepper">';
  steps.forEach((s) => {
    let cls = s.status;
    if (s.status === "completed") cls = "done";
    const mark = STEP_MARKS[cls] || (cls === "done" ? "✓" : "");
    html += '<div class="step ' + cls + '">' +
      '<span class="sball">' + mark + "</span>" +
      '<span class="slabel">' + esc(s.label || STAGE_LABELS[s.id] || s.id) + "</span>" +
      '<span class="sline" aria-hidden="true"></span></div>';
  });
  // 已完成（回执到达或原文下载走）：最后一个节点完成时一次缩放淡入（§6.2）
  if (wf && wf.overall_state === "published") {
    html = html.replace(/class="step done"(?!.*class="step done")/, 'class="step done published-flash"');
  }
  html += "</div>";
  return html;
}

function barHTML(progress, indeterminate) {
  if (indeterminate) return '<div class="panelbar indeterminate"><i></i></div>';
  if (progress == null) return "";
  return '<div class="panelbar"><i style="width:' + Math.min(100, progress) + '%"></i></div>';
}

// 当前阶段面板（§6.3）：一个面板、至多一个主按钮
// extraHTML 由详情页传入（如音频候选选择），保持「面板内不拼业务状态」
export function stagePanelHTML(wf, { primaryHandler = "data-stage-action", platLabel = "" } = {}) {
  if (!wf) return "";
  const running = wf.overall_state === "working";
  const indeterminate = running && wf.progress_percent == null && wf.current_stage !== "publish";
  const showBar = running || (wf.progress_percent != null);
  let html = '<div class="stagepanel tone-' + esc(wf.overall_state) + '">';
  html += '<div class="panelhead"><span class="paneltitle">' + esc(wf.message || "") + "</span>";
  if (wf.progress_percent != null) html += '<span class="panelpct num">' + wf.progress_percent + "%</span>";
  html += "</div>";
  if (showBar) html += barHTML(wf.progress_percent, indeterminate);
  if (wf.overall_state === "working") {
    html += '<div class="panelnote">你可以离开此页面，服务器会继续处理。</div>';
  }
  const label = wf.primary_action ? actionLabel(wf.primary_action, platLabel) : null;
  if (label) {
    html += '<div class="panelaction"><button class="primary" ' + primaryHandler + '="' +
      esc(wf.primary_action) + '">' + esc(label) + "</button></div>";
  }
  html += "</div>";
  return html;
}

// 列表行的状态行（§4.5）：只显示用户文案 + 来源标签 + 真实进度
export function listRowAux(wf, it) {
  const src = esc((it && it.source_label) || "");
  if (!wf) return '<span class="msg"></span><span class="grow"></span><span class="src">' + src + "</span>";
  let msg = '<span class="msg' +
    (wf.overall_state === "failed" ? " bad" : (wf.overall_state === "attention" ? " warn" : "")) +
    '">' + esc(wf.message || "") + "</span>";
  let bar = "";
  if (wf.overall_state === "working" && wf.current_stage === "process") {
    const pct = wf.progress_percent;
    if (pct != null) {
      bar = '<div class="itembar"><i style="width:' + Math.min(100, pct) + '%"></i></div>';
    } else {
      bar = '<div class="itembar indeterminate"><i></i></div>';
    }
  }
  return msg + '<span class="grow"></span><span class="src">' + src + "</span>" + bar;
}
