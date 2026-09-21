# 分享作品构建／检查镜像（docs/20 §7.2、§9.1、§14.2）
#
# 依赖在镜像构建阶段按 package-lock.json 固定安装；任务执行阶段不装包、不取 CDN。
# 浏览器用 Playwright 自带的固定 Chromium 构建，以非 root 运行、保持 sandbox 开启
# （Playwright 官方文档说明默认 root 运行会关闭 Chromium sandbox）。
FROM mcr.microsoft.com/playwright:v1.60.0-noble

WORKDIR /app

# 只复制依赖清单：源码改动不会让依赖层缓存失效，构建阶段也不执行仓库里的脚本
COPY package.json package-lock.json ./
RUN npm ci --omit=dev=false \
 && npx playwright install chromium \
 && npm cache clean --force

COPY runtime-manifest.json ./
COPY src ./src
COPY samples ./samples

# 镜像自检：清单里登记的依赖入口必须存在，浏览器必须能以非 root + sandbox 启动
RUN node -e "import('./src/manifest.mjs').then(async m => { await m.loadManifest(); console.log('manifest ok'); })"

# 基础镜像里的 pwuser 已具备可用的 Chromium sandbox
USER pwuser
ENV NODE_ENV=production

ENTRYPOINT ["node", "src/cli.mjs"]
CMD ["runner", "--spool", "/spool"]
