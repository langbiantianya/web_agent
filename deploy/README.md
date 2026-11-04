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
docker compose -f deploy/docker-compose.lab.yml up -d --build
```

如需在容器内运行 isolated 档，先构建 sandbox 镜像，再启动实验栈。具体环境变量和构建顺序见 compose 文件头部说明。

## 安全要求

- studio 不注入数据库口令、模型 Key、`SECRET_KEY` 或任何 secret。
- studio 默认禁止出网。
- studio 不挂 Docker socket；isolated 调度只发生在 dispatcher。
- 主服务镜像以非 root 运行，凭据只通过环境变量注入，不进入镜像层。
- sandbox 使用一次性容器、只读 rootfs、非 root、无网络和固定资源上限。

## 相关文档

- [服务模块总览](../services/README.md)
- [studio 设计](../services/studio/design/README.md)
- [项目文档目录](../README.md)
