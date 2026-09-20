#!/usr/bin/env bash
# KB 自动部署脚本（服务器端）
#
# 由 systemd timer（deploy/systemd/kb-auto-deploy.timer）每分钟调用一次：
#   1. fetch origin/main，无新提交则静默退出（服务不健康时仅记日志告警）
#   2. 有新提交：git pull --ff-only -> docker compose build -> alembic 迁移
#      -> up -d -> 健康检查（127.0.0.1:8000/health/ready 期望 HTTP 200）
#   3. 构建成功后给镜像打上提交号标签（deploy-api:<sha>），部署成功后保留最近
#      KEEP_VERSIONS 个旧版本供回退，其余摘掉标签并清理悬空层
#
# 回退：deploy/rollback.sh（不带参数列出保留的版本，带提交号执行回退）
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
COMPOSE_FILE="deploy/docker-compose.yml"
# 除当前 latest 外额外保留的旧构建版本数；只多留 app 代码层（约 5MB），
# python/pip/ffmpeg 基础层按内容去重共享，所以留 3 份几乎不占额外空间
KEEP_VERSIONS=3
# compose 未显式设置 image:，镜像名 = <项目目录名>-<服务名>；项目目录即 compose 文件所在目录
PROJECT=$(basename "$(dirname "$REPO_DIR/$COMPOSE_FILE")")
IMAGES=("$PROJECT-api" "$PROJECT-worker")

log() { echo "[$(date '+%F %T')] $*" >>"$LOG_FILE"; }

# 所有容器（含已停止）占用的镜像 ID，换行分隔
used_image_ids() {
  ids=$(sudo docker ps -aq | tr '\n' ' ')
  case "${ids// /}" in
    "") return 0 ;;
  esac
  sudo docker inspect -f '{{.Image}}' $ids 2>/dev/null | sort -u
}

# 摘掉超出 KEEP_VERSIONS 的旧版本标签。docker images 按创建时间倒序；latest 不计入
# 保留数；同一镜像 ID 的多个标签只算一个版本
prune_old_versions() {
  repo="$1"
  used="$2"
  sudo docker images "$repo" --no-trunc --format '{{.Tag}}\t{{.ID}}' \
    | awk -F'\t' -v keep="$KEEP_VERSIONS" \
        '$1 != "latest" && !seen[$2]++ && ++n > keep { print $1 }' \
    | while read -r tag; do
        id=$(sudo docker images "$repo:$tag" --no-trunc --format '{{.ID}}')
        case " $used " in *" $id "*) continue ;; esac
        sudo docker rmi "$repo:$tag" >/dev/null \
          || echo "[$(date '+%F %T')] WARNING: rmi $repo:$tag failed"
      done
}

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
sudo docker compose -f "$COMPOSE_FILE" build \
  || { echo "[$(date '+%F %T')] ERROR: build failed"; exit 1; }

# 按内容寻址的镜像本身可以共享，但 compose 只产出 :latest，上一版会变成 <none> 悬空镜像：
# 既没法按提交回退，也随时会被悬空清理掉。打完标签后，回退目标不再"悬空"，
# 后面的 image prune 才敢放心执行。
SHA=$(git rev-parse --short HEAD)
for repo in "${IMAGES[@]}"; do
  sudo docker tag "$repo:latest" "$repo:$SHA" \
    || echo "[$(date '+%F %T')] WARNING: tag $repo:$SHA failed"
done

echo "[$(date '+%F %T')] running migrations"
sudo docker compose -f "$COMPOSE_FILE" run --rm api alembic upgrade head \
  || { echo "[$(date '+%F %T')] ERROR: migration failed"; exit 1; }

echo "[$(date '+%F %T')] restarting containers"
sudo docker compose -f "$COMPOSE_FILE" up -d \
  || { echo "[$(date '+%F %T')] ERROR: up -d failed"; exit 1; }

sleep 8
HTTP=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$HEALTH_URL" || true)
if [ "$HTTP" = "200" ]; then
  echo "[$(date '+%F %T')] DEPLOY OK: $SHA (health=200)"
  # 健康检查通过后才清理，失败部署不把退路一起删掉
  USED=$(used_image_ids)
  for repo in "${IMAGES[@]}"; do
    prune_old_versions "$repo" "$USED"
  done
  # 悬空镜像即构建遗留层：已打标签的版本和被任何容器引用的镜像都不在其中
  sudo docker image prune -f \
    || echo "[$(date '+%F %T')] WARNING: image prune failed (non-fatal)"
  # 构建缓存保留最近 3GB；失败不影响本次部署结果（下次成功部署会再清）
  sudo docker builder prune --keep-storage 3GB -f >/dev/null \
    || echo "[$(date '+%F %T')] WARNING: builder prune failed (non-fatal)"
else
  echo "[$(date '+%F %T')] WARNING: health check returned '$HTTP'; inspect: sudo docker compose -f $COMPOSE_FILE logs api --tail 50"
  exit 1
fi
