# SvelteKit 管理端镜像（V4.10）。
#
# 两段构建：
# - build 段装全量依赖并跑 `vite build`，产出 adapter-node 的 `build/`；
# - runtime 段只装**生产依赖**与构建产物，镜像里不留构建工具链。
#
# 运行期只有一个 Node 进程（adapter-node），监听 3000。它是后台的**前端**：
# 鉴权与授权仍然在 Python 侧——会话 Cookie、CSRF、Casbin 判定、审计都不在
# 这里实现。
FROM node:24-alpine AS build
WORKDIR /app

# 先只拷依赖清单：这样改业务代码不会让 npm 缓存失效，重建快很多。
COPY services/admin_ui/package.json services/admin_ui/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY services/admin_ui/ ./
RUN npm run build

FROM node:24-alpine AS runtime
WORKDIR /app
ENV NODE_ENV=production \
    PORT=3000 \
    HOST=0.0.0.0

COPY services/admin_ui/package.json services/admin_ui/package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund && npm cache clean --force

COPY --from=build /app/build ./build
COPY --from=build /app/node_modules ./node_modules
COPY services/admin_ui/package.json ./package.json

USER node
EXPOSE 3000

# 运行期不需要写盘：只读 rootfs + /tmp 的 tmpfs 足够。
CMD ["node", "build/index.js"]
