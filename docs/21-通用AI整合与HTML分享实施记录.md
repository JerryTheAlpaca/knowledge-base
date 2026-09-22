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

- 入口在「已收集」抽屉的工具栏：按钮叫「分享」，按下去点一条就选中——整张卡片四周亮起来
  （描边 + 一圈外发光 + 内侧一点反光），行内不再摆圆框那类第二套控件；再点一次取消，
  再按「分享」或 Esc 退出多选。底部条一行摆「已选 N 篇 ｜ 清空 ｜ 完成」（320px 也不换行）；
  单篇详情保留「用这篇生成分享页」入口。
- 「完成」就地弹一个小菜单三选一：下载原文（`readable.md`，回退 `normalized.md`）、
  下载整理稿（`preview.md`）、制作 HTML。前两条逐条走 `/v1/items/{id}/reading` 拿文件
  清单，再取 `bundles/{rev}/files/{file_id}` 按标题存成 `.md`；没有这份文件的条目跳过，
  提示里说清几篇没有。下载原文顺带登记 `source-download`，把条目推到「已下载」。
- 创作页直接搭在首页之上（`#shareStage`）：金蔷薇盖一层**只压暗、不加模糊**的强遮罩
  退成背景（糊成一片会被读成「太模糊」），中间是多轮对话流，底部复用首页那副胶囊
  输入框——静置没字就是胶囊，一打字长成圆角矩形（同一套 `.smartbox` 两态与 90ms 形变，
  发送钮两种形态下 x 完全相同），右上角叉叉退出（Esc、浏览器返回同效）；顶栏改成三横线，
  进去是以前的所有对话（标题 + 状态 + 版本 + 时间），点开继续。
- 底部只有一个输入框，这句话按状态决定算什么：开工要求／回答本轮问题／确认阶段直接
  说哪里要改／已有成品时继续提修改。为此 `POST …/messages` 在 `awaiting_confirmation`
  也受理（空内容 422），确认阶段的出口是输入框上方那组「确认并生成／按你的建议生成」。
- 草稿态先列选中的材料；材料不可读时就地标出具体条目并给「移除／补充材料」，
  服务端 422 的 `unreadable_items` 同样落到条目上，不悄悄排除。
- 轮询只重画变化的部分：对话流按签名比对决定是否重建，输入框和按钮在流外面，
  所以打字不会被轮询顶掉；新版本做出来时成品卡片自动展开预览。
- 作品跑完后 `active_run_id` 会摘掉，`GET /v1/shares/{id}` 现在回退到最近一轮，
  否则「以前的对话」点进去只剩一张成品卡、看不到当时问过答过什么。
- 阶段状态只显当前一步（文案表来自服务端），关闭页面不取消任务、重进恢复；
  「停止生成」如实说明已发出的调用不能撤回。
- 预览取 `preview-content` 的 JSON 后由可信容器赋值 `srcdoc`，iframe 只开
  `allow-scripts`；下载 HTML、分享／撤销都在这张成品卡片上。

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
| 生产容器内 `share_runner doctor` | `browser: ok`：非 root（pwuser）＋`network_mode: none`，用真实交付件走完构建与浏览器检查，不是空白占位页。**当时补的一句「Chromium sandbox 开启」不成立**：那一版 `launch()` 没传 `chromiumSandbox`，而 Playwright 的默认值就是往启动参数里加 `--no-sandbox`，doctor 也验不到这个开关（docs/22 C-01）。现在显式传 `true`、doctor 报 `sandbox_enabled`，这一条要在部署机重跑一次才算数 |
| 生产容器内跑代表样本 | `check --task samples/rich`（chart.js＋KaTeX＋声明式交互）在 runner 容器内 `ok: true`、诊断 0 条、四张截图（桌面/移动各 2） |
| 2GB 机器上的 runner 内存 | 上述一次完整构建＋浏览器检查峰值 226MiB，限额 512MiB；同期宿主机 available 约 1.0GB |
| 生产容器内 spool 交接往返 | 常驻 runner 领取伪造任务（rich 样本）→ 产出 1.49MB 单文件成品＋四张截图 → 服务端 worker 读回 `result.json` 并清理，两端跨 uid 都可读写可删。**当时靠的是目录 0777 ＋ runner `umask 0`**，已按 docs/22 C-02 换成两端同 gid ＋ 2770 ＋ `umask 002`，这条同样要重跑一次确认（卷在宿主上的最终 mode、runner 实际进程组） |
| 2026-09-21 分享页改成对话舞台后 | 服务端全量 371 passed（新增「确认阶段还能补一句」「跑完的作品保留对话」两例）；页面走查用的是本机静态服务 + 打桩 `/v1` 响应（未接真实模型与 runner）：多选→草稿→材料不可读的「移除/补充材料」→等待回答/确认/进行中/成品/失败五种状态的按钮与输入框文案→叉叉与 Esc 逐级返回→打字不被轮询顶掉→1280 与 390 视口均无横向溢出 |
| 2026-09-21 多选点亮态与「完成」三选一 | 本机 `dev_inbox_server`（伪造会话 + 演示数据）走查：选中行 `box-shadow` 三段金光到位、`::after` 已无内容、行右内边距回到 16px；底部条在 320/360/390/1280 都是单行（高 50，三个子元素同一中线）且不越界；勾 3 篇 → 下载整理稿得到 2 个 `-整理稿.md`（缺 `preview.md` 那篇被跳过并在提示里报数），下载原文另登 `source-download`；「制作 HTML」进舞台草稿；Esc 逐级收菜单/舞台/多选；无 JS 报错 |

