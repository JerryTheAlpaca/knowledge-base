# Obsidian 插件精简为纯同步：实施记录

记录日期：2026-10-03。状态：代码改造完成，`tsc --noEmit` 通过，冒烟测试 43 项全绿，
`main.js` 已重新构建；**未在真机 Vault 里跑过一轮真实同步**。

## 1. 为什么要改

插件此前同时承担两件事：把云端 Bundle 同步进 Vault（同步），以及在本机调模型把 Digest
整理成主题笔记并写入 `03 Knowledge`（整理）。整理这条链路涉及本地模型直连、线上 Key
绑定、目标选择与融合提示词、候选采纳与回滚、旧知识引用迁移十余个模块，是插件体积和
故障面的主要来源；而云端已经产出 `content.json`，整理层在当前产品形态下并不产生用户
依赖的产出。

本次改造按用户要求：先备份现版，再删除整理功能，只保留同步。

## 2. 备份

两处，均在删除任何代码之前完成：

- 目录级完整拷贝：`.workbuddy/backups/obsidian-plugin-full-2026-10-03/`，含 `src/`、
  `scripts/`、`manifest.json`、`package.json`、`package-lock.json`、`tsconfig.json`、
  `esbuild.config.mjs`、`styles.css`、构建产物 `main.js` 与 `node_modules`（44MB）。
  `.workbuddy/` 已在 `.gitignore` 中，不进入提交。
- git tag 锚点：`backup/plugin-with-organize-2026-10-03`，指向改造前的 HEAD。

## 3. 删除范围

整目录删除：

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `src/knowledge/organize.ts` | 1002 | 整理服务：目标选择、融合、候选状态机、差异渲染、采纳与回滚 |
| `src/knowledge/index.ts` | — | 主题索引、中文分词、匹配排序 |
| `src/knowledge/prompts.ts` | — | 目标选择与融合的系统提示词、输出校验 |
| `src/knowledge/citations.ts` | — | 旧 evidence_map 展开、快照锚点 |
| `src/knowledge/migrate.ts` | — | 混合模块，见下 |
| `src/views/organizePanel.ts` | — | 整理面板视图 |
| `src/providers/local.ts` | — | 本地模型解析、直连请求、错误分类 |
| `src/providers/transport.ts` | — | Obsidian 侧 HTTP 传输 |

`src/knowledge/migrate.ts` 原本混装两件独立的事，本次按职责拆分：旧知识引用迁移（属整理层）
丢弃，文件重命名迁移（属布局职责）保留并迁到新文件 `src/vault/rename.ts`。

其余文件的删除：

- `src/types.ts`：删掉 `ContentSubjectV3`、`LocalModelMode`、`LocalModelConfig`、
  `OrganizeTaskState`、`OrganizeTask`、`TopicProposal`、`LegacyProposal`、
  `KnowledgeIndexEntry`、`KnowledgeIndex` 及相关版本常量；`KbSettings` 去掉
  `localModel`、`localOrganizeEnabled`、`autoPrepareOnSync`、`organizeDeviceId`。
  `knowledgeFolder` 作为历史字段保留在类型里，但设置面板不再暴露，Vault 中已有的
  Knowledge 笔记文件不受任何影响。
- `src/vault/template.ts`：删掉 `LOCAL_ORGANIZE_*`、`KNOWLEDGE_*`、
  `KNOWLEDGE_HISTORY_*` 标记与 `renderLocalOrganize`、`renderKnowledgeNote`、
  `extractKnowledgeScope`、`renderProposalIndex`；`partitionHashes` 只返回
  `cloud_digest`；`managedTags` 的 kind 收窄为 `source` / `digest`；
  `renderDigestNote` 不再输出本地整理分区与 `kb_organize`。
- `src/vault/paths.ts`：删掉 `knowledgeNotePath`、`knowledgeIdFromTitle`、
  `knowledgeIndexPath`、`proposalsDir`、`organizeDir`、`knowledgeRefsFileName`、
  `bundleDirName`；`revisionDir` 的 kind 收窄为 `digests`。
- `src/vault/content.ts`（776 → 241 行）：只留解析与渲染，即 `parseContentDocument`、
  `renderContentMarkdown`、`renderCompletenessNotice` 及其私有辅助。组装侧
  （`assembleContentDocument`、`buildRefTableFromSegments`、`selectBlocksForAdoption`、
  `quoteVerificationErrors`、`referenceHashErrors`、`renderReferenceTable` 等）随整理层
  一并删除——正文由云端组装，本插件只消费。
- `src/vault/records.ts`：`RevisionStore` 的 kind 收窄为 `digests`。
- `src/settings.ts`（562 → 约 230 行）：设置面板移除整个「模型设置」本地模型区与全部
  「线上 Key 绑定」；移除整个「整理知识库」分区（启用本地整理、自动准备整理候选、
  整理设备、打开整理面板）；「模型设置」中的「云端提炼」下拉保留（Key 由服务器托管，
  与本地整理无关）；`SecretBridge` 只管服务 Token。
