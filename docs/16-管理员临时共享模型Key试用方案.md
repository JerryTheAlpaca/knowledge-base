# 管理员按用户代配模型 Key 与 B 站登录态方案

版本：草案 v2 · 2026-09-12。

状态：待评审，未实施。**取代 v1 的「服务端代理授权」方向**——产品决定改为管理员直接为每个试用用户配置可用凭据，并允许在本地 Obsidian 复用 LLM Key。

## 1. 问题与目标

产品给朋友试用时，对方往往不会申请 API Key、也不会填 endpoint / SESSDATA。希望管理员在「管理」页**按用户**把可用凭据配好，对方打开收件箱或 Obsidian 就能用。

### 1.1 目标

| 目标 | 说明 |
| --- | --- |
| 云端加工可用 | 管理员代配 LLM Key 后，试用用户的 enrich 无需自行填 Key。 |
| B 站字幕可用 | 管理员可代配该用户的 B 站登录态（SESSDATA），需要登录的视频走现有托管路径。 |
| 本地 Obsidian 可用 | 代配的 **LLM Key** 可经现有「本机绑定」下发到试用用户的插件，用于本地整理；插件流程不改。 |
| 可管可收 | 管理页能看每人是否已配置、版本与最近检测；可更新或撤销（服务端侧）。 |

### 1.2 非目标

- 不恢复费用统计与预算账本（docs/05）。
- 不把 B 站 SESSDATA 下发到插件（见 §3.4）。
- 不实现供应商侧子账号/虚拟 Key 的自动开通（运营上仍建议人工去供应商开独立 Key）。
- 不做「共享同一密钥池 + 调用次数扣减」的授权表（v1 方案 C，本版放弃）。

### 1.3 与 v1 的关系

v1 坚持「Key 永不离开服务器」，因此否决了「写入试用用户名下」。  
本版接受产品约束：**管理员代配 = 写入该用户自己的凭据表**；LLM Key 在用户于插件中绑定后会落到其本机。这是有意的取舍，安全责任写在 §4，不在实现里假装还能远程擦除已下载副本。

## 2. 方案选择

| 方案 | 做法 | 结论 |
| --- | --- | --- |
| A. 代理授权（v1） | 授权表 + Worker 兜底解密捐赠方 Key | 放弃：无法满足「本地 Obsidian 使用」。 |
| B. 管理员代写入目标用户凭据（本方案） | 管理接口按 `user_id` 写 ProviderProfile + Credential | **采用**。复用现有加密、Worker、本地绑定。 |
| C. 发明文让试用用户自己填 | 微信发 Key | 否决：更易泄露且不可审计。 |

实现要点：加密 AAD 始终绑定**目标用户**（`grantee` 的 `user_id`、`profile_id`、`credential_version`），因此 enrich / B 站提取 / 本地绑定的现有解密路径**零改动**即可工作。

## 3. 核心设计

### 3.1 一句话

管理员在管理页选择一个本地用户，把 LLM 配置（endpoint / model / secret）和/或 B 站 SESSDATA **写入该用户的 `provider_profiles` + `credentials`**；写入后与用户本人配置完全同构——云端能用，LLM 还能走插件本机绑定。

### 3.2 不新增表

不引入 `credential_grants` 或其他「借还」模型。沿用：

- `ProviderProfile.kind=llm` + `Credential`：模型 Key  
- `ProviderProfile.kind=bilibili_session` + `Credential`：SESSDATA（与 `routes_bilibili.py` 相同）

可在 `ProviderProfile.meta_json` 增加非敏感审计字段，例如：

```json
{
  "provisioned_by": "<admin_central_user_id>",
  "provisioned_at": "2026-09-12T10:00:00Z",
  "source": "admin_panel"
}
```

便于回看「是谁代配的」，不参与鉴权，不含密钥。

### 3.3 管理接口（均 `require_admin`）

挂在 `routes_admin.py`（或独立小模块由其挂载），路径前缀 `/v1/admin`。

| 方法与路径 | 作用 | 要点 |
| --- | --- | --- |
| `GET /v1/admin/users` | 用户列表 | 本地 user.id、name、auth_subject、status、是否已有 llm/bilibili 凭据、最近更新时间。**无密钥、无掩码。** |
| `GET /v1/admin/users/{user_id}/credentials` | 该用户凭据状态 | llm：configured、credential_version、endpoint 主机、model、updated_at；bilibili：configured、verification、last_check（脱敏）。复用用户侧 `_profile_out` / `_out` 的展示逻辑。 |
| `PUT /v1/admin/users/{user_id}/llm-credential` | 新建或更新 LLM Key | body：`endpoint`、`model`、`capabilities?`、`secret`。见 §3.3.1。 |
| `DELETE /v1/admin/users/{user_id}/llm-credential` | 撤销该用户 llm 凭据 | 撤销未 revoked 的 Credential；后续任务 `waiting_key`。 |
| `PUT /v1/admin/users/{user_id}/bilibili-session` | 新建或更新 SESSDATA | body：`secret`（允许粘贴 `SESSDATA=…` 或完整 Cookie，服务端只提取值）。复用 `_extract_sessdata`。 |
| `DELETE /v1/admin/users/{user_id}/bilibili-session` | 撤销该用户 B 站登录态 | 与用户侧 DELETE 语义一致。 |
| `POST /v1/admin/users/{user_id}/bilibili-session/test` | 可选：nav 检测 | 只验证、不重抓；写 `meta_json.bilibili_last_check`。 |