上面两轮的权限演练各暴露一个真实缺陷，都已修：数据卷首次挂载时 root 属主导致
runner 建不出 `ready/`；runner 建的 `out/screenshots` 是 0755，服务端 worker
删不掉，成品回收会卡在 `PermissionError`。当时的解法是把交接目录设成 0777 并把
runner 进程 `umask` 置 0——机器上任何本地进程都能读写别人的任务信封，等于把用户
之间的隔离让给了权限（docs/22 C-02）。现在换成两端固定同一个共享组：两个镜像都建
`kbshare`（gid 950）、compose 给 `share_worker` 与 `share_runner` 都 `group_add`，
目录 2770 带 setgid、runner `umask 002`，对端按组可读写可删，不再对任意进程放开。
另外交接目录里 `working/<task>` 的输入此前只靠 TTL 兜底，现在采纳结果或判定
超时就随手回收。

## 3. 尚未验证与已知限制

- **真实模型的多轮缓存命中（A39）未验证**：本轮没有用授权 Key 发起付费调用，
  只完成请求装配与 usage 归一化的模拟测试。上线前需按实际 endpoint＋model 跑
  首轮 + 至少两轮，记录真实 usage；未命中就标「尚未验证」，不用模拟结果代替。
- 生产分享域名尚未确定：`SHARE_PUBLIC_BASE_URL` 为空时「生成分享链接」返回
  明确的配置未完成原因，私有预览与下载可用。Caddy 片段里的域名是示例，
  必须换成与账号站不同可注册域名后才可用。
- 2 核 2GB 上的 runner 峰值：上表 226MiB 是**沙箱关着**时测的；开沙箱后最小样本就把
  512MiB 上限顶满（详见 §5），已抬到 768MiB。**与 ASR 等重型任务同时运行**时的表现、
  以及 §14.2 的跨任务重型槽位互斥仍未实测：当前只做了 `SHARE_RENDER_CONCURRENCY`
  与串行 runner，分享侧起浏览器前不查主机资源。
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
该单元已按 45min 装到服务器（`/etc/systemd/system/kb-auto-deploy.service`）。

还有两个只在真机上才暴露的问题（都已修）：

1. compose 里给 `share_runner` 又写了一遍 `node src/cli.mjs`，叠加镜像的
   `ENTRYPOINT` 后参数错位，容器以「用法」分支退出码 2 反复重启；改成只传参数。
2. 交接卷首次挂载时是 root 属主，非 root 的 runner 连 `ready/` 都建不出来；
   而 worker 与 runner 是两个不同 uid 的容器，任务目录和 runner 的产物目录
   （`out/screenshots` 等）必须对端可写、可删。当时预建 `/spool` 目录树并按
   0777 ＋ `umask 0` 放开，已按 docs/22 C-02 换成两端同组（见下面第 5 节）。

