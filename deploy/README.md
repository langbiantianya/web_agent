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

后台由**两个**容器组成：

| 容器 | 职责 | 对外 |
| --- | --- | --- |
| `admin-ui` | SvelteKit 前端，接管 `/admin/*`：已迁的页面自己渲染，仍属 Python 的路径与**尚未迁完**的页面转发回 `admin-web` | `127.0.0.1:8001`（compose 网络内 `3000`） |
| `admin-web` | Python 管理端：鉴权、Casbin 判定、审计、管理面 JSON API、登录页、画布 island | 只在 compose 网络内可达，另留 `127.0.0.1:8011` 回环口供排查 |

**对外端口归 `admin-ui`**：浏览器地址与反代规则都不用改。反向代理把
`/admin/*` 指向 `8001` 即可——`admin-ui` 会按归属表自己转发。

**回退**：Ludic 视图层下线后（V4.10 阶段 E），`admin-web` **不再渲染业务
页面**，所以「把反代指回 `admin-web`」不再是可用的回退路径。回退方式改为
**按 tag 重建镜像**（`v4.10-pre-stage-e` 是阶段 E 之前的基线）。鉴权与授权
始终在 Python 侧，回退不需要迁任何数据。

容器默认限制为 768 MiB（admin-web）/ 512 MiB（admin-ui）、1 CPU、256 PIDs。
上线前需要：

- 运行迁移后首次启动 admin-web；服务会自动创建固定 owner，并在仅该次启动日志中
  输出随机密码。需要通知渠道时可在首次启动前手工运行
  `python -m scripts.bootstrap_superadmin`；
- 配置 `ADMIN_PUBLIC_BASE_URL`、OAuth callback、邮件和短信环境变量；
- 使用 HTTPS 并保持 `ADMIN_SESSION_COOKIE_SECURE=true`；
- 为 admin 与客户端配置独立数据库账号，限制客户端读取超级管理员密码摘要；
- 不在客户端镜像中安装 Ludic、Argon2 管理页或短信 SDK。

## Origin 转发：两种模式方向相反

后台前端（`admin-ui`）把浏览器请求转发给 `admin-web` 时，要带上 `Origin`
头——而**带哪个**取决于管理端当前的同源校验模式：

| 模式 | 触发条件 | `admin-web` 怎么判 | 网关该怎么转发 |
| --- | --- | --- | --- |
| 白名单 | 配了 `AUTH_ALLOWED_ORIGINS` | 只认白名单里的地址 | **原样转发**浏览器的 Origin |
| Host | 没配 | 拿 Origin 与自己看到的 `scheme://Host` 比 | **改写成** `admin-web` 自己的 origin |

方向搞反的后果一样：**所有写请求被拒，登录直接失败**。而且本地开发默认
没配白名单（Host 模式），所以这个缺陷**只在生产拓扑下才会暴露**。

网关通过 `GET /admin/api/origin-policy` 得知当前模式（响应
`{"mode": "allowlist" | "host", "origins": [...]}`，不含用户数据，不需要
登录），缓存 5 分钟。所以部署时**不需要**往 `AUTH_ALLOWED_ORIGINS` 里塞内部
地址——那是把两种模式的语义搅在一起。

## 生产拓扑：PostgreSQL 单一数据源 + 双账号

管理端与客户端**共用一个 PostgreSQL 库，但用两个不同账号**。这不是「分库」——
分库会让管理端写进去的 Agent / Workflow / LLM 端点在客户端读不到，管理界面
等于白做。

| 账号 | 权限 | 职责 |
| --- | --- | --- |
| 管理端（`ADMIN_DATABASE_URL`） | 全部权限 | 读写所有表；**由它跑 `alembic upgrade head`** |
| 客户端（`DATABASE_URL`） | 仅 DML（SELECT/INSERT/UPDATE/DELETE） | 运行期读写业务数据 |

客户端账号要额外 `REVOKE` 掉 `super_admin_account` 的读取权：客户端不需要
超级管理员的口令摘要，能读就等于把口令摘要交给了运行期。

```sql
-- 库由管理端账号持有；客户端账号只有 DML
GRANT USAGE ON SCHEMA public TO webagent;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO webagent;
REVOKE ALL ON TABLE super_admin_account FROM webagent;
```

**迁移以管理端为准**：客户端账号没有 DDL 权限，它启动时那次
`alembic upgrade head` 只在库**已经是 head** 时成立（空操作）。升级到含新
迁移的版本时，先让管理端起来把结构迁完，客户端随后自动重试即可。

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
