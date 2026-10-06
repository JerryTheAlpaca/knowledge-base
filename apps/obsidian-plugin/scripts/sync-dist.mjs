// sync-dist.mjs — 把插件的三个发布文件同步到服务端镜像内
//
// 为什么放在服务端包里：api 镜像的 build context 是 apps/server，
// 拿不到 apps/obsidian-plugin 的源码，也就没法在部署时现场打包 esbuild
// （服务器上没有 node）。所以发布产物作为文本文件随服务端一起入库，
// 由 /downloads/golden-rose-inbox.zip 在请求时现打成 zip。
//
// 改动插件源码后跑一次：npm run build && npm run dist
// 两个命令都在 package.json 的 scripts 里；dist 之前必须先 build。

import { mkdirSync, copyFileSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const PLUGIN_DIR = join(HERE, "..");
const OUT_DIR = join(PLUGIN_DIR, "..", "server", "kbserver", "plugin_dist");

// Obsidian 只认这三个文件：入口、清单、样式。缺一个插件都装不上。
const FILES = ["manifest.json", "main.js", "styles.css"];

const manifest = JSON.parse(readFileSync(join(PLUGIN_DIR, "manifest.json"), "utf8"));
if (manifest.id !== "golden-rose-inbox") {
  // 目录名必须与 manifest.id 一致：Obsidian 按 .obsidian/plugins/<id>/ 加载
  console.error(`manifest.id 是 ${manifest.id}，与约定的 golden-rose-inbox 不一致`);
  process.exit(1);
}

mkdirSync(OUT_DIR, { recursive: true });
for (const name of FILES) {
  copyFileSync(join(PLUGIN_DIR, name), join(OUT_DIR, name));
}
console.log(`dist v${manifest.version} -> ${OUT_DIR}（${FILES.join(", ")}）`);
