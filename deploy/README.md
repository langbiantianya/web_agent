# 部署

`deploy/` 提供本地 compose 和一体化实验栈。部署前请确认 studio 的零凭据、禁出网、非 root、只读 rootfs 与资源限额。

## 只启动 studio

```bash
docker compose -f deploy/docker-compose.yml up -d --build
```

然后在 `web_agent` 配置：

```dotenv
STUDIO_EXEC_ADDR=127.0.0.1:9000
STUDIO_EXEC_REQUIRED=true
```

## 完整实验栈

[`docker-compose.lab.yml`](docker-compose.lab.yml) 启动 `web_agent`、`studio` 和 `studio-dispatcher`，并把 studio 放入没有默认网关的内部网络：

```bash
cp .env.example .env
# 在 .env 或部署环境中填写 MYSQL_PASSWORD；不要提交该文件
docker compose -f deploy/docker-compose.lab.yml up -d --build
```

实验栈要求 `MYSQL_USER`、`MYSQL_PASSWORD`、`MYSQL_DATABASE` 从环境注入。
`MYSQL_PASSWORD` 为空时 Compose 会直接报错，避免把测试口令写回仓库。
dispatcher 提供 `/health` Docker healthcheck，studio 会等待它 healthy 后再启动；
studio 自身也会在 isolated 启动预算内重试，覆盖慢启动竞态。

如需在容器内运行 isolated 档，先构建 sandbox 镜像，再启动实验栈。具体环境变量和构建顺序见 compose 文件头部说明。

## 独立管理后台

后台与客户端分开部署，使用不同的 ASGI 入口、Cookie 和容器：

```bash
docker compose -f deploy/docker-compose.admin.yml up -d --build
```

`admin-web` 默认只绑定 `127.0.0.1:8001`，所有 `/admin/*` 请求应由反向代理转发。
容器默认限制为 768 MiB、1 CPU、256 PIDs。上线前需要：

- 运行迁移后首次启动 admin-web；服务会自动创建固定 owner，并在仅该次启动日志中
  输出随机密码。需要通知渠道时可在首次启动前手工运行
  `python -m scripts.bootstrap_superadmin`；
- 配置 `ADMIN_PUBLIC_BASE_URL`、OAuth callback、邮件和短信环境变量；
- 使用 HTTPS 并保持 `ADMIN_SESSION_COOKIE_SECURE=true`；
- 为 admin 与客户端配置独立数据库账号，限制客户端读取超级管理员密码摘要；
- 不在客户端镜像中安装 Ludic、Argon2 管理页或短信 SDK。

## 安全要求

- studio 不注入数据库口令、模型 Key、`SECRET_KEY` 或任何 secret。
- studio 默认禁止出网。
- studio 不挂 Docker socket；isolated 调度只发生在 dispatcher。
- 主服务镜像以非 root 运行，凭据只通过环境变量注入，不进入镜像层。
- sandbox 使用一次性容器、只读 rootfs、非 root、无网络和固定资源上限。
- dispatcher 启动时加载自定义 seccomp profile；缺失或非法时 fail-closed。
- 多个 studio 共用 dispatcher 时，租户/技能并发与活动预算在同一准入面生效。
- 生产 PostgreSQL 应按 [PITR 指南](PITR.md) 配置独立 WAL 归档、全量/增量备份和恢复演练。

## 相关文档

- [服务模块总览](../services/README.md)
- [studio 设计](../services/studio/design/README.md)
- [项目文档目录](../README.md)
