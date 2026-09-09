# B 站 ASR 实施与验收记录

日期：2026-09-09。本文按 `docs/11-B站无字幕视频本地ASR可行性与实施方案.md` v2 第 9.4 节交付契约记录本轮实施结果。**本轮只完成本地实现与集成验证；模型/运行时未部署、真实音频流与真实模型质量未验证**，全部未验证项在 §5 如实列出。

## 1. 代码版本与范围

- 分支：`feat-bilibili-asr`（worktree），基于 `0437cff`（docs/11 v2 方案文档）。
- 本轮按用户决定：**只部署/下载主模型 Dolphin**；备用模型 SenseVoice 只保留代码路径（`retry_model=sense_voice`），不部署、不下载，质量复测时再按 docs/11 §8 第 8 条执行。

### 新增

| 文件 | 内容 |
| --- | --- |
| `apps/server/kbserver/extractors/bilibili_audio.py` | `resolve_audio_stream`（播放接口 `/x/player/wbi/playurl`（登录态 WBI 签名）/`/x/player/playurl`（匿名），`fnval=16` DASH，选最低带宽普通 AAC；Dolby/FLAC 不取；时长归属校验拒绝试看截断）+ `prepare_audio`（受限 HTTP → FFmpeg stdin 管道 → 16kHz/单声道/16-bit 短 WAV 段，`-f segment` 硬切 20s；背压限流；stderr 持续消费并脱敏；主地址失败只试同轨备选；总时长覆盖校验） |
| `apps/server/kbserver/workers/asr.py` | ASR 执行模块：`start_asr`（幂等入队）、`execute_prepare`（三段事务、原子提交清单）、`execute_transcribe`（一次领取识别一段、检查点推进、重新排队让出）、`_finish_if_complete`（合并、发布 ASR 来源版本、入 enrich、清理 PCM）、忙碌让出与失败退避 |
| `apps/server/kbserver/workers/idle.py` | 宿主机空闲准入：`/proc/stat`、`/proc/meminfo` 滑窗采样；`can_start`（60s 连续空闲窗口 + MemAvailable ≥ 800MiB + 无普通任务）、`check_running`（CPU>70% 持续 ~20s 或内存 <256MiB 两次采样 → 终止当前段）、`note_busy`（冷却 120s）；指标不可用保持排队不猜测 |
| `apps/server/kbserver/workers/publish.py` | 从 `worker._publish_segments_revision` 移出的共享发布路径（ASR 与字幕/网页提取共用，ASR 不反向导入 Worker 主循环） |
| `apps/server/kbserver/api/routes_asr.py` | `GET/PUT /v1/asr-settings`（用户级 `auto_when_no_track`，`deployment_enabled` 只读）、`POST /v1/items/{id}/asr`（只收模型别名、幂等、用户隔离）、`GET .../asr`（阶段/段数/原因，不暴露服务器路径）、`POST .../asr/cancel` |
| `apps/server/kbserver/models.py` + 迁移 `d6b8a2c4e9f7` | `asr_runs` 检查点表：唯一键 `(user,item,revision,recipe_hash)`、`next_chunk_index`、`manifest_json`、失败计数、受控工作目录 |
| `tests/fixtures/fake_ffmpeg.py`、`fake_sherpa.py`、`tests/integration/test_asr.py` | 假流/假 FFmpeg/假引擎的 15 个集成测试（§4） |
| `scripts/probe_bilibili_asr.py` | 端到端探测工具（真实音频流 + 真实引擎；输出脱敏 JSON 指标；不从命令行接 Cookie） |
| `docs/12`（本文） | 实施与验收记录 |

### 修改

