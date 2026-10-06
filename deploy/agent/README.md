# agent 容器部署（docs/27 §Phase 3）

知识库网页的 agent 后端。**默认不启用**：服务在 `profiles: ["agent"]` 下，
不在服务器 `deploy/.env` 里显式打开就完全不参与构建与启动，现有部署不受影响。

## 启用步骤

1. **导出一份 master_key 派生的中继密钥**（只在 api/worker 容器里能跑，因为它读 master_key）：

   ```sh
   sudo docker compose -f deploy/docker-compose.yml run --rm api \
     python -m kbserver.cli agent-relay-key \
     --write-to deploy/secrets/agent_relay_key
   sudo chmod 600 deploy/secrets/agent_relay_key
   ```

   写进去的是 `purpose_key(master_key, "agent-relay-v1")` 那 32 字节，**不是 master_key**：
   它能签/验服务间中继凭据，解不开任何模型凭据信封。

2. **开 profile 与开关**（服务器上的 `deploy/.env`，compose 就读这个目录下的文件）：

   ```sh
   COMPOSE_PROFILES=share,agent
   AGENT_ENABLED=true
   ```

   `AGENT_BASE_URL` 默认就是 `http://agent:8100`（api 在容器网内找到编排服务），
   搬 B 机时才需要改成公网域名。`AGENT_ENABLED` 与 `SHARE_ENABLED` 是分开的：
   agent 容器挂了只关对话入口，不影响 HTML 分享。要临时关掉对话把
   `AGENT_ENABLED=false` 再 `up -d api`，profile 可以留着不动。

3. **构建并起容器**：

   ```sh
   sudo docker compose -f deploy/docker-compose.yml build agent
   sudo docker compose -f deploy/docker-compose.yml up -d
   ```

   第一次构建会拉 `deepseek-harness-runtime-bin` 的 manylinux wheel（下载 77 MiB，
   解包 267 MiB，其中 262 MiB 是那一个自带 Node 的单文件运行时）。
   **容器里不装 Node、不装 npm**。

4. **`$DSH_HOME` 配额要硬限**（`AGENT_HOME_QUOTA_MIB=512`/人）。周期 `du` 发现时
   盘已经写满了，所以必须用 project quota：

   ```sh
   sudo mkfs.xfs -f -i size=512 -n size=8192 /dev/vdb      # 独立数据盘（示例）
   sudo mount -o uquota,gquota /dev/vdb /srv/agent-homes-mount
   sudo xfs_quota -x -c 'limit -u bhard=512m <agentd-uid>' /srv/agent-homes-mount
   ```

   卷 `agent_homes` 挂到 `/srv/agent-homes`。按 uid 设限正好落在「每用户不同 uid」上
   （`orchestrator/homes.py` 的 `user_slot`），不需要额外记账。
   超配额时前端提示「导出后清理」，**服务不静默删用户的历史**。

## 边界（验收时逐条实测，不接受「读文档宣布锁住了」）

容器里应该**看不到**这些东西：

```sh
sudo docker exec <agent 容器> sh -c 'ls /data; ls /run/secrets/master_key; ls /var/run/docker.sock'
# 三条都该是 No such file or directory
```

- 不挂 `data:/data`、不给 `/run/secrets/master_key`、不给 Docker socket；
- `read_only: true` + `tmpfs /tmp:size=64m` + `cap_drop ALL`（只加 CHOWN/SETUID/SETGID/FOWNER
  用于给用户降权）+ `no-new-privileges` + `pids_limit: 128` + `mem_limit: 512m`；
- 网络只有 `agent_internal`（`internal: true`）：没有网关与 NAT，容器到不了公网，
  也到不了 `169.254.169.254`。跨机部署改用 `nftables-agent.nft` 那份出向白名单；
- dsh 侧锁定见 `cordis.patch.yml.tmpl` 里的实测注释：模型可见工具清单应只剩
  `mcp__kb__*`；`@deepseek-ai/dsh-subprocess-local` 禁不掉（别人依赖它的服务），
  能禁的是工具层。

容器内验证（Phase 0b 的复现口径）：

```sh
curl -s -m 3 http://169.254.169.254/ ; echo "exit=$?"     # 该失败
curl -s -m 3 https://example.com ; echo "exit=$?"          # 该失败
curl -s -m 3 http://api:8000/v1/health ; echo "exit=$?"    # 该成功，这是唯一出路
```

再核对一次模型可见的工具清单（用假供应商跑一轮，看 `tools` 字段），
不要只读补丁文件就宣布锁住了 —— 上游插件改名时 patch 只会 warn 后跳过。

## 内存

Phase 0b 在本机（Windows x64、profile `sdk` + 锁定补丁）实测：dsh 子进程启动后
RSS 约 **186–214 MiB**，一轮对话峰值 **198–214 MiB**。Linux 容器里要用同一个补丁
重测一次，再据此定 `mem_limit` 与 `AGENT_ADMISSION_MIN_AVAILABLE_MIB`。

不设并发上限，按可用内存准入：低于 `AGENT_ADMISSION_MIN_AVAILABLE_MIB`（默认 384）
就排队，前端显示「服务器忙，已排队」。2GB 的 A 机上现实并发是 1，偶尔 2
（share_runner 渲染峰值实测 609MiB、ASR 要求 384MiB 空闲准入，都在抢同一份内存）。
`mem_limit` 的作用是把 OOM 关在 agent 容器里，不打死 api/worker/share_runner。

## 搬 B 机

只改部署，不改代码：把 `agent` 服务整块搬到 B 机的 compose，`KB_BASE_URL` 改成
`https://kb.jerrythealpaca.cn`，网络从 `internal` 换成宿主出向白名单
（`nftables-agent.nft`，只放 A 机 443），A 机侧不需要新增代码。