当前按「主要下载 HTML 文件」使用：`SHARE_ENABLED=true` 已开，分享站点仍未配置，
点「生成分享链接」会得到明确原因，私有预览与下载不受影响。

启用沙箱那次要在部署机做的一次性动作（结论来自 §5 的实测，compose 已经改好）：

1. 交接卷的组权限收口。现有卷是 0777、组是 appuser 的 gid 1000，靠的是「谁都能写」；
   换成按组收口后要让老数据落到 `kbshare` 上，否则两端会各自卡在对方建的目录外。
   没有在跑的任务时执行（只影响交接目录，对象与数据库都在 `data` 卷里）：

   ```bash
   sudo chgrp -R 950 /var/lib/docker/volumes/deploy_share_spool/_data
   sudo chmod -R 2770 /var/lib/docker/volumes/deploy_share_spool/_data
   ```

   想更干净也可以直接把这个卷删掉重建（它只放 24 小时内回收的临时交接文件，没有长期
   数据）：先 `docker compose stop share_worker share_runner`，再
   `docker volume rm deploy_share_spool`，最后 `docker compose up -d` 让 Docker 按镜像里
   `pwuser:kbshare 2770` 重新初始化。**别用 `docker compose down --volumes`**，那会连
   `data` 卷（数据库与对象）一起删。
   （2026-09-21 部署当晚已按第一条执行，收口后是 `2770 1001:950`。）
2. 部署后复验沙箱：`sudo docker compose -f ~/kb-inbox/deploy/docker-compose.yml exec
   share_runner node src/cli.mjs doctor` 要报 `"sandbox_enabled": true` 且退出码 0；
   顺手 `docker stats --no-stream` 看一眼开沙箱后的内存峰值离限额还有多少余量。
   （2026-09-21 已验：doctor 通过，峰值顶满过 512m，已据此抬到 768m，见 §5。）
3. 换到别的云/目录时，`deploy/docker-compose.yml` 里 seccomp 那条走的是
   `${KB_REPO_DIR:-/home/ubuntu/kb-inbox}`，在新机器 `deploy/.env` 里设 `KB_REPO_DIR`
   指到仓库根即可，其余不用动。

仍待做：
1. 用授权模型配置完成 §6.5.6 的真实多轮缓存命中验证（A39）与 §16 针对性验收。
2. runner 单次检查的内存峰值已记录（上表 226MiB），还差**与 ASR 等重型任务同时
   运行**时的表现，据此再决定 `mem_limit` 与是否升到 4GB。
3. 需要公开分享链接时才配独立可注册域名、DNS 与 Caddy 片段；只用「下载 HTML」
   不需要这一步，未配置时点「分享」会得到明确的「还没有配置独立的分享站点」提示，
   私有预览与下载不受影响。
4. 四类真实领域材料（专业概念整合、中医比较、数学讲解、跨领域）的人工内容复核。

回滚只关新任务与新发布入口；已发布作品可继续由只读分享路径服务，
不删除已保存作品、来源快照，也不立即 downgrade 数据表。

## 5. 2026-09-21 审查修复（docs/22）

按 docs/22 的「三批文件互不重叠 ＋ 两处串行」分工落地，条目编号沿用那份报告。

容器侧（`apps/share-renderer/`）：

- **C-01** `launchChromium()` 显式 `chromiumSandbox: true`。Playwright 的默认值是
  `--no-sandbox`，之前注释与镜像声明里的「沙箱保持开启」不成立。`doctor` 现在报
  `sandbox_enabled`，不为 true 就按失败退出。浏览器仍走 `--remote-debugging-pipe`，
  没有为了拿进程句柄改 `launchServer + connect`——那要在 127.0.0.1 开 WebSocket，
  而 runner 是 `network_mode: "none"`；墙钟到点改用限时 `close()` 收浏览器。
- **C-03** 外层 `iframe title` 改用 `escapeAttr`，补一条标题含引号与尖括号的用例
  （把外层改回 `escapeHtml` 这条会红）。
- **C-09** `processTask` 外套整任务墙钟（信封 `check.task_timeout_ms`，兜底 120s），
  结果只有一方落盘；`runSpool` 循环体加 try/catch，坏信封也记失败继续下一个。
