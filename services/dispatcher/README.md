# studio-dispatcher

dispatcher 是 isolated 档的控制面。它持有 Docker socket 或等价运行时权限，但不接收脚本执行结果以外的业务语义，也不会在自身进程内执行 `content`。

## 运行方式

dispatcher 通常由一体化实验栈启动：

```bash
docker compose -f deploy/docker-compose.lab.yml up -d studio-dispatcher
```

studio 通过 `STUDIO_ISOLATED_DISPATCHER_ADDR` 访问它，例如：

```dotenv
STUDIO_ISOLATED_DISPATCHER_ADDR=studio-dispatcher:9100
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 检查 dispatcher 和容器运行时是否可用 |
| POST | `/v1/exec` | 按固定模板创建一次性沙箱并等待结果 |
| POST | `/v1/exec/{execution_id}/cancel` | 强制删除运行中的一次性容器 |

dispatcher 只接受已约束的 `SandboxSpec`。镜像必须来自 `DISPATCHER_IMAGES` 白名单，请求
超时不能超过 `DISPATCHER_MAX_TIMEOUT_MS`。生产默认要求镜像 ref 固定 sha256 digest。

## 主要配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DISPATCHER_PORT` | `9100` | HTTP 监听端口 |
| `DISPATCHER_MAX_INFLIGHT` | `2` | 同时调度的沙箱数量 |
| `DISPATCHER_MAX_TIMEOUT_MS` | `60000` | 单次执行上限 |
| `DISPATCHER_STARTUP_BUDGET_MS` | `15000` | 沙箱启动预算 |
| `DISPATCHER_IMAGES` | 空 | `目录名=镜像@digest` 白名单 |
| `DISPATCHER_REQUIRE_DIGEST` | `true` | 拒绝可变 tag；仅刚构建本地镜像的实验栈显式关闭 |
| `DISPATCHER_DOCKER_SOCKET` | `unix:///var/run/docker.sock` | Docker 运行时地址 |
| `DISPATCHER_PAYLOAD_DIR` | `/var/lib/studio-payloads` | 请求载荷交换目录 |

Docker socket 只挂载到 dispatcher。studio 和 sandbox 都看不到它。

## 源码索引

| 文件 | 职责 |
| --- | --- |
| `server.py` | 内部 HTTP 服务、请求校验和并发限制 |
| `backend.py` | `SandboxBackend` 协议 |
| `docker_backend.py` | Docker 固定模板调度 |
| `config.py` | dispatcher 环境配置 |

完整协议和安全要求见 [studio 设计目录](../studio/design/README.md) 与
[sandbox 说明](../sandbox/README.md)。