所有写接口：

- CSRF/Origin（中心会话通道已由 `current_principal` 覆盖）。
- 请求体中的 secret **只进加密，不进日志、不进事件 payload、不在响应中回显**。
- 响应只含配置元数据与 `credential_version`，与用户侧 ProfileOut 一致。
- 目标 `user_id` 不存在或 `status != active` → 404/422。
- 禁止把 secret 放进 URL 或 GET。

#### 3.3.1 LLM 写入语义

与 `routes_profiles.create_profile` / `update_profile` 对齐，但操作对象是 `path` 里的目标用户：

1. 校验 endpoint（HTTPS + `PROVIDER_ALLOWED_ORIGINS`）、model、capabilities（复用现有校验函数，避免两套规则）。
2. 若该用户尚无 `kind=llm` 的配置：创建一条（首版每人至多一条主 llm 配置；已有多条时更新「最新一条未删除/约定主配置」，并在列表展示多条时的选中规则写清楚——建议首版**只维护最新一条**，避免管理页复杂化）。
3. 有则更新 endpoint/model/capabilities（配置 version+1）并写入新 Credential（撤销旧 version，新 version 递增）。
4. 同一短事务：`waiting_key` 条目重新入队 enrich（复用 `_requeue_waiting`）；`emit_event(credentials_updated)` 给目标用户。
5. `meta_json` 写入 `provisioned_by` 等审计字段。

#### 3.3.2 B 站写入语义

与 `routes_bilibili.put_session` / `delete_session` 对齐，参数化 `user_id`：

1. `_extract_sessdata` 清洗。
2. 确保存在 `kind=bilibili_session` 的 profile（endpoint/model 固定常量）。
3. 新 Credential version，AAD 用目标用户 id。
4. `_requeue_needs_input`：仅重提 B 站来源且因字幕/登录态等待的条目。
5. 事件 `bilibili_session_updated`。

### 3.4 与本地 Obsidian 的关系

| 凭据 | 云端 | 本地插件 | 说明 |
| --- | --- | --- | --- |
| LLM Key | enrich 使用 | **可**经现有 `POST /v1/provider-profiles/{id}/local-binding` 下发 | 试用用户在插件设置里走已有「将此 Key 配置到本设备」；**插件代码无需改**。 |
| B 站 SESSDATA | 提取字幕 / ASR 会话使用 | **不下发** | `bind_local` 已限制 `kind != llm` → 422。管理页文案写明「仅云端使用」。 |

因此：

- 管理员代配 LLM 后，试用用户打开 Obsidian → 登录 → 模型设置里应看到 `configured` 的线上配置 → 点绑定 → Key 进本机 SecretStorage。
- 专用 scope `profiles:bind-local` 仍由用户在设备授权时勾选，不因为「管理员代配」而放宽。
- 服务端撤销 LLM 凭据或屏蔽绑定，**不能收回已经下载到试用用户电脑的副本**；彻底失效需在供应商处作废该 Key。管理页必须显示这句，不承诺远程擦除。

### 3.5 管理页 UI（`admin.html`）

新增卡片「用户凭据代配」：

1. **用户列表**：用户名、状态、「LLM」/「B站」badge（已配置/未配置）、入口「配置」。
2. **配置抽屉或子卡片**（选定用户后展开）：
   - LLM：endpoint、model、Key（password 型输入）、能力高级项可折叠；「保存并覆盖旧 Key」「撤销 LLM」。
   - B 站：SESSDATA 输入、「保存」「检测登录态」「撤销」。
   - 只读状态：credential_version、最近更新、B 站 last_check 脱敏结果。
3. **固定安全提示**（表单旁）：
   - 「保存后 Key 归属该试用用户账号；对方可在 Obsidian 绑定到本机，绑定后服务端无法收回其本机副本。」
   - 「请为每位试用用户使用单独申请、可单独作废的供应商 Key，不要共用你的日常生产 Key。」
   - 「B 站登录态仅在服务器提取字幕，不会下发到插件；同一 B 站号代配多人有风控与隐私风险，建议每人一号或接受风险。」
4. 保存成功 toast：「已写入用户 xxx；云端任务将自动继续。」  
5. 保存表单在成功后清空 secret 输入框。

