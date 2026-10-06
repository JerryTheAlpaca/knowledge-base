# 27 · Agent 接入与 MCP 边界设计

> 编号说明：原计划写作 `docs/25`，但 25 已被《Obsidian 插件精简为纯同步》占用、
> 26 已被《插件下载分发》占用，本文顺延为 27。

## 1. 这一层要解决的问题

知识库网页此前那个「AI 对话」不是一条 agent 会话，而是一台写死的五阶段状态机
（`workers/share.py` 的 `preparing→clarifying→synthesizing→generating→packaging→
awaiting_runner`），问题全是预制的，用户没法自由对话。它同时还有一个真实产出：
可公开访问的 HTML 分享页。

本轮把两件事分开：

- **HTML 分享整条链路一行不动**（状态机、`_conversation`、`call_model`、
  `_cache_policy`、`_decrypt`、`share_runner` 沙箱），只是把入口换了个位置；
- **真正的 agent 对话**由新的三层提供：知识库自己的 MCP server、per-user LLM 代理、
  以及跑在独立容器里的 dsh 编排服务。

用户看到的动作只有一个：点顶栏三条横线 → 直接落到能自由对话的面板；agent 能查
他自己的条目、读原文、把整理结果投回他自己的收件箱；历史会话在面板内可切换、
长期保留。

## 2. 架构与五条不可破的边界

```
用户浏览器（知识库网页）
   │  既有中心会话 Cookie 或设备 Bearer
   ▼
宿主机 Caddy → 127.0.0.1:8000  api 容器
   ├─ /v1/*        现有接口（shares 全链路不动）
   ├─ /mcp         知识库 MCP server（新，agent token）
   ├─ /v1/llm/*    per-user LLM 代理（新，原始透传）
   ├─ /v1/agent/*  中继到 agent 容器（新，代表已认证用户）
   └─ /v1/agent/internal/*  容器回传事件与要会话 token（服务间凭据）
worker 容器：agent 任务分支（复用 claim / 租约恢复机制）
agent 容器（profiles: ["agent"]，每站点一个长驻）
   编排服务 FastAPI + 每用户一个 dsh 子进程
   $DSH_HOME = /srv/agent-homes/<site>/<user_id>/
SQLite 单写端 / 单一 data 卷 —— agent 容器绝不触碰
```

1. **agent 容器永不接触**：中心会话 Cookie、用户密码、模型 Key 明文、`data:/data`
   卷、`master_key`、SQLite。同机部署时这条靠「不挂载 + 只走 HTTP」成立。
2. **agent 容器永不写数据库**：所有写入经 api 容器的 HTTP 接口，保住 SQLite 单写端
   （ADR-001 里迁 PostgreSQL 的触发条件正是「多主机 Worker」，不能自己先造一个）。
3. **MCP 未注册的工具即不可调用**：改原文、删除、refetch、模型 Key、admin、同步回执
   一律不注册。不注册比运行时判断更硬。
4. **站点隔离是结构性的**：`$DSH_HOME` 路径、编排服务会话表、中继凭据三层都带 `site`，
   且 `site` 只从 A 机签发的凭据取、**永不从请求体取**。渲染出的 dsh 补丁里只有本站点
   那一个 MCP 条目 —— 那个进程里根本没有别站的连接。
5. **LLM 代理绝不走 `generate_conversation()`**：它不发 `tools` 字段，且在
   `content` 为空（模型这轮只想调用工具）时直接抛 `ProviderRetryable`。agent 循环
   的核心恰恰是 `tool_calls`。代理必须原始 HTTP 透传。

## 3. MCP 工具面

`apps/server/kbserver/api/mcp_server.py`，挂在现有 api 容器的 `/mcp`。
落点选 api 容器而不是新起一个：无状态短请求、不做模型调用，256m 限额够；
新容器换不到隔离收益（同一 SQLite、同一 data 卷、同一 master_key）。

