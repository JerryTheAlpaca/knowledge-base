#!/usr/bin/env bash
# KB 自动部署脚本（服务器端）
#
# 由 systemd timer（deploy/systemd/kb-auto-deploy.timer）每分钟调用一次：
#   1. fetch origin/main，无新提交则静默退出（服务不健康时仅记日志告警）
#   2. 有新提交：git pull --ff-only -> docker compose build -> alembic 迁移
#      -> up -d -> 健康检查（127.0.0.1:8000/health/ready 期望 HTTP 200）
#   3. 部署成功后 docker builder prune 清理构建缓存（保留最近 3GB）
#
# 安装（服务器上执行）：
#   sudo cp ~/kb-inbox/deploy/systemd/kb-auto-deploy.{service,timer} /etc/systemd/system/
#   sudo systemctl daemon-reload && sudo systemctl enable --now kb-auto-deploy.timer
# 手动触发一次：sudo systemctl start kb-auto-deploy.service
# 查看日志：tail -n 50 ~/kb-auto-deploy.log
#
# 注意：与本仓库其他部署脚本一致，仅在 ~/kb-inbox 内做 --ff-only 拉取，
#       不做任何 reset/checkout 破坏性操作；构建失败时旧容器保持运行。
set -u

REPO_DIR="/home/ubuntu/kb-inbox"
LOG_FILE="/home/ubuntu/kb-auto-deploy.log"
HEALTH_URL="http://127.0.0.1:8000/health/ready"

log() { echo "[$(date '+%F %T')] $*" >>"$LOG_FILE"; }

cd "$REPO_DIR" 2>/dev/null || { log "ERROR: repo dir missing: $REPO_DIR"; exit 1; }

if ! git fetch origin main --quiet 2>>"$LOG_FILE"; then
  log "ERROR: git fetch failed"
  exit 1
fi

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)
if [ "$LOCAL" = "$REMOTE" ]; then
  # 仓库无更新：顺带巡检服务健康，异常只记日志，不自动重启
  HTTP=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$HEALTH_URL" 2>/dev/null || true)
  [ "$HTTP" = "200" ] || log "WARNING: repo unchanged but service unhealthy (health=$HTTP)"
  exit 0
fi

# 有新提交：日志超过 5MB 先截断保留尾部，随后全部输出进日志
if [ -f "$LOG_FILE" ] && [ "$(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)" -gt 5242880 ]; then
  tail -n 2000 "$LOG_FILE" >"$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
fi
exec >>"$LOG_FILE" 2>&1

echo "[$(date '+%F %T')] new commits: ${LOCAL:0:7} -> ${REMOTE:0:7}, deploying"
git pull --ff-only origin main \
  || { echo "[$(date '+%F %T')] ERROR: git pull --ff-only failed (diverged or dirty), manual fix needed"; exit 1; }

echo "[$(date '+%F %T')] building images"
sudo docker compose -f deploy/docker-compose.yml build \
  || { echo "[$(date '+%F %T')] ERROR: build failed"; exit 1; }

echo "[$(date '+%F %T')] running migrations"
sudo docker compose -f deploy/docker-compose.yml run --rm api alembic upgrade head \
  || { echo "[$(date '+%F %T')] ERROR: migration failed"; exit 1; }

echo "[$(date '+%F %T')] restarting containers"
sudo docker compose -f deploy/docker-compose.yml up -d \
  || { echo "[$(date '+%F %T')] ERROR: up -d failed"; exit 1; }

sleep 8
HTTP=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$HEALTH_URL" || true)
if [ "$HTTP" = "200" ]; then
  echo "[$(date '+%F %T')] DEPLOY OK: $(git rev-parse --short HEAD) (health=200)"
  # 部署成功后清理构建缓存，仅保留最近 3GB，防止缓存随部署无限膨胀；
  # 失败不影响本次部署结果（下次成功部署会再清）
  sudo docker builder prune --keep-storage 3GB -f >/dev/null \
    || echo "[$(date '+%F %T')] WARNING: builder prune failed (non-fatal)"
else
  echo "[$(date '+%F %T')] WARNING: health check returned '$HTTP'; inspect: sudo docker compose -f deploy/docker-compose.yml logs api --tail 50"
  exit 1
fi