样式复用 `tokens.css`，不新开视觉体系。

### 3.6 试用用户侧体验（不改插件）

1. 用邀请码注册并登录 Web 收件箱 → 正常投递内容 → 云端开始加工（因为已有代配 LLM）。
2. Obsidian 插件登录同一账号 → 模型设置显示线上配置已配置 → 按需绑定到本机做本地整理。
3. 若管理员撤销 LLM：云端新任务 `waiting_key`；本机已绑定副本仍在，直到用户删除或供应商作废。
4. 若管理员撤销 B 站登录态：后续提取回到匿名路径；需要登录的视频进入补充材料。

## 4. 安全设计（必读）

### 4.1 产品取舍（先说清楚）

本方案**不再**保证「试用用户永远拿不到 LLM Key」。  
一旦用户完成本机绑定，Key 明文在其设备上，与用户自填 Key 的风险模型相同。  
管理页与本文档必须如实展示，不能在 UI 上写「绝对安全 / 可远程收回」。

仍然保证的是：

| 仍保证 | 做法 |
| --- | --- |
| 传输与存储加密 | HTTPS + 信封加密；AAD 绑定目标用户。 |
| 只进不出（绑定前） | 普通 GET 不返回 secret；管理读接口同样不返回。 |
| 权限 | 代配接口仅 `is_admin`；目标用户数据仍按 user_id 隔离。 |
| 可撤销（服务端） | DELETE 后不再发起新云端调用；B 站不再带登录态。 |
| 可审计 | `meta_json.provisioned_by`、事件、管理操作本身可记简单日志（无 secret）。 |
| 不扩大 B 站面 | SESSDATA 不进插件、不进 Bundle、不进字幕 CDN 请求头（现有 safe_fetch 约定不变）。 |

### 4.2 威胁与对策

| 威胁 | 对策 |
| --- | --- |
| 非管理员调用代配 API | `require_admin`；403。 |
| 管理响应/日志泄露 Key | 响应模型去掉 secret；日志中间件不记 body；事件只含 id/version。 |
| 指到别的用户的 profile | 所有读写以 path 的 `user_id` 过滤；解密 AAD 用目标 user_id，不用请求方身份。 |
| 试用用户通过管理 API 读 Key | 管理 API 本身不返回 secret；本地绑定是用户自己的受控出口。 |
| 共用生产 LLM Key | UI 强提示 + 运营规范；系统无法技术阻止管理员粘贴同一把 Key 到多人——若粘贴，供应商侧无法区分盗用者。 |
| 共用同一 B 站 SESSDATA | 同上；额外风险：平台风控、他人视频请求特征、账号安全。建议独立小号。 |
| 绑定后撤销无效 | 文档与 UI 如实说明；供应商作废为最终手段。 |
| 代配后用户自己改 Key | 用户侧 PATCH 仍可用；`provisioned_by` 保留为历史审计，以最新 Credential 为准。 |
| 审计字段被当成鉴权 | 代码不读取 `provisioned_by` 做授权判断。 |
| secret 进前端缓存 | 表单不回填已保存 secret；输入框 `autocomplete=off`；成功后清空。 |
| CSRF | 沿用双提交 Cookie + Origin。 |

### 4.3 明确不做

1. 管理接口「查看/复制当前 Key」。  
2. 把 B 站 SESSDATA 绑定进插件。  
3. 恢复用量计费。  
4. 用管理员自己的会话去「代表用户」调用用户侧 profiles API（应使用专用 admin API，避免 CSRF/身份混淆与 scope 扩大）。  
5. 在对象存储或 Bundle 中附带任何凭据。

### 4.4 隐私与隔离

- 代配只改变「谁提供密钥材料」，不改变条目、事件、Bundle 的归属：全部仍属目标用户。  
- 管理页可以看到「是否配置、版本、B 站检测状态」，**不**应展示用户条目正文。  
- 多个试用用户若使用同一供应商 Key 或同一 B 站号，供应商侧会看到混合流量——这是运营选择，不是本系统串数据。

## 5. 行为细节

### 5.1 更新与撤销

| 操作 | 云端 in-flight | 新任务 | 本地已绑定副本 |
| --- | --- | --- | --- |
| 管理员更新 LLM Key | 已 sent 的调用可能仍按旧 Key 结束 | 用新 version | 下次配置同步可更新；需用户侧再绑定/同步逻辑（现有行为） |
| 管理员撤销 LLM | 不再新发 | `waiting_key` | 仍在，直到本机删除或供应商作废 |
| 管理员更新/撤销 B 站 | 在途提取可能仍结束 | 按新登录态或匿名 | 无本机副本 |

### 5.2 失败文案

沿用现有 `waiting_key` / `needs_input`，管理保存失败时 toast 显示 ApiError message，不打印 secret。