| 工具 | 参数 | 复用的实现 |
| --- | --- | --- |
| `kb_list_items` | `state?/view?/source_type?/limit≤50/offset` | `routes_items._list_items_impl`（从 HTTP 路由抽出） |
| `kb_search_items` | `query/limit≤50` | 同一实现体的 `search` 分支。**不新建全文索引** |
| `kb_get_item` | `item_id` | `_require_item` + `_item_out` |
| `kb_read_source` | `item_id/kind=normalized\|readable/max_chars≤60000`（默认 20000） | `_source_material` |
| `kb_read_digest` | `item_id/max_chars` | `_cloud_digest` |
| `kb_capture_note` | `text/title?/user_note?/original_url?/client_capture_id?/idempotency_key?` | `routes_captures.create_capture_impl` |
| `kb_draft_note` | `title/markdown/source_item_ids?/idempotency_key?` | 同上，另附真实来源清单 |
| `kb_submit_job` | `kind/item_id?/params?/idempotency_key?` | `domain/agent_tasks.submit_agent_task` |
| `kb_get_job` | `job_id` | 读 `AgentTask` |

**明确不注册**：`POST /v1/items/{id}/source-text`（改原文）、`DELETE /v1/items/{id}`、
`POST /v1/items/{id}/refetch`、`routes_profiles.py`（模型 Key）、`routes_admin.py`、
`routes_sync.py` 的回执。`tests/integration/test_agent_mcp.py` 里对每一项都有断言。

几条实现约定：

- **60 秒工具超时**：读类工具全部远小于它；凡要等模型的走 `kb_submit_job` +
  `kb_get_job` 轮询，工具描述里写死「间隔 ≥5 秒、最多 20 次、别用同一个
  `idempotency_key` 重复提交」。
- **报错要把原因带出来**：MCP SDK 对普通异常只给一句 `Error executing tool X`，
  模型既不知道是 404 还是 403，也就无从判断该重试还是该回头问用户。
  `_surface_errors` 把 `ApiError` 换成 `ToolError("CODE: 中文说明")`。
- **读取一律限长并如实标 `truncated`**，返回带来源/投递版本号。被截断不能冒充读完了。
- 身份只从已验签的 `AccessToken.subject` 取；`kb_get_job` 读别人的句柄与不存在的
  句柄给同一个 404。

### 异步任务为什么单独一张表

`jobs` 按 `(user, item, source_revision, stage, recipe)` 唯一，且 `enqueue_stage`
**复用同一行**：用户同时点了「重新加工」，那一行的状态就被重置，agent 手里的句柄
就不再代表「我提交的那一次」。`AgentTask` 是这次提交的凭据，`job_id` 指向真正的
执行体；租约、恢复、`ProviderOperation` 的 `sent → unknown_outcome` 语义全部沿用
既有那套。**复用机制，不复用表。**

本轮两个 kind：`reprocess_item`（用已有材料重新整理）、`optimize_text`（只做纠错与
分段）。都不重新抓取、不改原文、不删除。

## 4. agent token 生命周期

`security/tokens.py` 新增 scope `mcp:read` / `mcp:write` 与 `Device.kind == "agent"`。

- **不复用设备 Token 语义**：撤销设备 Token 会连带掐掉 Obsidian 同步的回执通道
  （`routes_devices.py` 的 DELETE），agent token 的一键停用只影响 MCP 接入。
- 校验复用 `deps._load_device_principal`：显式 Bearer 无效直接拒绝、不回退浏览器
  Cookie，天然适合 MCP；`token_valid` 读 `revoked_at`，所以**停用后下一个请求即 401**，
  不存在 TTL 正缓存可等。
- TTL 默认 180 天（`AGENT_TOKEN_TTL_DAYS`）：dsh 侧 `headers` 是**启动时读一次的静态
  配置**（Phase 0b 实测确认没有刷新钩子），TTL 短了会让用户的客户端隔天就坏。
- 轮换由编排服务改写子进程环境完成，旧 token 留重叠宽限；一键停用在
  `POST /v1/agent-tokens/{id}/revoke`。
- Token 不进 URL、不进日志、不进异常栈；明文只在签发响应里出现一次。
- 界面：设置页「Agent 接入」签发 / 查看 / 撤销，显示 scope 与到期时间。

## 5. LLM 代理与会话 token

`api/routes_llm_proxy.py`：`POST /v1/llm/chat/completions`。

- `api` 形态选 `openai-completions`：`_chat_completions_url`、capabilities、
  `normalize_usage` 都在这条线上验证过。
