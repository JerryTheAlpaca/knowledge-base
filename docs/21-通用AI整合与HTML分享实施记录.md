# 通用 AI 整合与 HTML 分享：实施记录

实施依据：[docs/20-通用AI整合与HTML分享开发指南](docs/20-通用AI整合与HTML分享开发指南.md)。
记录日期：2026-09-20～09-21。状态：服务端与 Web 首版已实现并通过本机自动化验证；
**未部署到生产，未接真实模型 Key 做缓存命中验证，`SHARE_ENABLED` 默认仍为 false。**

## 1. 已实现范围

### M1 固定运行环境与可交付文件（`apps/share-renderer/`）

- `package.json` + `package-lock.json` 精确锁版本：esbuild 0.28.2、parse5 8.0.1、
  css-tree 3.2.1、acorn 8.18.0、chart.js 4.5.1、katex 0.18.7；检查工具
  playwright 1.60.0（对应 Chromium build 1223）。任务执行阶段不装包、不取 CDN。
- `runtime-manifest.json`：`runtime_version=share-runtime-1.0.0`、依赖精确版本与许可证
  （含 chart.js 的传递依赖 @kurkle/color 0.3.4）、允许的 import specifier、
  构建参数与给模型的 import 示例、禁止的运行能力清单。
- `src/checks.mjs`：page_source Schema、HTML（禁用标签/内联事件/任何 src、href、
  srcset、SVG 外链）、CSS（`url()` 与 `@import` 都算资源依赖）、JS（网络、导航、
  弹窗、动态导入、Worker、eval、`new Function`、变量属性绕过）按语法树检查。
- `src/build.mjs`：§7.4 的固定顺序；依赖只按清单登记解析，用 esbuild metafile 反向
  确认没有未登记输入与残留动态依赖；素材按魔数核对 MIME 后转 data URL；
  KaTeX 样式与 woff2 字体内联；成品超过 `max_html_bytes` 明确失败。
- `src/shell.mjs`：可信外层 + `iframe sandbox="allow-scripts"`（不放开
  `allow-same-origin`）的 srcdoc 子页面；srcdoc 继承外层策略，因此外层 CSP 同时
  列出桥接脚本与子页面脚本的 sha256；`frame-ancestors` 不写在 meta（浏览器会忽略
  并报错），改由分享站点响应头下发；`</script>`、`<!--`、U+2028/2029 边界都做了转义。
- `src/browser.mjs`：1440×900 与 390×844 两视口、断网上下文、pageerror/console/
  资源失败/意外请求捕获、固定 ready 信号（不用 networkidle）、主体非空与全局横向
  溢出检查、按 `interactions` 声明真实点击/填充/选择/拖动并核对结果、有界分段截图。
- `src/runner.mjs` + `src/cli.mjs`：`SHARE_SPOOL_DIR` 的 ready/working/done/failed
  原子交接（rename 领取、结果回传 `lease_id`），`seal/build/check/runner/doctor` 入口。
- 手写样本 `samples/prose`（纯文字）、`samples/rich`（SVG 结构图 + Chart.js + KaTeX
  + 参数演示 + 一条交互声明）；`tests/` 17 个用例，含一段**故意绕过静态扫描**的脚本
  验证真实隔离仍然生效（A15/A16）。

### M2 数据模型、多轮会话与编排 Worker

- 新表 `share_works / share_runs / share_revisions / share_artifacts /
  share_conversations / share_messages`；`provider_operations` 增加可空
  `share_run_id`、`step_key`、`conversation_id`、`context_epoch`、`input_message_seq`、
  `prefix_hash`、`usage_json`，并加 CHECK 保证 `job_id` 与 `share_run_id` 互斥。
  迁移 `f1d3b5c7e9a2` 已在临时库上做过 upgrade → downgrade → upgrade 往返。
- `providers/llm.py`：新增 `ConversationMessage / ConversationRequest /
  generate_conversation()`，保留 `generate()` 内部映射为两条消息并共用同一发送与
  错误分类；返回可回放的 assistant 消息、`truncated`（finish_reason=length）与
  归一化 usage（openai_chat / openai_responses / deepseek / anthropic 四套口径，
  缺失为 null 不填 0）；缓存参数只在 profile 明确核验支持时发送。
