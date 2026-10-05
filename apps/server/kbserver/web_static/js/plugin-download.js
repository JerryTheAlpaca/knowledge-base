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

// o.compact：设置页卡片里的紧凑排版（不重复标题）
export function pluginInstallHTML(o = {}) {
  return '<div class="pd">' +
    '<a class="btn primary small pd-btn" href="' + DOWNLOAD_URL + '" download>下载插件</a>' +
    '<ol class="small steps-list pd-steps">' +
      "<li>解压后把 <b>" + PLUGIN_ID + "</b> 文件夹放进你的库：" +
        "<code>.obsidian/plugins/</code> 下。</li>" +
      "<li>重启 Obsidian，在「设置 → 第三方插件」里启用它。</li>" +
      "<li>在插件设置里用同一账号登录。</li>" +
    "</ol>" +
    (o.note ? '<p class="help pd-note">' + esc(o.note) + "</p>" : "") +
  "</div>";
}