- **C-10** 交互断言目标在动作前后都命中不到时判 `ASSERTION_INVALID`（不再算通过），
  `count_equals 0` 仍合法；正文下限从 30 字改成按材料规模给的 `check.min_body_chars`。
- **C-14** runner 异常文本里的交接目录绝对路径统一换成 `<路径>` 并限长 240。

服务端（`apps/server/kbserver/`）：

- **C-04** 发给模型的素材目录统一用剥过 `storage_key` 的那一份；原表留在
  `checkpoint["asset_catalog"]` 里，交接 runner 还要靠它读对象。用例盯的是实际
  发出的请求文本，不是构造处。
- **C-05** 回收判断改用对的字段并补上真实回收分支（被取代且过 `SHARE_REVISION_RETENTION_DAYS`
  的版本、过了诊断保留期的旧任务产物），当前可用／已发布版本的产出链一起保住；
  迁移 `e9a3c5f7b2d4` 给 `share_artifacts.storage_key` 加索引。
- **C-06** 按「要么接线要么删掉」选了删：`share_max_supplement_rounds`、
  `share_context_compact_ratio` 与三个没有调用方的压缩判断函数删除，口径写回
  docs/20 §14.1。`list_messages` 不加条数上限（静默截断历史更糟）。
- **C-07** 删除条目改走 `cancel_asr()` 同一个入口，`running` 的 Job 也标掉；
  测试不再手改 `lease_until`，改成「租约还有效也能停」才算数。
- **C-08** 中间件写回续期 Cookie 之前再查一次撤销表；logout 不再依赖
  `current_principal`（中心不可达时那里先 503，用户点了退出其实什么都没清），
  改成本地先记撤销、先清 Cookie，再尽力通知中心，CSRF 与 Origin 校验照旧。
- **C-11** `/s/{token}` 与 `/preview/{token}` 先看 `SHARE_ENABLED`。
- **C-12** `DELETE /v1/shares/{id}` 的 `expected_version` 真的比对了；删除作品过了
  1 天宽限期后，run／会话／消息／版本／模型调用记录这些文本行由保留任务一起收掉。

前端（`web_static/`）：U-01 点亮态改由「行画完」的单一接缝补标记、发光收进
`body.share-selecting`；U-02 用现成的 `GET /v1/shares/{id}/link` 取真链接，没取到就
不渲染空锚点；U-03 舞台关了就不再续排轮询、也不在列表页弹无关提示；U-04 幂等键跟着
草稿走并统一 `crypto.randomUUID`，`startGeneration` 上同一把发送锁；U-05 勾选态下
回车／空格是勾选，Esc 记号约定补上「菜单与舞台」和详情页「更多操作」两层，切题后
恢复焦点，「已选 N 篇」带 `aria-live`。

交接（**C-02**，跨两端所以单独做）：两个镜像都建 `kbshare`（gid 950）并把各自用户
加进去，compose 给 `share_worker` 与 `share_runner` 都 `group_add: ["950"]`，服务端
建目录 `chmod 2770`、runner 建目录同样 2770 且 `umask 002`；0777 与 `umask 0` 都去掉了。

| 本轮跑过的检查 | 结果 |
| --- | --- |
| `python -m pytest tests -q`（服务端全量） | 384 passed |
| `npm test`（runner） | 22 passed（原 17 ＋ 新增 5 条：外层属性注入、无效断言、正文下限、墙钟、坏信封） |
| `node src/cli.mjs doctor`（本机 Windows） | `browser: ok`、`sandbox_enabled: true`，真实沙箱下走完构建与检查 |
| `ruff check --select F,E9` 本轮改动文件 | 无新增告警（余下均为既有） |
| `node --input-type=module --check` 改动的 js | 4 个文件解析通过（不等于运行时验证） |

**部署机实测结果（2026-09-21 当日，用已在生产机上的镜像起一次性容器，参数与线上 `share_runner` 完全一致；没碰部署目录、数据卷和任何在跑的服务）**：