- `src/api.ts`：删掉 `localBindingStatus`、`bindLocalKey`、`unbindLocalKey` 与
  `getReading()`；`deviceStart` 不再申请 `profiles:bind-local` scope。
- `src/sync/engine.ts`：`EngineDeps` 去掉 `onDigestWritten`；`docs.ensure()` 不再登记
  Knowledge 层；`writeDigestNote` 的更新分支只做 frontmatter 外科式更新 + 云端区替换 +
  标签合并，不再触发任何整理回调。同步主链路（事件拉取、pending、格式闸门、暂存校验、
  Source/Digest 写入、commit、回执、Inbox 索引重建、回执恢复）未改动。
- `src/main.ts`（704 → 约 330 行）：删除整理面板视图注册与实例、相关 ribbon 图标、
  4 个整理命令（整理当前 Digest、准备待整理、打开候选索引、迁移旧知识）与登录时的
  `organizeDeviceId`。保留 5 个命令：立即同步、查看状态、恢复被抑制条目、
  文件改名迁移、重建文档索引。`rebuildDocumentIndex` 只扫 Sources + Digests。
- `styles.css`：删掉只服务整理面板的 `kb-btn`、`kb-btn-mid`、`kb-actions`、
  `kb-list-row`、`kb-list-row-roomy`、`kb-edit-body`、`kb-diff-pre`；保留状态视图在用的
  `kb-btn-wide`。
- `scripts/smoke.ts`（2138 → 约 1000 行）：删除知识索引与匹配、提示词契约、
  `OrganizeService` 端到端、本地模型解析与错误分类、旧知识迁移、`assembleContentDocument`
  与部分采纳等约 30 个用例。保留路径安全、可读文件名、分区读写与写保护、两层模板、
  frontmatter 合并、标签合并、多笔记 commit、抑制、格式闸门、v3 解析与渲染、共享夹具
  跨实现互校、文档索引、同步端到端、文件改名与恢复。
- `scripts/acceptance-headless.ts`：去掉 `defaultLocalModel()`。

## 4. 保留范围（同步链路）

事件拉取 → 本地 pending → 游标推进 → 暂存下载与 SHA-256 校验 → 内容格式闸门
（v3 `content.json`）→ 原子落盘 → Source + Digest 双笔记 → 文档索引登记 → commit 标记
→ 回执发送 → 清理。另含：写保护与冲突文件（`merge_needed`、`conflicts/`）、
`Suppression`（用户删掉 Source 笔记后停止复建）、`FormatGate`（格式不对时暂停该项并提示
升级）、文件重命名迁移与恢复、Inbox 索引重建。

## 5. 兼容性说明

- 既有 Digest 笔记里的 `kb:local-organize` 分区标记与内容**原样保留**：`engine.writeDigestNote`
  只对云端区做 `replacePartition`，不触碰其它分区，用户 Vault 不会被改动。
- `03 Knowledge` 目录不再被插件写入或索引，已有 Knowledge 笔记文件不受影响。
- `layout_version` 未变（仍为 3），LAYOUT_VERSION 常量保持原值；本次是能力删减，不是
  布局变更，不需要迁移既有笔记。
- 登录 scope 收窄。若此前授权过 `profiles:bind-local`，新的 `deviceStart` 不再申请该 scope，
  已有的设备 Token 继续可用。

## 6. 验证

- `npm run typecheck`（`tsc --noEmit --skipLibCheck`）：通过，无错误。
- 冒烟测试：
  ```
  node_modules/.bin/esbuild scripts/smoke.ts --bundle --platform=node --format=cjs \
    --alias:obsidian=./scripts/obsidian-stub.ts --outfile=.smoke/smoke.cjs && node .smoke/smoke.cjs
  ```
  43 项全绿。
- `npm run build`：通过，`main.js` 重新产出（63KB，较改造前的构建产物明显变小）。
- `scripts/acceptance-headless.ts` 单独 esbuild 打包通过。

未做的事：没有在真机 Vault 里跑一轮真实同步；没有覆盖 M3 真机验收清单（docs/06）里与
整理相关的历史用例——那些用例对应的功能已不存在，按需从验收清单中移除。

## 7. 相关文件

- 备份目录：`.workbuddy/backups/obsidian-plugin-full-2026-10-03/`
- git tag：`backup/plugin-with-organize-2026-10-03`
- 原三层设计（已不适用于插件，供追溯）：[docs/08](08-Obsidian三层知识库实现方案.md)
- 内容协议 v3 契约（本次同步链路仍依赖）：[docs/24](24-内容协议v3契约冻结与实施记录.md)