| 文件 | 内容 |
| --- | --- |
| `security/safe_fetch.py` | 新增 `stream_to_sink`：逐块写 sink、字节上限、每跳重定向校验、取消回调、进度回调；不持有完整响应。原 `safe_fetch` 全文请求行为不变 |
| `workers/worker.py` | `claim_job` 按 stage 过滤（普通任务优先，ASR 不抢跑导致饥饿）；`run_once` 接收空闲门（无普通任务且整机空闲才领 ASR）；`_extract_bilibili` 确认 `no_track` 且部署+用户开关均开 → 自动入队 ASR（登录错误等其余原因不触发）；主循环周期恢复过期租约 |
| `config.py` | `ASR_*` 配置集中声明（§9.3 全部初值）；`get_settings()` 运行时读取环境变量 |
| `extractors/bilibili.py` | 无行为变化（`bilibili_audio` 复用其 WBI 签名、设备指纹、`_fetch_json`、`_api`、短链展开与分 P 解析） |
| `api/app.py`、`contracts/openapi.json` | 挂载 ASR 路由；OpenAPI 由应用重导（37 paths） |
| `deploy/docker-compose.yml` | worker `memswap_limit: 640m` + `cpus: "0.75"`；api `memswap_limit: 256m`；`ASR_*` 环境变量（默认 `ASR_ENABLED=false`）；模型只读挂载占位（部署时启用） |
| `apps/server/Dockerfile` | 安装 `ffmpeg`（管道解码依赖，随镜像固定分发） |
| `web_static/inbox.html` | 设置页「平台接入」卡加「无字幕时自动转写」开关（部署开关关闭时隐藏）；详情页管理签加「音频转写」折叠区（状态/进度/触发/取消） |
| `README.md`、`docs/02`、`docs/04` | 现状与行为说明同步（见各文档新增小节） |

## 2. 关键实现决定

- **模型清单固定**（docs/11 §2）：`dolphin` = `sherpa-onnx-dolphin-base-ctc-multi-lang-int8-2025-04-02`；`sense_voice` = `sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17`。一次 run 只绑定一个 `recipe_hash`（模型 + 切段 + 解码参数），切换模型产生新 run，输出不混合。
- **部署探测**：`model_available(alias)` 检查 `ASR_MODEL_DIR/<model_id>/` 下 `model.int8.onnx` 与 `tokens.txt` 都存在；未部署时 API 明确拒绝（422），不静默降级。
- **音频获取**：登录态走 WBI playurl（复用 2026-09-09 实测的字幕串号教训：WBI 端点 + buvid3 + SESSDATA 三件套）；匿名走普通 playurl。选 `dash.audio[]` 中 bandwidth 最低的普通轨（64k AAC 对 16kHz 单声道识别输入足够）；`dash.dolby`/`dash.flac` 不取。音频 CDN 下载带 UA+Referer、不带 Cookie。
- **归属校验**：接口 `dash.duration` 明显短于 view 分 P 时长（<0.9×）拒绝为试看截断；解码总时长与接口时长偏差 > max(2s, 2%) 拒绝提交（静默截断）。
- **检查点语义**：清单（含每段 sha256）先 `os.replace` 原子写磁盘，再短事务更新 `AsrRun`；段识别结果同样先临时文件后改名、再短事务推进 `next_chunk_index`。崩溃只损失未提交的段。已提交段摘要校验失败 → 终态失败（不静默续跑）。
- **让出语义**：忙碌让出/正常推进把 `job.attempt` 清零，不消耗失败额度（`failed_count` 单独计数，≥5 终态化进补充材料）。
- **租约**：准备与识别期间每 ~20s 按 `lease_token` 条件续租；续租失败/取消/接管 → 终止子进程（POSIX 杀整个进程组），不提交。
- **发布标识**：`source=asr`、`engine=sherpa-onnx`、完整模型 ID、`quantization=int8`、`acquisition=player_audio_stream`、`audio_retained=false`、`timestamp_kind=estimated`、`original_media_retained=false`；warnings 含「机器转写，未经人工校对」；segments `source="machine_asr"`、`origin="asr"`；空段区间记录为 `failed_ranges`（部分转写时 coverage=partial_text）；全片无有效语音进补充材料。
- **时间换算**：识别输入 = 前段尾部 1s 上下文 + 当前段；模型 token 时间 + 输入实际起点 = 视频绝对时间，按 core 区间裁剪重叠带；无 token 时间时退化为段级粗粒度，不伪造逐字时间。

## 3. 部署配置（未应用，占位）

`deploy/docker-compose.yml` 已写入配置但**生产尚未部署**：`ASR_ENABLED=false`（默认关）、模型只读挂载注释状态。上线前需按 docs/11 §8 第一步在服务器完成：模型包下载与 SHA-256 校验、`sherpa-onnx-offline` 固定版本放置 `/opt/sherpa/bin/`、`alembic upgrade head`、`ASR_ENABLED=true` + 挂载取消注释后重建容器。

