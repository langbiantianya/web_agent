# 部署

`deploy/` 提供本地 compose、一体化实验栈和 Kubernetes 清单。生产部署前请先确认 studio 的零凭据、禁出网和 headless gRPC Service 配置。

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

## Kubernetes

清单位于 [`k8s/`](k8s)，包括：

- studio Deployment
- headless Service
- NetworkPolicy

部署步骤、探针自检和已知限制见 [Kubernetes 部署说明](k8s/README.md)。

## 安全要求

- studio 不注入数据库口令、模型 Key、`SECRET_KEY` 或任何 secret。
- studio 默认禁止出网。
- studio 不挂 Docker socket；isolated 调度只发生在 dispatcher。
- 主服务镜像以非 root 运行，凭据只通过环境变量注入，不进入镜像层。
- gRPC 使用 headless Service 和 `round_robin`，否则长连接会固定在单个 Pod。

## 相关文档

- [服务模块总览](../services/README.md)
- [studio 设计](../services/studio/DESIGN.md)
- [项目文档目录](../README.md)
