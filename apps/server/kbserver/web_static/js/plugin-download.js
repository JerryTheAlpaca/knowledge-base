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
    '<a class="btn primary pd-btn" href="' + DOWNLOAD_URL + '" download>下载插件</a>' +
    '<span class="pd-ver small" data-plugin-version hidden></span>' +
    '<ol class="small steps-list pd-steps">' +
      "<li>解压后把 <b>" + PLUGIN_ID + "</b> 文件夹放进你的库：" +
        "<code>.obsidian/plugins/</code> 下。</li>" +
      "<li>重启 Obsidian，在「设置 → 第三方插件」里启用它。</li>" +
      "<li>在插件设置里用同一账号登录，这页会自动变成「已连接」。</li>" +
    "</ol>" +
    (o.note ? '<p class="help pd-note">' + esc(o.note) + "</p>" : "") +
  "</div>";
}

// 版本号是补充信息，取不到就不显示，不影响下载本身。
// 结果缓存：新手指导每 5 秒重渲染一次卡片，不缓存等于每轮多打一个请求。
let versionCache = null;

export async function loadPluginVersion() {
  const nodes = document.querySelectorAll("[data-plugin-version]");
  if (!nodes.length) return;
  const fill = (v) => nodes.forEach((n) => { n.textContent = "v" + v; n.hidden = false; });
  if (versionCache) { fill(versionCache); return; }
  try {
    const res = await fetch("/v1/plugin/release", { credentials: "same-origin" });
    if (!res.ok) return;
    const info = await res.json();
    if (!info || !info.version) return;
    versionCache = info.version;
    fill(versionCache);
  } catch (e) { /* 版本号拿不到就不显示 */ }
}
