# studio 脚本执行服务

studio 是无状态的 gRPC 服务，只接收“脚本正文 + 参数”，在受限环境中执行并返回退出码、stdout 和 stderr。它不认识 Skill 业务模型，Skill 白名单、脚本路径和 argv 校验全部在 `web_agent` 调用侧完成。

## 本地启动

推荐使用完整实验栈：

```bash
docker compose -f deploy/docker-compose.lab.yml up -d --build
```

也可以直接运行进程，但宿主机需要具备 `python`、`bash` 和 `node`：

```bash
python -m services.studio
STUDIO_PORT=9001 python -m services.studio
```

在 `web_agent` 侧配置：

```dotenv
STUDIO_EXEC_ADDR=127.0.0.1:9000
STUDIO_EXEC_REQUIRED=true
```

`STUDIO_EXEC_ADDR` 为空表示不启用脚本执行，此时 `skill_exec` 不会注册。地址存在但服务不可达时，`STUDIO_EXEC_REQUIRED=true` 会让主服务启动失败，避免部署不完整却静默运行。

## 执行档位

| 档位 | 执行方式 | 适用场景 |
| --- | --- | --- |
| `fast` | studio 进程内的独立进程组 | 默认、短小脚本 |
| `isolated` | studio 调 dispatcher，每次创建一次性 sandbox | 强隔离、可能耗时数秒的脚本 |

服务端只能把请求档位升级，不能降级，并在响应中回显实际档位。声明 isolated 的技能要求 studio 版本和运行时能力匹配。

## 资源与安全

- 并发闸门和等待队列分别限制 fast 与 isolated 档。
- 超时后杀掉整个进程组，不只杀父进程。
- stdout 和 stderr 有明确上限，达到上限后停止继续读取。
- 可识别退出码 `-9` / `137`，并结合 cgroup 的 `oom_kill` 计数报告 `LIMITED`。
- gRPC 取消会传播到 dispatcher，并强删对应的 isolated 一次性容器。
- `STUDIO_SKILL_QUOTAS=skill-a=1` 与 `STUDIO_SKILL_QUEUE` 限制单技能并发和等待队列。
- `/metrics` 提供 sandbox 启动耗时、阶段计数和执行耗时。
- 容器使用非 root、只读根文件系统、临时目录、无凭据和无特权模式。

## 接口

proto 契约只有一份：[`studio.proto`](../../proto/webagent/studio/v1/studio.proto)。

- `Exec(ExecRequest) -> ExecResponse`：同步执行一次脚本。
- `Health(HealthRequest) -> HealthResponse`：报告版本、就绪状态、运行时和档位能力。

脚本自身退出码非 0 表示 `COMPLETED + exit_code != 0`，不是 gRPC 服务错误。只有 studio 自身故障、参数错误、队列满等情况才使用 gRPC status。

## 源码索引

| 文件 | 职责 |
| --- | --- |
| `server.py` | gRPC servicer、并发闸门、队列和去重 |
| `executor.py` | 进程组、超时、输出截断和环境变量白名单 |
| `launcher.py` | fast / isolated 档位路由 |
| `dispatcher_runtime.py` | dispatcher HTTP 客户端 |
| `oom.py` | OOM 退出码与 cgroup 佐证 |
| `metrics.py` | Prometheus 文本指标 |
| `exec_log.py` | 结构化执行日志 |
| `reaper.py` | 启动时清理残留进程 |

## 相关文档

- [studio 设计目录](design/README.md)
- [studio V4 当前设计](design/v4-current.md)
- [全局设计总目录](../../docs/design/README.md)
- [设计 TODO](../../docs/design/TODO.md)
- [dispatcher](../dispatcher/README.md)
- [sandbox](../sandbox/README.md)
- [部署说明](../../deploy/README.md)