- **透传换 Key，不重新装配请求**：删客户端 `model` → 强制解析出的 profile 的 model →
  按 capabilities 白名单重注私有缓存字段（复用 `_cache_fields`）→ 解密取 Key → 转发。
  其余字段（`tools`、`tool_choice`、`response_format`…）原样进出。
- **流式原样透传字节**（`aiter_raw` + `StreamingResponse("text/event-stream")`）：
  DeepSeek 的 `reasoning_content` 这类 wire 扩展自动保留，不需要维护会过期的字段清单；
  只在末帧抓 `usage` 记账。
- profile 选择复用 `domain/provider_select.pick_profile`（从 `workers/share.py` 抽出），
  网页对话与 agent 用**同一条规则**，否则同一个人在两处会用上不同的 Key。
- Key 只在单次 httpx 请求对象里持有，转发后释放；上游 401/403 归一成
  `PROVIDER_AUTH_FAILED`，不透传上游正文（里面常有账号 ID 与请求号）。
- 超时 `connect=15 / read=600 / write=30 / pool=10`；并发每用户 2 + 全局
  `AGENT_LLM_MAX_CONCURRENCY`（A 机验证期 2），超出 **503 + `Retry-After`**。
  释放必须幂等 —— 非流式路径先释放再记账，记账出错时外层会兜第二次释放。
- 记账用**自己的短会话**：`StreamingResponse` 的生成器在请求级依赖退出之后才跑，
  那时 `get_db` 的 Session 已经关了。

**会话 token**（`security/agent_llm_tokens.py`）：HMAC 自包含载荷
`{purpose, site, user_id, profile_id, exp, nonce}`，密钥 `purpose_key(master_key,
"llm-proxy-v1")` 域分隔，不与凭据主密钥、不与分享令牌混用。TTL 15 分钟。
**不放 Key、不放原文**。签名 token 无法主动撤销，所以「停用」是三重收口：TTL 到期、
`AgentBudget.exhausted_at`、`AGENT_ENABLED` 关掉整个入口。

**预算不串用（硬要求）**：`AgentBudget(user_id, profile_id, period=YYYY-MM-DD,
input/output_tokens_used, requests_used, exhausted_at)`。入口先查，超限
`429 BUDGET_EXHAUSTED` 且**根本不转发**（所以不产生供应商费用）；出口用
`normalize_usage` 归一后累加，缺失的计数不填 0（填 0 等于把「供应商没返回 usage」
伪装成「这次没花钱」）。`site/user_id/profile_id` 只从 token 载荷取，并校验
`ProviderProfile.user_id == token.user_id`；指向别人的配置直接 **403**，
自己的配置没了才是 422。

## 6. agent 容器边界与部署

`deploy/agent/`，服务在 `profiles: ["agent"]` 下，默认不启。加固抄 `share_runner`
已实测跑通的那套，两处刻意不同：

- **不给 `SYS_ADMIN`、不带自定义 seccomp** —— 那是 Chromium 沙箱要的，这里不跑浏览器；
- **不是 `network_mode: none`** —— 要访问 api 容器。改走 `internal: true` 的网络：
  没有网关、没有 NAT，容器到不了公网也到不了 `169.254.169.254`，只能碰到同一网络里的
  api。这比宿主 nftables 更干净，且随 compose 一起搬机器。跨机部署才用
  `deploy/agent/agent-egress.nft` 那份出向白名单。

其余：`read_only: true`、`tmpfs /tmp:size=64m`（tmpfs 是内存背书的，直接算进 RAM）、
`cap_drop: [ALL]` 后只加 `CHOWN/SETUID/SETGID/FOWNER`（给用户降权起子进程）、
`no-new-privileges`、`pids_limit: 128`、`mem_limit: 512m`、
独立小卷 `agent_state` / `agent_homes`，**不挂 `data:/data`**、
**不给 `/run/secrets/master_key`**、**不给 Docker socket**（docs/20 §7）。

服务间凭据的一个修正：`purpose_key(master_key, "agent-relay-v1")` 要两边都能算，
而容器拿不到 `master_key`。所以部署时用
`python -m kbserver.cli agent-relay-key` 导出**派生后的那 32 字节**，作为独立 secret
只挂进 agent 容器。它能签/验中继凭据，解不开任何凭据信封。