### 5.3 并发

与用户本人保存 Key 相同：短事务内撤销旧 Credential、插入新 Credential、重排队、事件；不持外网 HTTP 写库。

### 5.4 首版简化

- 每用户**一条**主 LLM 配置由管理页维护（最新未撤销）。若用户曾自行建过多条，管理页只显示/覆盖最近一条，并提示存在多条时以用户侧设置为准。  
- 不做「批量给所有试用用户粘贴同一 Key」的 UI（易误操作且鼓励共用 Key）；需要时逐个配置。  
- 不做到期自动收回（供应商 Key 有效期由供应商管）；若以后要「试用 14 天」，可加 `meta_json.trial_expires_at` 并由 Worker 拒绝过期代配配置——记为后续增强，不阻塞本版。

## 6. 实现切片

1. **管理 API**：users 列表、credentials 状态、llm/bilibili PUT/DELETE（复用校验与加密函数）。  
2. **管理页**：用户列表 + 代配表单 + 安全提示 + 撤销。  
3. **审计**：`meta_json.provisioned_*` + 事件；可选 access log 不含 body。  
4. **测试**：见 §7。  
5. **契约**：若维护 `contracts/openapi.json`，补 admin 路径。

预计有效开发日：1.5–2.5（含集成测试与 UI）。插件无需发版。

## 7. 验收场景

### 7.1 正向

1. 管理员为新用户代配 LLM → 该用户投递一条文字 → 云端 enrich 成功。  
2. 同用户代配有效 SESSDATA → 需登录的 B 站条目能取到字幕（或按现有实测样本验证）。  
3. 该用户在 Obsidian 对代配配置执行本机绑定 → 返回 secret 且本地整理可调用（现有 local-binding 测试语义）。  
4. 管理页列表显示已配置 badge 与 credential_version；用户侧 profile 列表 `configured=true`。

### 7.2 安全否定

1. 非管理员调用上述 admin API → 403。  
2. 任何管理 GET/PUT 响应体不含 secret 明文。  
3. 事件 payload、`meta_json` 不含 secret。  
4. 用用户 A 的 token 不能读写用户 B 的代配接口（admin 接口除外；admin 也只能按显式 user_id 操作）。  
5. 撤销 LLM 后新任务 `waiting_key`，且不能解密出旧明文。  
6. 尝试对 bilibili profile 做 local-binding → 422（回归）。  
7. 字幕 CDN 请求头不带 SESSDATA（回归现有安全测试）。

### 7.3 运营

1. 供应商侧作废某试用 Key 后，云端任务失败/等待，材料保留。  
2. 更新 Key 后 `waiting_key` 自动重排队。

## 8. 代码落点（实施时）

| 位置 | 改动 |
| --- | --- |
| `api/routes_admin.py`（或 `routes_admin_credentials.py`） | users + 代配 CRUD |
| `api/routes_profiles.py` | 抽出/复用 `_validate_endpoint`、`_validate_capabilities`、`_requeue_waiting`（避免复制粘贴两套规则） |
| `api/routes_bilibili.py` | 抽出 `_extract_sessdata`、profile 查找、写入/撤销/重排队为可传 `user_id` 的内部函数 |
| `web_static/admin.html` | 「用户凭据代配」卡片 |
| `tests/integration/` | admin 代配、隔离、无 secret 泄露、local-binding 回归 |
| `docs/02` §9 / 本文 | 实施后回填接口清单；本文为安全与产品边界权威说明 |

**不需要改**：`workers/enrich.py` 凭据解析、`workers/worker.py` B 站会话读取、Obsidian 插件绑定流程、加密模块。

## 9. 运营建议（写入管理页帮助文案）

1. **每位试用用户一把独立 LLM Key**，在供应商控制台可单独限流/作废。  
2. **B 站**：优先每人小号；若共用，接受风控与隐私风险，并在试用结束后轮换 SESSDATA。  
3. 试用结束：管理页撤销 + 供应商作废；提醒用户本机若已绑定需自行删除插件密钥。  
4. 不要用日常生产 Key 做代配。

## 10. 结论

采用 **管理员按用户代写入凭据（方案 B）**：

- LLM Key 与 B 站 SESSDATA 都写入目标用户自己的加密凭据表，复用现有 Worker 与提取链路。  
- LLM Key 允许经既有本机绑定在 Obsidian 使用；B 站登录态仅云端使用。  
- 安全上如实承认：本机绑定后无法远程收回 LLM 副本；靠独立供应商 Key、服务端撤销、审计与「只进不出（绑定前）」把风险收敛到可接受范围。  

实现前仅需确认：

1. 每用户首版只维护一条主 LLM 配置是否足够；  
2. 是否需要首版就带 B 站「检测登录态」按钮（建议要，避免代配了无效 SESSDATA）。
