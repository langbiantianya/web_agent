# isolated 隔离档：Docker 实施记录

> 历史实施记录：本文说明 isolated 档如何分成 Docker 控制面与一次性 sandbox，并于
> 2026-11-04 完成真环境复验。本文不是当前 TODO；未完成事项统一见
> [设计 TODO](../../design/TODO.md)。

## 目标

`fast` 档在 studio 容器内运行模型指定的脚本。多个脚本共享同一个容器，隔离边界仅限于
进程组、临时目录、环境变量白名单和资源限制。对于需要更强隔离的技能，isolated 档改为
每次执行创建一个一次性 sandbox 容器。

安全前提是：**运行用户代码的进程不持有创建容器的凭据。**

Docker socket 只挂载到 `studio-dispatcher`。dispatcher 不执行脚本正文，只按固定模板
创建、读取并清理 sandbox。studio 通过内部 HTTP 调用 dispatcher，自身不挂 socket。

## 最终链路

```text
模型
 └─ skill_exec(skill, script, args)
     └─ web_agent
         ├─ 技能白名单 / 脚本路径 / argv 校验
         ├─ 从数据库读取脚本
         └─ gRPC → studio
             ├─ fast：studio 内独立进程组
             └─ isolated：studio → dispatcher → 一次性 Docker sandbox
```

isolated 请求的执行顺序：

1. web_agent 解析技能声明的 `runtime-profile`，并与 studio `Health` 能力对账。
2. `ExecRequest.profile` 明确请求 isolated；服务端只能保持或升级档位，不能降级到 fast。
3. studio 在独立并发池中排队，并把执行规格交给 dispatcher。
4. dispatcher 写只读 payload，按固定模板创建 sandbox。
5. sandbox runner 读取 payload、启动脚本、收集输出并发送控制帧。
6. dispatcher 解析有界结果，删除 sandbox，studio 回传档位、状态、退出码和错误码。
7. web_agent 断言响应实际档位不低于请求档位，再回灌给模型。

## 已实现

### 档位与能力协商

- proto 增加 `ExecProfile`、`ExecRequest.profile/image`、
  `ExecResponse.profile/startup_ms/error_code` 和 `HealthResponse.profiles/images`。
- 技能通过扁平元数据声明 `runtime-profile`、`runtime-image` 和 `runtime-timeout-ms`。
- web_agent 校验 studio 最低版本、实际档位回显和语言/镜像能力，拒绝静默降级。
- 启动期审计已绑定技能；声明 isolated 但当前部署不具备能力时明确失败。

### Docker 控制面

- `services/dispatcher/` 是只调度、不执行脚本的控制面。
- Docker 后端固定设置无网络、只读 rootfs、非 root、capabilities 全空、
  `no-new-privileges`、PID/内存/CPU 限额。
- 请求不能指定任意镜像、挂载或容器模板；镜像只能来自服务端目录。
- payload 写入独立目录，再以只读 bind mount 交给 sandbox。
- dispatcher 的有界日志读取负责结果回收、超时与异常映射；完成后主动删除容器。

### sandbox 协议

- `services/sandbox/runner.py` 读取 payload，设置资源限制并启动脚本。
- 控制帧区分 `META`、`OUT`、`ERR` 和 `END`，4 KB 分片避免运行时切行破坏结果。
- `END` 携带摘要；缺少结果帧或摘要不匹配时返回 `sandbox_result_lost`。
- OOM 同时结合退出状态与 cgroup v1/v2 `oom_kill` 计数，避免漏判子进程 OOM。
- 输出达到上限时停止读取、终止进程组并返回 `LIMITED`。

### studio 执行与清理

- fast 和 isolated 使用独立并发池与等待队列，排队时间计入调用时限。
- 超时杀整个进程组；启动巡检根据临时工作目录清理残留进程。
- 去重只缓存成功与超时结果，瞬态失败允许重试；语义仍为 at-least-once。
- 指标和结构化日志包含档位、镜像、启动耗时、错误码与输出摘要，不记录输出正文。

## 关键配置

| 配置 | 作用 |
| --- | --- |
| `STUDIO_PROFILE` | 未显式请求档位时的实例默认档 |
| `STUDIO_ISOLATED_SKILL_CODES` | 运维强制升级到 isolated 的技能码，只升不降 |
| `STUDIO_ISOLATED_IMAGES` | 服务端镜像目录与允许的语言 |
| `STUDIO_ISOLATED_DISPATCHER_ADDR` | studio 调用 dispatcher 的内部地址 |
| `STUDIO_ISOLATED_MAX_INFLIGHT` | dispatcher 同时在途上限 |
| `DISPATCHER_PAYLOAD_DIR` | dispatcher 与 Docker 后端共享的 payload 目录 |
| `STUDIO_EXEC_LB_POLICY` | gRPC 连接均衡策略；多实例服务发现时使用 |

## 真 Docker 复验

测试机为 4 vCPU / 1.8 GB RAM，串行启动真实 MySQL、Redis、web_agent、studio、
dispatcher 与一次性 sandbox，控制并发以降低内存压力。

验证结果：

| 项 | 结果 |
| --- | --- |
| Python / Bash / Node | 三种运行时均返回 `COMPLETED + exit_code=0` |
| 网络隔离 | sandbox 没有网络端点，DNS 与外连均被阻断 |
| 文件系统 | rootfs 只读，运行用户为 uid 1000 |
| 权限 | capabilities 全空、非特权、`no-new-privileges` |
| 资源限制 | PID、内存与 CPU 限额生效 |
| 凭据 | 只挂载只读 payload，没有 Docker socket、数据库口令、应用密钥或模型 Key |
| 生命周期 | 执行结束后容器被删除，没有 sandbox 残留 |

真实模型五场景：

| 场景 | 结果 |
| --- | --- |
| happy | `profile=isolated`、退出码 0、stdout 正确回灌 |
| timeout | `sandbox_timeout`，run 继续完成 |
| busy | 队列满，模型收到“studio 忙不过来” |
| studio down | 连接拒绝，模型收到“连不上 studio” |
| sandbox OOM | `LIMITED + sandbox_oom`，run 继续完成 |

## 验证发现并修复的问题

1. dispatcher 从错误工作目录启动 runner，导致 isolated 请求在执行前失败。
2. 临时目录权限为 `0700` 时 uid 1000 无法读取 payload；改为目录 `0755`、
   payload `0644`，同时保持只读挂载。
3. runner 的子进程被 cgroup OOM killer 杀掉时，容器级 `OOMKilled` 可能为 false；
   改为比较 cgroup 累计计数。

## 已知边界

- isolated 当前只实现 Docker 后端，不维护其它容器编排后端。
- 执行语义是 at-least-once；脚本有副作用时必须自行保证幂等。
- sandbox 无网络、无业务凭据；需要外部服务或密钥的脚本应通过 MCP 工具完成。
- 输入输出只支持文本；二进制的 base64 处理仍在 TODO 中。
- 取消不会保证立即终止 sandbox；依赖请求结束、TTL 与清理路径兜底。
- 配额是无状态的实例级限制；跨实例全局配额仍在 TODO 中。

## 相关文档

| 文档 | 内容 |
| --- | --- |
| `services/studio/design/v4-current.md` §2.4 | isolated 当前架构与执行路径 |
| `services/studio/design/v4-current.md` §13 | Docker 部署与加固 |
| `services/studio/design/v4-current.md` §20 | 2026-11-04 真 Docker 复验 |
| `app/design/v4-tools-skills.md` §22.8 | web_agent 调用侧契约与失败语义 |
| `docs/design/TODO.md` | 当前未完成事项 |