搬 B 机时只改部署：`agent` 服务整块搬过去，`KB_BASE_URL` 从 `http://api:8000` 改成
`https://kb.jerrythealpaca.cn`，网络换成出向白名单。**代码零改动** —— 这是 Phase 3
全部写成「容器 + HTTP 边界」的理由。

### $DSH_HOME

```
/srv/agent-homes/<site>/<user_id>/            权限 700，每用户不同 uid
  profiles/sdk/cordis.patch.yml               按站点渲染，只含本站点一个 MCP 条目
  sessions/                                    唯一会长大的东西（JSONL 历史）
/srv/agent-homes/<site>/<user_id>.workspace/   dsh 的 cwd，空的
```

限额 `AGENT_HOME_QUOTA_MIB=2048`/人**由软件实现**：起新一轮前量一次这个用户的
`$DSH_HOME`（`homes.disk_usage_mib`），超了就如实报错、不再让运行时往里写。
计划里原本要上 XFS project quota 按 uid 硬限，实测这台 A 机只有一块 ext4 系统盘、
没有独立挂载点，为此动磁盘不值当（2026-10-06 决定），所以真正兜住「别写满宿主盘」
的是整机可用空间闸门 `AGENT_MIN_FREE_DISK_MIB=1024` —— 低于这个点不起新轮次，
前端显示「服务器忙，已排队」。搬到有独立数据盘的机器上才值得再按 uid 配额。
清理：A 机侧事件按保留期回收；超限额**不静默删**用户的历史。

## 7. dsh 制品与锁定实测（2026-10-04，本机 Windows x64）

### 制品

`deepseek-harness-sdk==0.1.5rc1` + `deepseek-harness-runtime-bin==0.1.5rc1`。
Linux x64 wheel 下载 77 MiB、解包 **267.4 MiB**，其中单文件 Node 运行时
261.88 MiB、ripgrep 5.46 MiB。包内 README 原文：「The target machine needs no Node
installation」→ **服务器不装 Node、不装 npm 全树**。

这 267 MiB 是一份，所有用户共用；每用户独立的只有 `$DSH_HOME`。

上游自己写的边界（必须记在这里）：`SAFETY.zh.md` 明写「尚未接受安全审计，**不得视为
安全或可用于生产环境的软件**」「沙箱、审批提示与权限控制可以降低风险，但**不保证隔离**」
「不要把 DeepSeek Harness 当作不可信工作负载唯一的安全控制措施」。README 明写处于
开发者预览阶段，未来会有破坏兼容性的变更。

### 实测结论（六条，都影响代码）

1. **协议协商到 `2025-11-25`**。dsh 内置的 TS SDK 客户端 `LATEST_PROTOCOL_VERSION`
   是 `2025-11-25`，`SUPPORTED_PROTOCOL_VERSIONS` 含 `2025-06-18 / 2025-03-26 /
   2024-11-05 / 2024-10-07`。Python `mcp 2.3.0` 是 dual-era 服务，旧客户端的
   `initialize` 走 legacy 通道正常协商 → **不需要把 pyproject 降到 `mcp<2`**。
2. **`json_response=True` + `stateless_http=True` 被接受**（go/no-go 通过）。
   dsh 完成 `initialize` 后每个 POST 都带 `MCP-Protocol-Version` 头、不带
   `Mcp-Session-Id`，服务端回单个 JSON，工具 `tools/list` / `tools/call` 全部成功。
   「Streamable HTTP 重连归属未定」那条风险因此不成立。
   dsh 仍会开一条 `GET`（Accept: text/event-stream）的通知通道，无状态模式下它闲置无碍。
3. **锁定真实有效**。加补丁后模型可见工具清单实测收敛为：
   `['mcp__kb__kb_list_items']` 一个。bash / pwsh / 文件读写 / 搜索 / 网页抓取 /
   子 agent / workflow / skill / jobs / goal / ralph / todo 都不再出现。