- `domain/sharing.py`：source_pack 适配与校验、`clarification.json` /
  `synthesis.json` / `page_source.json` 规则、带命名空间引用
  （`s1:segment:seg0001`）定位与短引逐字核对、每篇材料必须交代用途、状态文案映射。
- `domain/share_conversations.py`：稳定前缀装配与 `prefix_hash`、回答规范化成一条
  可重放的 user 消息、需求摘要合并与来源归属（用户已定的项不被模型改写）、
  委托 AI 决定时固化假设、压缩阈值判断与新 epoch 起点。
- `domain/share_prompts.py`：三段固定 system 规则、由 runtime-manifest 装配的短版
  运行手册、澄清/整合/代码/修复的尾部消息。
- `workers/share.py`：preparing → clarifying →（等待用户／等待确认）→ synthesizing →
  generating → packaging → awaiting_runner →（repairing）→ 可用草稿；一次领取内连续
  推进，等待类阶段释放租约；ProviderOperation 生命周期与 unknown_outcome 不重发；
  心跳续租；只有哈希匹配的成品才成为版本。
- `workers/share_retention.py`：分享对象与 StoredFile／Bundle 清单／Upload／
  AudioAsset 统一存活判断，过期引用按小批量在写事务内解除并回收，spool 临时目录
  按 TTL 清理；Bundle 到期分支现在也走同一判断（不再无条件删 manifest）。
- `reconcile.py` 识别 `share_run_id` 归属，不把 `job_id=null` 的分享调用当成无主操作。

### M3 Web 创作流程（`web_static/js/shares.js` 等）

- 列表「选择」模式（翻页/筛选不清空、localStorage 持久、底部条显示已选数量、
  清空/退出/生成）、单篇详情「用这篇生成分享页」入口。
- 创作面板：材料清单 + 单个要求输入框；材料不可读时就地标出具体条目并给
  「移除／补充材料」，服务端 422 的 `unreadable_items` 同样落到条目上，不悄悄排除。
- 需求对话：问题 + 快捷选项 + 自由回答，可一次答整组；「目前的需求」可展开并标
  「AI 暂定」；确认并生成／再调整一下／按你的建议生成三条出口。
- 阶段状态只显当前一步（文案表来自服务端），关闭页面不取消任务、重进恢复；
  「停止生成」如实说明已发出的调用不能撤回。
- 作品列表、预览（`preview-content` 取 JSON 后由可信容器赋值 `srcdoc`）、
  继续提要求、下载 HTML、分享／撤销。轮询只在任务活跃时继续，且预览只在版本
  真的变了才重设 srcdoc。

### M4 分享、撤销与边界

- `routes_share_public.py`：`GET /s/{token}`、`GET /preview/{token}`，只核验令牌、
  发布状态与有效期，无效／撤销／到期统一返回不泄露标题的不可用页面；响应
  `no-store` + `X-Robots-Tag: noindex, nofollow` + `Referrer-Policy: no-referrer`，
  不种 Cookie；非分享域名请求一律不可用。
- `security/share_tokens.py`：≥32 字节随机令牌、库存摘要 + 按用户加密的可复制原文、
  用途派生密钥与预览 HMAC 凭据（默认 300 秒）。
- `deploy/docker-compose.yml`：新增 `share_worker`（有库与本人凭据）与
  `share_runner`（`network_mode: none`、根文件系统只读、tmpfs、cap_drop ALL、
  no-new-privileges、pids/mem/cpus 限额、不挂 data 卷与 Docker socket）；
  `apps/share-renderer/Dockerfile` 用 Playwright 官方镜像按锁文件安装固定依赖。
- `deploy/Caddyfile`：分享站点片段——只代理 `/s/*` 与 `/preview/*`，删除传入
  Cookie/Authorization，其余路径 404，访问日志对这两类路径做 URI 脱敏。
- `contracts/openapi.json` 已重新生成（新增 17 条路径，无删除）。

## 2. 实际做过的验证

| 检查 | 结果 |
| --- | --- |
| `python -m pytest`（服务端全量） | 369 passed |
| 分享相关新增用例 | 会话与缓存 9、编排 Worker 8、私有接口 14、公开与生命周期 7 |
| `npm test`（runner） | 17 passed，含断网 file:// 打开、CSP 哈希一致、真实隔离探测 |
| Alembic 迁移 | 临时库上 upgrade→downgrade→upgrade 往返通过，CHECK/索引/FK 均落库 |
| 浏览器端到端走查 | 用本机走查服务（伪中心登录 + 假模型响应）+ **真实 runner**：勾选两篇材料 → 生成分享页 → 回答 2 个问题 → 确认摘要 → 成品出现在预览 iframe → 子页面「来源」按钮打开外层来源面板 → 下载/分享按钮可用 → 390px 视口无横向溢出 → 页面无 JS 报错 |
| 回归 | 音频原件保留、Bundle 到期清理等既有用例在改动后仍通过（曾出现自引用导致 Bundle 永不回收，已修） |

