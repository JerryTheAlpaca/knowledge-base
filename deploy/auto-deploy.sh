#!/usr/bin/env bash
# KB 自动部署脚本（服务器端）
#
# 由 systemd timer（deploy/systemd/kb-auto-deploy.timer）每分钟调用一次：
#   1. fetch origin/main，无新提交则静默退出（服务不健康时仅记日志告警）
#   2. 有新提交：git pull --ff-only -> 只构建本次改动涉及的服务 -> alembic 迁移
#      -> up -d -> 健康检查（127.0.0.1:8000/health/ready 期望 HTTP 200）
#   3. 构建成功后给镜像打上提交号标签（deploy-api:<sha>），部署成功后保留最近
#      KEEP_VERSIONS 个旧版本供回退，其余摘掉标签并清理悬空层
#   4. 磁盘水位：常规清理只回收没被镜像引用的缓存；爬到 DISK_WARN_PCT 以上时把回退
#      版本和本项目已退出的容器一并摘掉，再清全部 BuildKit 缓存记录，直到降回线下
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
# 磁盘水位线（%）。`builder prune -f` 只清不被引用的缓存记录，实测每轮回收 65–800MB；
# 而标着「与镜像层同一份（Shared）」的那部分并非清不掉——10-03 在生产实测
# `builder prune -af` 把 12.91GB 记录整排摘掉，containerd 从 19G 降到 7.8G，回收 11.2G，
# 期间一张镜像没少、四个容器没重启。所以到 WARN 就升级成 -af。
# 代价只有一项：下一次构建要重跑 pip install / npm ci（基础镜像仍在，不会重拉 787MB 层）。
DISK_WARN_PCT=90
# 到 FAIL 且本轮需要构建时直接跳过：2 核机上 share_runner（底座 Playwright 镜像 3.4GB）
# 构建中途写满会同时留下半成品层和缓存记录，而且失败不会自动重试（ timer 只会看到
# "没有新提交"），停在旧版本比把盘挤爆更可恢复。
DISK_FAIL_PCT=95
# compose 未显式设置 image:，镜像名 = <项目目录名>-<服务名>；项目目录即 compose 文件所在目录
PROJECT=$(basename "$(dirname "$REPO_DIR/$COMPOSE_FILE")")
IMAGES=("$PROJECT-api" "$PROJECT-worker")

log() { echo "[$(date '+%F %T')] $*" >>"$LOG_FILE"; }

disk_pct() { df --output=pcent / 2>/dev/null | tail -1 | tr -dc '0-9'; }

# 所有容器（含已停止）占用的镜像 ID，空格分隔——prune_old_versions 用
# `case " $used " in *" $id "*` 判断，换行分隔时这个匹配永不命中（审查报告 22 §镜像保留）
used_image_ids() {
  ids=$(sudo docker ps -aq | tr '\n' ' ')
  case "${ids// /}" in
    "") return 0 ;;
  esac
  sudo docker inspect -f '{{.Image}}' $ids 2>/dev/null | sort -u | tr '\n' ' '
}

# 摘掉超出保留数的旧版本标签。docker images 按创建时间倒序；latest 不计入
# 保留数；同一镜像 ID 的多个标签只算一个版本。keep 默认 KEEP_VERSIONS，
# 磁盘紧张时由升级清理传 0（只留 :latest 和在跑的容器）
prune_old_versions() {
  repo="$1"
  used="$2"
  keep="${3:-$KEEP_VERSIONS}"
  sudo docker images "$repo" --no-trunc --format '{{.Tag}}\t{{.ID}}' \
    | awk -F'\t' -v keep="$keep" \
        '$1 != "latest" && !seen[$2]++ && ++n > keep { print $1 }' \
    | while read -r tag; do
        id=$(sudo docker images "$repo:$tag" --no-trunc --format '{{.ID}}')
        case " $used " in *" $id "*) continue ;; esac
        sudo docker rmi "$repo:$tag" >/dev/null \
          || echo "[$(date '+%F %T')] WARNING: rmi $repo:$tag failed"
      done
}