4. **两个必须知道的坑**：
   - MCP 客户端**不是**全局 `mcp.servers` 映射，而是**每台服务器一条独立插件**
     `@deepseek-ai/dsh-mcp-client`，且 `sdk` profile 默认组合里根本没有它，
     要用根级 `- insert: [...]` 加进去（不带 `id` 的 insert 才是「追加条目」；
     带 `id` 的 insert 目标是 group，普通条目会报「is not a group」）。
     配置字段是平的：`serverName / transport / url / headers / toolCallTimeoutMs /
     failOnStartupError / reconnect.*`。
   - `@deepseek-ai/dsh-subprocess-local` **禁不掉**：`pwsh-sandbox` 等它的 `subprocess`
     服务、`permission-presets` 等 `shell` 服务，禁了整个 plugin tree 起不来。
     能禁的是「模型可用的工具」那一层。
   - `read-only` + `approval: never` 不匹配任何内置 preset（内置 `read-only` 配的
     是 `ask`）→ 必须**整块重述 `presets` 并显式 `defaultPreset`**，否则启动即失败。
     这正是「patch 替换整个配置块、不自动合并」的现实形状。
   - patch 未命中的条目只 warn 后跳过：上游插件改名会让某个「已经禁掉」的东西悄悄
     回来。所以验收口径是**核对模型看到的工具清单**，不是读补丁文件。
5. **撤销失败闭合**：`failOnStartupError: true` 时，agent token 被撤销后 dsh 运行时
   启动阶段就报错，agent 不会在「连不上自己的库」的情况下凭记忆继续答。
6. **健壮性**：让模型强制调用一个不在清单里的工具名（`bash`），运行时会直接退出
   （`TransportClosedError` / stdout closed）。所以编排层把「非零退出 / stdio EOF」
   当正常路径处理：标 broken、写 error 事件、下一轮重开，**不自动重试在途轮次**。

### 内存实测

Windows x64、`profile sdk` + 上述锁定补丁、单文件 Node 运行时：
启动后 RSS **186–214 MiB**，一轮带工具调用的对话峰值 **198–214 MiB**。

计划里按「禁用大部分插件后 150–300 MB」估的是对的量级。Linux 容器里必须用同一份
补丁重测一次（本机数字不能直接当容器数字），再据此定 `mem_limit` 与准入阈值。

`apiKeyEnv` 到底是启动时读一次还是每次请求读：**没有实测出结论**（子进程环境无法在
运行中改写）。设计上不依赖它：LLM 会话 token 通过 `api_key=` 在**起进程时**注入，
一个进程服务一个用户的一段会话；空闲回收（600s）早于 token TTL（900s），重启即换新。

## 8. 编排服务

Python 3.12 + FastAPI 单进程（与 api/worker 同一基础镜像，层按内容共享），
状态落**容器内本地 `agent.db`**（独立小卷）。

对外 API：`POST /agent/sessions`、`GET /agent/sessions`、`PUT/DELETE /agent/sessions/{id}`、
`POST /agent/sessions/{id}/messages` → 202 `{turn_id}`、`GET /agent/sessions/{id}/events?after=<seq>`（SSE）。

- **事件先落表、SSE 从表追**：`seq` 由编排服务单调分配，`uq(session_id, seq)`。
  用表不用内存队列：断线按 seq 续传、容器重启不丢、回传失败可按 `mirrored_seq` 重发。
- **只镜像用户可见事件**：`user_message / assistant_message / tool_call / tool_result /
  status / error / turn_end`。dsh 的内部记账类事件（`step/start`、`request/context`…）
  不进界面 —— 这条同时也是 docs/20 §3.5「不展示模型隐藏推理、不编造进度百分比」的落地。
- **内存准入**：起进程前查 `/proc/meminfo` 与 cgroup，低于
  `AGENT_ADMISSION_MIN_AVAILABLE_MIB`（默认 384）就排队并写一条 `status` 事件
  「服务器忙，已排队」，**不是静默失败也不是硬起**。回收默认 600s、每 60s 扫一次，
  **只在途轮次为零时才回收**。
- **进程模型**：每用户一个长驻子进程（不是每会话一个），按会话的 `dsh_session_id`
  跑 `harness.run(..., session_id=...)`。崩溃恢复：退出码非 0 或 stdio EOF →
  标 broken + 写 error 事件，下一轮重新拉起并试 `session/resume`；续不上就按新会话
  继续，历史照样读得到，**界面不假装「接着上次说完」**。容器重启：所有 `running`
  轮次标 `interrupted` 并补一条 error 事件。
