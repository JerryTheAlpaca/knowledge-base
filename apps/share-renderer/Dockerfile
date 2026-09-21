# 分享作品构建／检查镜像（docs/20 §7.2、§9.1、§14.2）
#
# 依赖在镜像构建阶段按 package-lock.json 固定安装；任务执行阶段不装包、不取 CDN。
# 浏览器用 Playwright 自带的固定 Chromium 构建，以非 root 运行、保持 sandbox 开启
# （Playwright 官方文档说明默认 root 运行会关闭 Chromium sandbox）。
#
# 基础镜像默认走官方 MCR；国内到 MCR 实测约 170KiB/s，一次全新拉取会被部署超时砍掉。
# 部署侧可用 PLAYWRIGHT_BASE_IMAGE 指到镜像站的同一 tag：两边 manifest 摘要一致
# （sha256:9bd26ad9…），blob 按内容寻址，不是另一份构建。
ARG PLAYWRIGHT_BASE_IMAGE=mcr.microsoft.com/playwright:v1.60.0-noble
FROM ${PLAYWRIGHT_BASE_IMAGE}

WORKDIR /app

# 只复制依赖清单：源码改动不会让依赖层缓存失效，构建阶段也不执行仓库里的脚本。
# 不再跑 `playwright install`：基础镜像里已带同版本 Chromium，构建阶段不碰 Playwright CDN。
COPY package.json package-lock.json ./
RUN npm ci && npm cache clean --force

COPY runtime-manifest.json ./
COPY src ./src
COPY samples ./samples

# 镜像自检：清单里登记的依赖入口必须存在（浏览器能否以非 root + sandbox 起跑
# 由部署后 `share_runner doctor` 实测，构建期是 root、验不到同一条路径）
RUN node -e "import('./src/manifest.mjs').then(async m => { await m.loadManifest(); console.log('manifest ok'); })"

# 空数据卷首次挂载时按镜像里同名目录的属主与权限初始化。spool 要同时被服务端
# worker 容器（另一个 uid）写入，所以在这里预先建好并对两端放开写权限。
RUN mkdir -p /spool/ready /spool/working /spool/done /spool/failed /spool/.tmp \
 && chown -R pwuser:pwuser /spool \
 && chmod -R 0777 /spool

# 基础镜像里的 pwuser 已具备可用的 Chromium sandbox
USER pwuser
ENV NODE_ENV=production

ENTRYPOINT ["node", "src/cli.mjs"]
CMD ["runner", "--spool", "/spool"]
