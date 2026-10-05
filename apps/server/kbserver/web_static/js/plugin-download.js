// plugin-download.js — 插件下载入口（docs/26）
//
// 两个入口共用同一段渲染：设置页「Obsidian 知识库」卡片（未连接时）与
// 新手指导第二步。放同一个模块是为了安装说明只写一份，不会两处漂移。
//
// zip 由服务端现打（/downloads/golden-rose-inbox.zip），无需登录：
// 拿到插件的人本来也要用自己的账号在插件里登录才有数据。

import { esc } from "./api.js";

export const PLUGIN_ID = "golden-rose-inbox";
export const DOWNLOAD_URL = "/downloads/" + PLUGIN_ID + ".zip";

// 向下箭头进托盘：与页面上其他描边图标（信息气泡等）同一套画法，
// 1.8 描边 + round 端点，随字色走因而在 primary 按钮上自动取 --on-accent
const DOWNLOAD_ICON =
  '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"' +
  ' stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  '<path d="M12 3.5v11"/><path d="M7.5 10.5 12 15l4.5-4.5"/><path d="M4.5 19.5h15"/>' +
  "</svg>";

// o.compact：设置页卡片里的紧凑排版（不重复标题）
export function pluginInstallHTML(o = {}) {
  return '<div class="pd">' +
    '<div class="row pd-row">' +
      '<a class="btn primary pd-btn" href="' + DOWNLOAD_URL + '" download>' +
        DOWNLOAD_ICON + "下载插件</a>" +
    "</div>" +
    '<ol class="small steps-list pd-steps">' +
      "<li>解压后把 <b>" + PLUGIN_ID + "</b> 文件夹放进你的库：" +
        "<code>.obsidian/plugins/</code> 下。</li>" +
      "<li>重启 Obsidian，在「设置 → 第三方插件」里启用它。</li>" +
      "<li>在插件设置里用同一账号登录。</li>" +
    "</ol>" +
    (o.note ? '<p class="help pd-note">' + esc(o.note) + "</p>" : "") +
  "</div>";
}