# 磁盘超线时的升级清理：从便宜的做起，每步重新读数，降回 WARN 以下就停。
# 只动本项目：不碰别的站点的退出容器，也不 rmi 任何仍在运行的镜像。
# 参数：当前被容器占用的镜像 ID（空格分隔）
escalate_disk_cleanup() {
  used="$1"
  start=$(disk_pct)
  [ "${start:-0}" -lt "$DISK_WARN_PCT" ] && return 0
  log "DISK GUARD: ${start}% used (>= ${DISK_WARN_PCT}%), escalating"
  for repo in "${IMAGES[@]}"; do
    prune_old_versions "$repo" "$used" 0
  done
  sudo docker image prune -f >/dev/null 2>&1 || true
  if [ "$(disk_pct)" -ge "$DISK_WARN_PCT" ]; then
    # compose run --rm 正常会自毁，异常退出时留下带可写层的一次性容器
    exited=$(sudo docker ps -aq --filter status=exited --filter "name=${PROJECT}-" 2>/dev/null)
    if [ -n "$exited" ]; then
      sudo docker rm $exited >/dev/null 2>&1 || true
    fi
    sudo docker builder prune -af >/dev/null 2>&1 || true
  fi
  log "DISK GUARD done: ${start}% -> $(disk_pct)%"
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

# 先记下这次要合并的改动落在哪些目录（pull 之后 HEAD 就变了，取不到这个区间）
CHANGED=$(git diff --name-only "$LOCAL" "$REMOTE" 2>/dev/null || echo "")
need_server=0
need_renderer=0
while IFS= read -r path; do
  case "$path" in
    apps/server/*) need_server=1 ;;
    apps/share-renderer/*) need_renderer=1 ;;
    # 插件源码本身不进镜像，但发布产物 plugin_dist 在 apps/server 下；
    # 源码改动若没跑 npm run release 就不会改变产物，也就不需要重建
    deploy/docker-compose.yml) need_server=1; need_renderer=1 ;;
  esac
done <<<"$CHANGED"

# 有新提交：日志超过 5MB 先截断保留尾部，随后全部输出进日志
if [ -f "$LOG_FILE" ] && [ "$(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)" -gt 5242880 ]; then
  tail -n 2000 "$LOG_FILE" >"$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
fi
exec >>"$LOG_FILE" 2>&1

echo "[$(date '+%F %T')] new commits: ${LOCAL:0:7} -> ${REMOTE:0:7}, deploying"
git pull --ff-only origin main \
  || { echo "[$(date '+%F %T')] ERROR: git pull --ff-only failed (diverged or dirty), manual fix needed"; exit 1; }

# 构建输入按目录划分：apps/server 一个 context 产出 api/worker/share_worker 三个镜像
#（层按内容共享，重建几乎不额外占空间），apps/share-renderer 产出 share_runner
#（底座 Playwright 镜像 3.4GB）。docs/tests/scripts/contracts 不进镜像。
# 此前每轮 push 都无脑 build 全部四个服务，是 containerd 快照 13 天新增 435 个的直接来源。
if [ "$need_server" = 1 ] || [ "$need_renderer" = 1 ]; then
  SERVICES=()
  [ "$need_server" = 1 ] && SERVICES+=(api worker share_worker)
  [ "$need_renderer" = 1 ] && SERVICES+=(share_runner)
  USE=$(disk_pct)
  if [ "${USE:-0}" -ge "$DISK_FAIL_PCT" ]; then
    echo "[$(date '+%F %T')] ERROR: disk at ${USE}% (>= ${DISK_FAIL_PCT}%), skipping build; 先人工回收再推"
    exit 1
  fi
  echo "[$(date '+%F %T')] building: ${SERVICES[*]} (disk ${USE}%)"
  sudo docker compose -f "$COMPOSE_FILE" build "${SERVICES[@]}" \
    || { echo "[$(date '+%F %T')] ERROR: build failed"; exit 1; }
else
  echo "[$(date '+%F %T')] no build inputs changed (docs/scripts only), skipping build"
fi

SHA=$(git rev-parse --short HEAD)

# 按内容寻址的镜像本身可以共享，但 compose 只产出 :latest，上一版会变成 <none> 悬空镜像：
# 既没法按提交回退，也随时会被悬空清理掉。打完标签后，回退目标不再"悬空"，
# 后面的 image prune 才敢放心执行。
# 只在真重建了服务端镜像时打：没构建也打标签，等于把同一份镜像多算一个保留版本，
# 把 KEEP_VERSIONS 的坑位白白占掉。
if [ "$need_server" = 1 ]; then
  for repo in "${IMAGES[@]}"; do
    sudo docker tag "$repo:latest" "$repo:$SHA" \
      || echo "[$(date '+%F %T')] WARNING: tag $repo:$SHA failed"
  done
fi

# 迁移每轮都跑，不按改动目录跳过：构建失败的那一轮不会重试（timer 之后只看到
# "没有新提交"），若因为后续一个纯文档提交就没跑迁移，schema 会一直停在旧版本上。
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
  # 构建缓存：只清 BuildKit 里已不被任何镜像引用的记录。与镜像层共享的那部分不在清理
  # 范围内（留着让下次构建继续秒级命中），所以这一步每轮实测只回收 65–800MB。
  # 不用 --keep-storage：它按缓存总量封顶，会把共享的基础层记录一起挤掉，换来回填重编。
  # 输出进日志，回收量可核对；失败不影响本次部署结果（下次成功部署会再清）
  sudo docker builder prune -f \
    || echo "[$(date '+%F %T')] WARNING: builder prune failed (non-fatal)"
  # 收尾记一次用量，磁盘增长能从日志里按天读出来；超线再走升级清理
  echo "[$(date '+%F %T')] disk after routine cleanup: $(disk_pct)% used"
  escalate_disk_cleanup "$USED"
else
  echo "[$(date '+%F %T')] WARNING: health check returned '$HTTP'; inspect: sudo docker compose -f $COMPOSE_FILE logs api --tail 50"
  exit 1
fi