- **降权**：走 SDK 公开的 `dsh_bin` 参数指向 `dsh-drop` 包装脚本（`setpriv --reuid`），
  不 monkeypatch SDK 内部的 `Popen`。

dsh 的 SDK 调用面全部收在 `orchestrator/dsh_runtime.py` 一个类里。上游是 rc 线，
破坏性变更预期由这一个文件吸收。

## 9. 前端：换入口

- `#sharesBtn` **位置、图标、尺寸完全不变**，只把 click 从 `shares.js:openConvList`
  改成打开新的对话面板（`?view=agent`），`aria-label` / `title` 改成对话语义。
- HTML 分享作品列表迁到顶栏紧挨着的新按钮，沿用现有 `openConvList` / `showConvList`
  与 `?view=shares` 路由；`#sharesView`、`#shareStage`、`js/shares.js` 其余部分
  一行不改。
- **feature flag 分开**：`AGENT_ENABLED` + `/v1/auth/me` 返回 `agent_enabled`，
  不复用 `SHARE_ENABLED`。agent 容器挂了只关对话入口。
- 对话面板沿用 `#shareStage` 验证过的视觉语言（同一套 scrim / dock / smartbox /
  过程块），但**不继承预制问卷那套**（方案顶部折叠、答过折起高亮、答完自动跳下一题）。
- 面板内自带会话列表（新建 / 切换 / 重命名 / 删除），按钮直接进当前或最近一次会话。
- 流式走 SSE；前端与 `/v1/agent/*` 同源 → 零 CORS；A 机 ↔ 容器是服务端到服务端。
  Caddy kb 站片段加 `flush_interval -1` 与 `transport http { read_timeout 620s }`。
- **UI 必须明示的两件事**：agent 会放大原文读取量（一次可能读几十条 vs 人工看一条），
  若用户的 Key 指向第三方中转站，这些数据即出境；以及准入排队时的「服务器忙，已排队」。

## 10. 与既有链路的关系

- Obsidian 插件本轮**零改动**（`apps/obsidian-plugin/src` 搜 share/conversation/对话
  零命中）。
- HTML 分享链路**零改动**：状态机、快照、公开页、沙箱渲染、`share_retention`、
  四个 `tests/integration/test_share*.py` 全部照旧跑。
- `providers/llm.generate_conversation` **不动也不用**（见边界 5）。
- 记账仓库本轮不动。记账站点接 dsh 要单独设计：Next.js 16 + better-sqlite3、
  无 Bearer 机制、16 个写路由有 Origin 守卫，实现方式与知识库完全不同。

## 11. 风险（按严重度）

1. **【致命 · 无法缓解】** 上游明写「不得视为可用于生产环境的软件」，而目标是产品级。
   容器 + internal 网络 + `read-only` 权限 + 禁插件 + Key 不下发只能**压爆炸半径**。
   对所有用户开放前应搬到 B 机，把「不挂 data 卷、不给 master_key」从逻辑边界
   变成物理边界。A 机验证期额外承担：dsh 与 `master_key`、全库同机。
2. **【高】** 破坏性变更。缓解：精确锁版本 + 部署机记制品 SHA-256（照
   `/opt/models/MANIFEST-sha256.txt`）+ dsh 交互面收敛到一个类。
   **无法缓解**：跟着上游 rc 跑，升级窗口期功能不可用是必然，不能对用户承诺 SLA。
3. **【高】** A 机内存没有余量（限额已排满 2048m；share_runner 峰值实测 609MiB、
   ASR 要求 384MiB 空闲准入）。现实并发 1，偶尔 2。观测口径照 docs/02：
   「可用内存持续 <300MiB 或反复 OOM」才升 4GB。
4. **【中高】** 配置层锁定的可靠性：patch 未命中只 warn。缓解：OS 层容器 + internal
   网络是独立屏障；能做的坏事被限制在「用该用户的 MCP 权限读该用户数据 +
   往他自己收件箱投草稿」。**残留**：SAFETY 明说沙箱不保证隔离，配置层永远不能当
   唯一屏障。
5. **【中】** `toolCallTimeoutMs` 60s 与模型耐心。靠工具描述 + 幂等键压制，
   但模型行为不可保证。