## 3. 尚未验证与已知限制

- **真实模型的多轮缓存命中（A39）未验证**：本轮没有用授权 Key 发起付费调用，
  只完成请求装配与 usage 归一化的模拟测试。上线前需按实际 endpoint＋model 跑
  首轮 + 至少两轮，记录真实 usage；未命中就标「尚未验证」，不用模拟结果代替。
- 生产分享域名尚未确定：`SHARE_PUBLIC_BASE_URL` 为空时「生成分享链接」返回
  明确的配置未完成原因，私有预览与下载可用。Caddy 片段里的域名是示例，
  必须换成与账号站不同可注册域名后才可用。
- 2 核 2GB 上的真实内存峰值、runner 限额与 ASR 重型任务准入互斥**未实测**；
  当前 512MiB/单并发是试运行初值。§14.2 提到的共享重型任务槽位只做了
  `SHARE_RENDER_CONCURRENCY` 与串行 runner，未做跨 ASR 的租约式互斥。
- 真实 Safari/iPhone 检查、代表样本（专业概念／中医／数学／跨领域）的
  人工内容复核未做（M5）；Chromium 移动视口不等于 iOS 实机。
- 首版限制：Web 只能用服务器已有且属于本人的材料；修改接口不增减材料；
  不接 ASR/OCR/重新抓取；不联网搜索；未配置分享站点时公开链接不可用。
- 需求摘要的来源归属按「本轮是否由用户回答推动」记录，不是逐字段向模型求证；
  界面把 AI 填的项标成「AI 暂定」，用户回答优先。
- 静态检查是执行契约与找错，不是任意 JS 的安全证明；真实隔离靠 sandbox、CSP、
  不透明源与 runner 的网络命名空间。

## 4. 启用状态与剩下的步骤

已做（2026-09-21）：
1. 迁移 `f1d3b5c7e9a2` 已随自动部署应用到生产库（公网 `/s/{无效令牌}` 返回
   不可用页而不是 500，可证分享表已存在）。
2. `deploy/docker-compose.yml` 里 api 与 worker 设 `SHARE_ENABLED: "true"`，
   Web 生成入口随之开放。
3. 服务器 `deploy/.env` 设 `COMPOSE_PROFILES=share`，让 `share_worker` 与
   `share_runner` 参与构建与启动；启用前它们不进部署路径。

部署侧踩到的一个真问题：`kb-auto-deploy.service` 原来是 `TimeoutStartSec=15min`，
而 share_runner 的基础镜像约 1.9GB、国内到 MCR 实测约 1MB/s，首次拉取要 20–30 分钟——
systemd 会在下载中途杀掉构建，下一轮定时器又从头再拉，表现为「代码推上去了、功能静默地
一直没部署」。已把超时放宽到 45min（`deploy/systemd/kb-auto-deploy.service`），
并在这次启用时先在窗口外 `docker pull` 基础镜像。服务器侧启用还需要
`deploy/.env` 里的 `COMPOSE_PROFILES=share`（该文件不进仓库，删掉这行即回到不构建、不启动）。

仍待做：
1. 用授权模型配置完成 §6.5.6 的真实多轮缓存命中验证（A39）与 §16 针对性验收。
2. 记录 2GB 机器上 runner 的真实内存峰值与 ASR 同时运行时的表现，再决定
   `mem_limit` 与是否升到 4GB。
3. 需要公开分享链接时才配独立可注册域名、DNS 与 Caddy 片段；只用「下载 HTML」
   不需要这一步，未配置时点「分享」会得到明确的「还没有配置独立的分享站点」提示，
   私有预览与下载不受影响。
4. 四类真实领域材料（专业概念整合、中医比较、数学讲解、跨领域）的人工内容复核。

回滚只关新任务与新发布入口；已发布作品可继续由只读分享路径服务，
不删除已保存作品、来源快照，也不立即 downgrade 数据表。
