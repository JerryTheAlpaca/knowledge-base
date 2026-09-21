# 分享作品构建／检查镜像（docs/20 §7.2、§9.1、§14.2）
#
# 依赖在镜像构建阶段按 package-lock.json 固定安装；任务执行阶段不装包、不取 CDN。
# 浏览器用 Playwright 自带的固定 Chromium 构建，以非 root 运行；沙箱在 src/browser.mjs 里
# 显式请求开启（Playwright 的 chromiumSandbox 默认是 false，会往启动参数里塞 --no-sandbox）。
# 请求了不等于起得来：非 root + cap_drop: ALL + no-new-privileges 还依赖 setuid helper 或
# 可用的 user namespaces，实际生效与否只能由部署机 `share_runner doctor` 报出的 sandbox_enabled 判定。
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
# share_worker 容器（另一个 uid）读写：两端都用 kbshare（gid 固定 950，与服务端镜像
# 同一个组），目录 2770 + setgid 就够了，不必对任意本地进程放开 0777（审查 C-02）。
RUN groupadd -g 950 kbshare \
 && usermod -aG kbshare pwuser \
 && mkdir -p /spool/ready /spool/working /spool/done /spool/failed /spool/.tmp \
 && chown -R pwuser:kbshare /spool \
 && chmod -R 2770 /spool

# 基础镜像里的 pwuser 已具备可用的 Chromium sandbox
USER pwuser
ENV NODE_ENV=production

ENTRYPOINT ["node", "src/cli.mjs"]
CMD ["runner", "--spool", "/spool"]