## 4. 已完成的验证（本地集成，135 passed 全量）

`tests/integration/test_asr.py`（15 个用例，假流/假 FFmpeg/假引擎，不触网）：

1. 设置开关读写；`deployment_enabled` 默认 false 且不可经 API 打开。
2. 触发隔离：非 B 站条目 422；他人条目/不存在条目 404。
3. 触发幂等：重复 POST 同一 run、同一 prepare job。
4. 模型切换：`retry_model=sense_voice` 产生新 recipe/run，旧 run 不动。
5. `no_track` + 用户未开启 → needs_input，无 ASR run（登录错误等其余原因同样不触发）。
6. `no_track` + 用户开启 → 自动入队（requested_by=auto），条目不进 needs_input。
7. 端到端：prepare（2 段）→ 逐段转写 → 发布 → enrich 入队 → waiting_key；manifest 含 asr_raw.json / asr_manifest.json / transcript.srt / normalized.md；warnings 含机器转写标记；发布后 PCM 工作目录清理。
8. 已提交段被篡改 → 摘要校验失败终态化，不静默续跑。
9. 忙碌让出：paused(resource_busy)、未提交段不计进度、`attempt=0` 不消耗失败额度、恢复后从未提交段继续。
10. 段失败退避：retry_wait → 恢复后只重算失败段（引擎调用次数 3 = 段0 + 段1失败 + 段1重试）。
11. claim 按 stage 过滤：ASR job 更早到期也先领普通任务。
12. 普通任务运行中 → 空闲门拒绝启动 ASR。
13. 取消：run/job cancelled。
14. 转写完成前补充正文（revision+1）→ 旧 ASR 作废（cancelled），不覆盖新材料。
15. 引擎命令按 §2 固定：dolphin/sense_voice 参数互不套用、单线程。

既有 `test_m1/m2/m4/m4_web/web_inbox/reading_view/reconcile/digest` 全量回归通过（135 passed）。

## 5. 未验证事项（不可凭本地测试宣称）

以下按 docs/11 §8/§9.4 属于上线门槛，本轮**均未执行**：

1. **真实模型**：Dolphin INT8 未下载、`sherpa-onnx-offline` 运行时未构建/部署；真实识别质量（漏句、术语、数字、否定词、静音幻觉）无任何实测数据。
2. **真实音频流**：播放接口在目标服务器上的可用性（匿名/登录态、风控、码率档位）未实测；`resolve_audio_stream` 的字段路径以 yt-dlp 锁定版本为参考，未经生产样本核对。
3. **资源峰值**：Worker cgroup 全流程峰值 ≤550MiB 的验收线未测；`/proc` 空闲判定未在宿主机实测校准；单段 15 分钟超时与 RTF 均为工程初值。
4. **端到端探测**：`scripts/probe_bilibili_asr.py` 已交付但未跑过真实样本（工具 `--help` 与导入路径已验证）。
5. **备用模型**：SenseVoice 按用户决定未部署未下载；`retry_model` 路径仅经集成测试。
6. **生产迁移**：`d6b8a2c4e9f7` 未在生产库执行。
7. **长时间吞吐**：48 小时试点、空闲窗口吞吐、普通任务 P95 对比未进行。

## 6. 下一步（按 docs/11 §8 第一步）

1. 服务器放置模型：下载 Dolphin 包 → 校验 SHA-256 → 展开 `/opt/models/`；构建/下载固定版本 `sherpa-onnx-offline` → `/opt/sherpa/bin/`。
2. `git pull` → 重建镜像（含 ffmpeg）→ `alembic upgrade head` → 先保持 `ASR_ENABLED=false` 验证部署无损。
3. 用 `scripts/probe_bilibili_asr.py` 对 3–5 条确无字幕的常看视频（含多 P 的 P2）做真实探测：记录准备耗时、每段 RTF、cgroup 峰值、样本转写文本人工对照（开头/中间/结尾各 30–60s）。
4. 质量可接受 → 部署开关开启后真机走查 UI（自动入队、进度、让出、取消）；质量不达标 → 按 §8 第 6 条用 SenseVoice 同样本复测（届时再部署备用模型）。
