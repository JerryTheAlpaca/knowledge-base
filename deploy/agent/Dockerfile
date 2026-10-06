# agent 编排容器（docs/27 §Phase 3a）
#
# 与 A 机同栈：python:3.12-slim + FastAPI，和 api/worker 用**同一个基础镜像**，
# 层按内容去重共享 —— 20GB 余量的机器上多一份独立基础镜像就是几百 MB。
# dsh 的 Python SDK 自带单文件 Node 运行时（`deepseek-harness-runtime-bin`，实测解包
# 267 MiB，其中 262 MiB 是那一个可执行文件），**容器里不装 Node、不装 npm 全树**。
#
# 这个镜像里没有、也不该有：模型 Key、master_key、SQLite、Docker socket。
FROM python:3.12-slim

# setpriv 来自 util-linux：dsh-drop 用它把运行时降到该用户自己的 uid 上
# ca-certificates 是访问 A 机 HTTPS 接口的最小依赖
RUN apt-get update \
    && apt-get install -y --no-install-recommends util-linux ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/agent

# 精确锁版本：上游是 rc 线（SDK 0.1.5rc1 / CLI 0.2.0-rc.2），README 明写会有破坏
# 兼容性的变更。制品的 SHA-256 由部署时在机器上记进 /opt/agent/MANIFEST-sha256.txt
# （照 /opt/models/MANIFEST-sha256.txt 的既有做法），升级是一次显式动作，
# 不是构建时顺手飘上去。
COPY requirements.txt ./
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

COPY orchestrator/ ./orchestrator/
COPY cordis.patch.yml.tmpl dsh-drop ./

RUN chmod 755 /srv/agent/dsh-drop \
    && useradd --uid 9000 --create-home --shell /usr/sbin/nologin agentd \
    && mkdir -p /srv/agent-state /srv/agent-homes \
    && chown root:root /srv/agent-state /srv/agent-homes \
    && chmod 755 /srv/agent-state /srv/agent-homes
# 这两个目录**不能**chown 给 agentd：服务以 root 起（要给每个用户 setpriv 降权），
# 但 cap_drop ALL 把 DAC_OVERRIDE 一起拿掉了 —— root 写不进别人属主的 755 目录，
# 结果就是 SQLite「unable to open database file」崩溃重启（2026-10-06 线上实测）。
# 每用户家的属主由 orchestrator/homes.py 在创建时按 uid 逐个 chown。

# 编排服务要能给每个用户降权起子进程，必须 root + 精确的 capability 子集；
# 这是相对 share_runner 那套（SYS_ADMIN + 自定义 seccomp）**刻意不同**的一处：
# 这里不跑浏览器，不需要 Chromium 沙箱那套特权。
USER root

ENV PYTHONUNBUFFERED=1 \
    AGENT_PORT=8100 \
    AGENT_DB=/srv/agent-state/agent.db \
    AGENT_HOMES_ROOT=/srv/agent-homes \
    AGENT_RELAY_KEY_FILE=/run/secrets/agent_relay_key

EXPOSE 8100

# 容器内绑 0.0.0.0 是必须的（同网络的 api 要连得进来）；不暴露靠的是 compose 的
# internal 网络 + 不发布宿主端口，边界实测见 deploy/agent/README.md
CMD ["python", "-c", "from orchestrator.main import run; run()"]