6. **【中】** 换入口是本轮唯一的用户可见破坏性改动。老用户肌肉记忆是
   「三条横线 = 我的分享作品」。
7. **【中】** 用户原文进模型上下文的量被放大；Key 指第三方中转站即数据出境。

## 12. 本轮偏离计划之处（写明，别让后来者以为是原设计）

| 计划里写的 | 实际做的 | 为什么 |
| --- | --- | --- |
| 文档编号 `docs/27` | `docs/27` | 25/26 已被占用 |
| MCP 配置写成全局 `mcp.servers.kb` | 一条 `@deepseek-ai/dsh-mcp-client` 插件条目 + 根级 `insert` | Phase 0b 实测：全局 `mcp` 映射不存在，`sdk` profile 默认不含 MCP 客户端 |
| 禁 `subprocess` 等基础设施插件 | 只禁工具层，保留 `subprocess` / `shell-env` | 实测禁用会让 plugin tree 起不来 |
| `ProviderOperation` 加 `agent_task_id` | 不加，归属由 `AgentTask.job_id` 承载 | 加了没有真实使用方：agent 的执行体就是那条 Job，操作已按 `job_id` 归属 |
| `kb_draft_note` 用 `pipeline_state="draft_pending"` | 用 `capture_channel="agent_draft"` + 真实来源清单 | 新造一个 Web 工作流不认识的流水线状态，会给用户显示一个说不清的状态 |
| 容器基础镜像 Python 3.11 | `python:3.12-slim`，与 api/worker 同一基础镜像 | 层按内容共享，20GB 余量的机器上少一份基础镜像就是几百 MB |
| 宿主 nftables 出向白名单 | `internal: true` 网络；nftables 片段留在 `deploy/agent/agent-egress.nft` 供跨机部署用 | 同机场景 internal 比宿主防火墙更干净，且随 compose 一起搬机器 |
| A→agent 用 `purpose_key(master_key, …)` 且容器不给 master_key | 部署时导出**派生后的**密钥作为独立 secret | 原文两种写法互斥：容器要能验/签中继凭据就拿不到 master_key |

## 13. 验收清单

服务端与状态层已有自动化覆盖：

- `tests/integration/test_agent_mcp.py` —— 9 个工具、用户隔离、每个被禁能力一条负向
  断言、只读 token 写不进去、幂等投递、截断与版本、句柄幂等、`AGENT_ENABLED=false`
  时 `/mcp` 不挂载。
- `tests/integration/test_llm_proxy.py` —— `tool_calls` 原样透传、强制替换 model、
  流式透传 `reasoning_content`、usage 记账、跨用户 403、供应商 401 不带 Key、
  超限 429 不转发、并发 503 + `Retry-After`。
- `tests/integration/test_agent_relay.py` —— 两侧凭据互认、用途不可互换、回传幂等、
  不能写进别人的会话、别站凭据不认、token 选出的 profile 与网页对话一致、
  容器不可达时 503 而收件箱其余功能正常。
- `tests/integration/test_agent_orchestrator.py` —— seq 单调、事件白名单、准入排队
  不硬起、一用户一进程、在途轮次不自动重试、回收只在途为零、跨用户跨站点不可见、
  重启把在途轮次标 interrupted。

还需要在 A 机上手工实测（本机 Windows 给不出这些结论）：

- [ ] Linux 容器里 dsh 子进程真实 RSS → 定 `mem_limit` 与 `AGENT_ADMISSION_MIN_AVAILABLE_MIB`
- [ ] `docker exec` 进 agent 容器确认看不到 `/data`、`master_key`、Docker socket
- [ ] 容器内 `curl 169.254.169.254`、`curl https://example.com` 全部失败；只有 api 通
- [ ] share_runner 正在渲染（峰值 609MiB）时发起对话：应排队，而不是打死 api/worker
- [ ] 核对一次模型实际看到的工具清单（用假供应商跑一轮看 `tools` 字段）
- [ ] 两个用户并发，`$DSH_HOME` 互不可见（uid + 700）
- [ ] 断网 5 分钟后事件补传，`seq` 不重不漏；`kill -9` dsh 子进程后历史完整
- [ ] `tests/integration/test_share*.py` 四个文件零退化