- share profile 线上**是开着的**：`deploy-share_worker`、`deploy-share_runner` 都在 Up，runner 至今没有任务流量日志——也就是说那句「生产已实测沙箱开启」从来没真跑到过。
- 生产同款参数 + 显式开沙箱 → **起不来**：`Chromium sandboxing failed!`。同一容器不开沙箱是对照组正常（Chromium 148，构建与渲染都过）。
- 挡住的是三道，逐条测出来的：① Playwright 官方镜像里**根本没有 `chrome-sandbox` 这个 setuid helper**（`/ms-playwright` 下只有 `chrome` 与 `chrome-headless-shell`），SUID 那条路直接不存在；② 非特权 user namespace 被**宿主**挡住——Ubuntu 24.04 的 `kernel.apparmor_restrict_unprivileged_userns=1`，在宿主机上以普通用户 `unshare -Urm` 同样失败（root 可以），所以不是 Docker 或 seccomp 的问题；③ 只加 `CAP_SYS_ADMIN` 而沿用 Docker 默认 seccomp 表也不行（默认表把 `chroot`/`mount` 按 capability 挡在外面）。
- 目前**唯一实测通过**的组合：`cap_add: SYS_ADMIN` ＋ 收紧版 seccomp profile（`deploy/seccomp-share-runner.json`：逐条照抄 Docker 29 默认表，只在最前面加三条——`clone`／`unshare` 带 CLONE_NEWUSER 才放行、`chroot/mount/umount2/pivot_root` 放行；不是 `seccomp=unconfined`），沙箱下真实渲染与干净收尾都验过。
- 不给 SYS_ADMIN 的两条替代路（都还没验）：改宿主 sysctl 关掉那道缓解（全机生效，这台还跑着另两个站点），或只给这一个容器写一份带 `userns,` 的 AppArmor profile（宿主上没有现成的 docker profile 源可照抄，得手写）。
- `share_spool` 卷在宿主上确认是 `0777`、五个状态目录属主 `1001:1001`（pwuser 私有）——**这条不是洁癖而是必修**：新的 2770 模型下 `ready/` 若是 pwuser 私有，share_worker 根本写不进任务，而它自己的 `_spool_dir()` 又改不动别人建的目录（chmod 抛 PermissionError 被吞掉）。所以那份一次性 `chgrp -R 950` ＋ `chmod -R 2770` 已在部署机上执行，收口后是 `2770 1001:950`，两端各自建/删都验过。

**已部署（2026-09-21 23:55，`DEPLOY OK: 6b249ee`，health=200）之后在正式容器里复验**：

- `docker exec deploy-share_runner-1 node src/cli.mjs doctor` → `browser: ok`、**`sandbox_enabled: true`**、退出码 0；`docker inspect` 确认 `capadd=[CAP_SYS_ADMIN]`、`groupadd=[950]`、seccomp 用的是那份收紧表。`/inbox` 200、未登录 `/v1/auth/me` 401（不是 500）。分享站点域名仍未配，所以 C-11 的公网那条只能靠单测覆盖。
- 沙箱下的真实开销（跑 `check --task samples/rich`，只写容器自己的 `/tmp`，不进交接卷、不调模型）：结论 `ok: true`、诊断 0 条、四张截图；**cgroup `memory.peak` 正好顶到当时 512m 的上限**，最狠的一秒是 `anon 192MiB ＋ file 299MiB`——大头是可回收的文件缓存所以没被 OOM，但最小样本就用满了，真实大页面（接近 10MiB 单文件、长页面）会把浏览器打爆。据此把 `share_runner` 抬到 **768m**；`pids_limit: 128` 不用动，沙箱下一次检查的进程数峰值只有 18。
- 仍未测：真实大页面的内存与耗时、以及**与 ASR 同时跑**时的表现（docs/20 §14.2 要的共享重型准入目前只做了 ASR 单方面让路，分享侧不查资源就起浏览器）。

剩下只能真机走查的：U-01／U-02／U-04／U-05 与 U-06 的清单（口径见 §2 那两行）。

按报告口径本轮未动：C-13（部署失败分支不清理、`used_image_ids` 的空格匹配永不命中、
`share_runner` 不按提交号打标签）、C-15（模型档位选择不看用途）、U-06（窄屏长标题下
✕ 挤位、「完成」菜单打开后不重算位置），以及 🔵 低项（`tests/isolation.test.mjs`
自行 `launch`、`read_artifact` 等死代码、空闲门禁 70 秒均值口径），随下一次相关改动带走。
