#!/usr/bin/env bash
# KB 镜像回退（服务器端）：退回 auto-deploy.sh 保留下来的历史构建版本
#
#   deploy/rollback.sh                  # 列出可回退的版本
#   sudo deploy/rollback.sh 0a3c94f     # 回退到该提交构建的镜像
#
# 版本标签由 deploy/auto-deploy.sh 每次构建时打上，只保留最近 KEEP_VERSIONS 个，
# 更早的已被摘掉标签，回退不到。
#
# 只回退代码，不回退 alembic 迁移：跨迁移版本回退前先确认 schema 兼容，
# 必要时先备份 /data/db/app.db。仓库 ~/kb-inbox 仍停在新提交上，main 一旦
# 有新提交，自动部署会重新构建并覆盖 :latest。
set -u

cd "$(dirname "$0")" || exit 1
COMPOSE_FILE="docker-compose.yml"
PROJECT=$(basename "$PWD")
IMAGES=("$PROJECT-api" "$PROJECT-worker")
HEALTH_URL="http://127.0.0.1:8000/health/ready"

if [ $# -eq 0 ]; then
  for repo in "${IMAGES[@]}"; do
    echo "== $repo =="
    sudo docker images "$repo" --format '  {{.Tag}}\t{{.CreatedSince}}'
  done
  echo
  echo "用法：sudo $PWD/$(basename "$0") <提交号>"
  exit 0
fi

SHA="$1"
for repo in "${IMAGES[@]}"; do
  if ! sudo docker image inspect "$repo:$SHA" >/dev/null 2>&1; then
    echo "ERROR: 本机没有 $repo:$SHA（未构建过或已按保留数清理）"
    exit 1
  fi
done

echo "回退 $PROJECT 到 $SHA"
for repo in "${IMAGES[@]}"; do
  sudo docker tag "$repo:$SHA" "$repo:latest" || exit 1
done
# :latest 换了镜像内容但标签没变，compose 可能判定容器无需重建，所以强制重建
sudo docker compose -f "$COMPOSE_FILE" up -d --force-recreate || exit 1

sleep 8
echo "health=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$HEALTH_URL")"
