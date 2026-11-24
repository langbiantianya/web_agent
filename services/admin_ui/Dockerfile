# SvelteKit 管理端镜像（V4.10）。
#
# 与 admin-web 容器并列：两个容器共享同一个管理库，但角色不同——
# admin-web 负责会话、CSRF、Casbin 与审计；本容器只负责渲染与交互。
#
# 构建阶段用 npm ci（锁文件为准），运行阶段只带构建产物，不带 node_modules。

FROM node:24-alpine AS build
WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY . .
RUN npm run build

FROM node:24-alpine AS runtime
WORKDIR /app
ENV NODE_ENV=production
ENV PORT=8002
ENV HOST=0.0.0.0

COPY --from=build /app/build ./build
COPY --from=build /app/package.json ./package.json

EXPOSE 8002
USER node

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["node", "-e", "fetch('http://127.0.0.1:'+(process.env.PORT||8002)+'/admin/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"]

CMD ["node", "build"]