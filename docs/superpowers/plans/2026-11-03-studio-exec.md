# studio 服务 + `skill_exec`（V4 Phase 3）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让技能包里的脚本能**真的跑起来**——但不在 web_agent 进程内跑。脚本经 gRPC 交给一个独立的 studio 服务执行，主服务进程里**不起任何子进程**。

**Architecture:** 一份 proto 契约（仓库里只有这一份，两边都从它生成代码）；调用侧 `app/core/studio.py`（gRPC 客户端 + 启动期兼容性检查）与 `app/core/skill_exec.py`（三道调用侧校验）；服务侧 `services/studio/`（gRPC server + 进程隔离 + 超时 + 截断 + 限流 + 去重 + 优雅停机）；部署产物 `services/studio/Dockerfile` 与 `deploy/k8s/`。

**Tech Stack:** Python 3.11+ / FastAPI / SQLAlchemy 2.0 async / Alembic / `grpcio` 1.85（+ `grpcio-tools` 生成桩代码）/ pytest-asyncio（`asyncio_mode = auto`）

## 全局约束

- **脚本只在 studio 容器里跑**：web_agent 进程内**不起任何子进程**。这是安全设计——技能可能来自导入的 zip，而 web_agent 进程持有 `SECRET_KEY` 与数据库口令。
- **三道校验全在 web_agent 侧**：技能白名单 / 脚本路径格式 / argv 形态。**校验不过的请求根本没发出去**（§22.13 的验收项），studio 收到的永远是已校验过的内容。
- **参数绝不拼成 shell 字符串**：直接传给进程，参数里的 `;` `|` 就是普通字符。
- **gRPC 成功 ≠ 脚本成功**：脚本自己 `exit 1` 是 `completed` + `exit_code=1`，不是错误。gRPC status 只表达 studio 自己的问题。
- **超时是硬的**：模型请求的时限只能在 `studio_exec_max_timeout_ms` 之内，模型改不了。gRPC deadline 是「时限 + 2 秒宽限」，先到哪个算哪个，**不做传输层重试**。
- **「地址为空」与「连不上」是两件事**：地址为空 = 未启用（服务正常起、`skill_exec` 不注册）；地址有 + 连不上 + `required=true` = **启动失败**。
- **`skill_exec` 是可选工具**：studio 不可用时**根本不注册**——模型看不见它、配置期也拒，不留「配了但调不通」的哑配置。
- **工具结果是不可信输入**：脚本输出回灌前必须过 §20 的敏感词过滤，**失败的脚本其 stderr 同样要筛**。
- **容器级加固不在代码里**：只读 rootfs / 非 root / drop capabilities / 禁出网 / 零凭据，在编排平台的容器配置里做。代码里的进程组、RLIMIT、临时目录只是第二道。
- **注释与 docstring 用中文**，解释「为什么」而不是「做了什么」。

## Phase 拆分说明

| Phase | 内容 | 计划 |
| --- | --- | --- |
| Phase 1 | Skill 基础：表 + CRUD + 导入导出 + 审计 + 提示词注入 + `skill_resource` 工具 | `2026-11-02-skill-basics.md` |
| Phase 2 | MCP 客户端：连接管理、工具目录快照、`mcp__` 命名分发、API | `2026-11-02-mcp-client.md` |
| **Phase 3（本计划）** | studio 服务 + `skill_exec`：proto、gRPC 客户端、容器部署 | 本文件 |

---

### Task 1: proto 契约落库 + 生成桩代码

**Files:**
- Create: `proto/webagent/studio/v1/studio.proto`
- Create: `webagent/studio/v1/studio_pb2.py`、`webagent/studio/v1/studio_pb2_grpc.py`（生成物）
- Create: `scripts/gen_proto.py`、`scripts/check_proto_contract.py`
- Create: `tests/test_proto_contract.py`
- Modify: `requirements.txt`

- [x] 契约**从两份文档逐字抽出**：`service Studio { Exec, Health }`、`ExecStatus` 6 个枚举、`ExecRequest` 10 字段、`ExecResponse` 8 字段、`HealthResponse` 5 字段。
- [x] **生成桩放仓库根的 `webagent/`**（不是 `app/`）：包名就是 proto 里的 `package webagent.studio.v1;`，`protoc` 生成的 `from webagent.studio.v1 import studio_pb2` 直接可解析，**一行生成代码都不用改**；studio 容器不装 web_agent 的代码，只拷这个目录即可。
- [x] `scripts/gen_proto.py` 幂等重新生成；`scripts/check_proto_contract.py` 比对「proto 文件 vs `docs/design.md` §22.8.3 vs `docs/skill-service.md` §5」三处**逐字一致**，不一致时打印 diff 并非 0 退出。

### Task 2: gRPC 客户端与执行上下文

**Files:**
- Create: `app/core/studio.py`、`app/core/studio_context.py`
- Modify: `app/config/settings.py`
- Create: `tests/test_studio_client.py`、`tests/test_studio_context.py`

- [x] `StudioClient`：`health` / `exec_script` / `close`，**懒建通道**（构造时不碰网络）。
- [x] `effective_timeout_ms()`：压到 `studio_exec_max_timeout_ms`；`DEADLINE_GRACE_SEC = 2.0`。
- [x] `_map_rpc_error()`：按 gRPC status code 翻译成 `StudioUnavailableError` / `StudioTimeoutError` / `StudioError`。
- [x] `StudioExecContext`（run + 节点 + tool_call_id）+ `make_request_id()`：来源是「**run + 节点 + tool_call_id**」，**不是脚本内容哈希**——按内容哈希会把「模型合法地再跑一次」也判成重复，而脚本可能压根不幂等。用 contextvars 承载（一次 run 里多个节点可能并发跑）。
- [x] 五个配置项：`studio_exec_addr` / `_required` / `_timeout_ms` / `_max_timeout_ms` / `_max_script_chars`。

### Task 3: `skill_exec` 工具与脚本执行

**Files:**
- Create: `app/core/skill_exec.py`
- Modify: `app/core/tools.py`、`app/core/agent_runner.py`
- Create: `tests/test_skill_exec.py`

- [x] `validate_script_path()`：复用 §22.7 的 `validate_resource_path`，并要求在 `scripts/` 目录下。
- [x] `validate_argv()`：`..` 段、绝对路径、反斜杠、空字节全拒；`--flag=value` 只看 `=` 之后那段。
- [x] `read_script()`：从**数据库**读脚本正文（技能已经不住在磁盘上了），二进制资源拒绝执行，超长拒绝。
- [x] `exec_skill_script()`：白名单检查**先于**存在性检查（不该跑的技能，连「它在不在」都不该告诉模型）。
- [x] `skill_exec` 作为**可选工具**注册（`OPTIONAL_TOOLS` + `set_optional_tools_enabled()`）；studio 不可用时配置期拒的错误信息说清是「studio 未启用或连不上」，而不是混在「未知工具」里。
- [x] `agent_runner.py` 每次工具调用**重设**执行上下文（不复位的话下一个 tool_call 会继承上一个的 id，studio 的去重窗口会把第二次静默吞掉）。

### Task 4: 启动期兼容性检查与工具注册联动

**Files:**
- Modify: `app/core/studio.py`、`app/main.py`、`app/core/tools.py`
- Create: `tests/test_studio_startup.py`

- [x] `init_studio()` 三态：未配置 → 未启用；连上 → 启用；连不上 → `required` 为真时抛 `StudioStartupError`（**启动失败**），为假时只记警告。
- [x] studio 起来了但 `ready=false` 也当作不可用（队列满时不能拿来执行脚本）。
- [x] lifespan 里 `await init_studio()` → `set_optional_tools_enabled(enabled)`。
- [x] 关停时 studio 通道排在**编排器之后**——编排器要取消还在跑的 run，那些任务可能正在调 studio，通道先关了调用会变成「连不上」。

### Task 5: 脚本输出回灌前的敏感词兜底

**Files:**
- Modify: `app/core/tools.py`
- Create: `tests/test_tool_output_sensitive.py`

- [x] 抽出 `_screen_tool_output()` 供 MCP 与 skill_exec **共用**一条规矩。
- [x] **失败的脚本其 stderr 同样要筛**——它照样会被回灌给模型。

### Task 6: studio 服务侧参考实现

**Files:**
- Create: `services/__init__.py`、`services/studio/{__init__,config,executor,server,__main__}.py`
- Create: `tests/test_studio_service.py`

- [x] `executor.py`：建临时目录 → 写脚本 → 建**独立进程组** → 起进程 → 读输出 → 等退出/超时 → 清理（**所有终态都做**）。
- [x] 杀的是**整个进程组**而不是进程（只杀父进程的话孙进程会继续跑、占内存、持有文件）。SIGTERM 给 2 秒优雅退出，还活着就 SIGKILL。
- [x] 输出超限**停止读取**并杀掉进程组——只截断但继续读的话，一个 `cat /dev/urandom` 就能把 studio 的内存吃光。
- [x] 环境变量白名单（PATH / HOME / LANG / TZ / PYTHONUNBUFFERED / PYTHONDONTWRITEBYTECODE）；POSIX 上再设 RLIMIT。
- [x] `server.py`：**gRPC 成功 ≠ 脚本成功**；并发闸门 + 等待队列（满了回 RESOURCE_EXHAUSTED）；**排队时间计入时限**；`request_id` 去重窗口（60 秒）。

**实现时修掉的两个真 bug：**

1. **超时路径把 gather 一起取消了**——`wait_for` 超时会取消被 await 的 gather，于是 gather 里的 `proc.wait()` 也被取消，后面再 await 它就抛 CancelledError。改成显式管理三个 task。
2. **输出超限被误判成超时**——超限时读管道的任务抛了异常，但等进程的任务还在 pending，两个分支表象一样。必须**先看异常、再看 pending**。

### Task 7: 端到端与故障注入

**Files:**
- Create: `tests/test_studio_e2e.py`

- [x] **真 studio**（同进程 gRPC 服务 + 真子进程）+ **真 uvicorn**（不是 ASGITransport——SSE 需要真 socket 的流式语义）+ **假 LLM**。
- [x] 全链路：脚本真跑 → 输出进 `tool_result` → 进下一轮 `role=tool` 消息 → run done。
- [x] 脚本 `exit 3`：**脚本失败**不是 studio 故障，stderr 照样回灌，run 仍然 done。
- [x] 超时：模型给 800ms，脚本睡 30 秒，真的被杀掉了。
- [x] 输出超限 → `limited` + 截断标记，截断内容还在。
- [x] studio 被 kill → 工具失败（「连不上 studio」），**run 不失败**（§17.5）。
- [x] 三种非法请求（路径穿越 / 不在 `scripts/` / argv 含 `..`）→ studio 的 `exec_calls` **一次都没涨**。
- [x] 技能没绑到 Agent → 白名单先于存在性检查拒绝，`exec_calls` 也是 0。
- [x] studio 被占满 → `RESOURCE_EXHAUSTED` 翻译成「忙不过来」，run 仍然 done。

### Task 8: 部署产物

**Files:**
- Create: `services/studio/Dockerfile`、`services/studio/requirements.txt`
- Create: `services/studio/healthcheck.py`、`services/studio/reaper.py`
- Create: `deploy/k8s/studio-{deployment,service,networkpolicy}.yaml`、`deploy/k8s/README.md`
- Create: `deploy/docker-compose.yml`
- Modify: `app/core/studio.py`、`app/config/settings.py`、`.env.example`

- [x] 镜像：python / bash / node 三运行时，版本钉死，**零凭据**，非 root，只装 `grpcio` 一个依赖。
- [x] `STUDIO_EXEC_LB_POLICY`（默认 `round_robin`）：客户端建通道时加 `dns:///` 前缀。**Service 侧也必须 headless**——两边配对才有意义，单改一边等于没改（skill-service §13.4）。
- [x] 探针用 **exec** 而不是 K8s 的 `grpc:` 探针——后者发的是标准健康检查协议 `grpc.health.v1.Health/Check`，跟契约里的 `Health` 对不上。
- [x] 启动巡检：按 **cwd 在临时目录里**判定残留进程，不按进程名匹配；没有 `/proc` 的平台上不做任何事。
- [x] **优雅停机先摘 readiness 再 drain**（§13.5）。修掉一个更要命的 bug：**取消 grpc 的 `wait_for_termination()` 那个 task 会让后面的 `server.stop()` 直接抛 `CancelledError`**，停机路径整个断掉。

**本环境不可验证**：K8s 清单没在真实集群上跑过（本机没有 Docker，也没有集群）。镜像构建、探针在镜像里跑通、只读 rootfs 下的 tmpfs 挂载、NetworkPolicy 是否真拦住出向、headless + round_robin 是否真把流量摊开——这五条都只做了「对照文档逐条写」。

### Task 9: 文档回写

**Files:**
- Modify: `docs/design.md`（§22.8.8 / §22.8.9 / §22.12 / §22.13）
- Modify: `docs/skill-service.md`（§19）
- Modify: `README.md`（路线图 / 项目结构 / 新增「脚本执行：studio」一节）
- Create: 本文件

- [x] 验收项逐条勾选，并标出钉它的测试。
- [x] 实施清单逐条勾选，**未做的照实标未做**（指标与结构化日志、`isolated` 档；
      容器级 OOM 已在本轮补上并实测）。
- [x] 新增的配置项写进两份文档。

---

## 终态

- 全量回归：**546 passed / 22 skipped**（Phase 2 终态是 428 / 19，新增 118 个 studio 相关用例；跳过里 3 个是本机没有 bash 与没有 `/proc` 导致的）。
- docstring 覆盖率 100%（`scripts/check_docstrings.py`，扫描范围含 `services/`）。
- 契约一致性：`scripts/check_proto_contract.py` 退出码 0。

---

## 补充：真实 Docker 环境实测（2026-11-03）

> 在一台真机器（Alpine 3.24 / Docker 29.9.2 / 4 vCPU / 1884 MB）上把整套东西跑起来，
> 用宿主机的 MySQL 与 Redis，编排用 `deploy/docker-compose.lab.yml`。
> 这一节记的是**实测**，不是设计意图。

### 实测到了什么

| 项 | 结果 |
| --- | --- |
| 迁移链从零跑通 | 真 MySQL 上 `alembic upgrade head` 建出全部 14 张表 |
| `skill_exec` 真链路 | 真 DB → 真 gRPC → 真 studio 容器 → stdout 回灌；web_agent 进程内没起任何子进程 |
| web_agent 侧三道校验 | 20 条用例全过；校验不过的请求**根本没发出去 studio** |
| 容器加固 | 11 项全过（只读 rootfs / 非 root / caps 全 0 / NoNewPrivs / 禁出网 / 零凭据 / env 白名单 …） |
| 进程组隔离 | 超时后整个进程组被杀，子进程不再残留 |
| 队列闸门 | 并发 2 + 队列 4，发 12 个 → 接受 6、拒 6（`RESOURCE_EXHAUSTED`） |
| studio 宕机 | `docker stop` → web_agent 侧「连不上 studio」，**不抛异常打断 run** |

### 实测挖出来的七个 bug（全部已修 + 加了回归守卫）

| # | 现象 | 根因 | 守卫 |
| --- | --- | --- | --- |
| 1 | studio 容器 `ModuleNotFoundError: No module named 'google'` | 生成桩 import `google.protobuf`，但镜像只装了 `grpcio` | `tests/test_studio_deps.py` |
| 2 | 只读 rootfs 下 `run.py` 崩在写 `.env` | `ensure_env_file()` 无条件写盘 | 捕获 `OSError` |
| 3 | MySQL `1101: BLOB/TEXT/JSON column 'prompt' can't have a default value` | 迁移给 TEXT 列带了字面量 `server_default` | `tests/test_migrations.py::test_no_text_or_json_column_has_server_default` |
| 4 | MySQL `1067: Invalid default value for 'requested_at'` | SQLAlchemy 渲染 `ALTER` 时把 `sa.func.now()` 输出成 `now()`，MySQL 要求表达式默认值带括号 | `tests/test_migrations.py::test_mysql_ddl_keeps_function_defaults_parenthesized` |
| 5 | 脚本间歇性 `exit_code=-6`（SIGABRT）、stdout/stderr 全空 | **在 gRPC 进程里 fork**：gRPC 的 atfork 处理器在「别的线程正在 gRPC 里」时放弃清理，子进程带坏 epoll 状态去 exec，exec 前被断言 abort | `tests/test_studio_runtime_guards.py` |
| 6 | 容器里攒僵尸 | 脚本被杀后它起的子进程被重新挂到 PID 1 底下，python 不回收孤儿 | `tests/test_studio_runtime_guards.py`（镜像用 tini 当 PID 1） |
| 7 | 真模型连猜十来个技能 code，全被白名单拒掉，最后「工具调用超过 5 轮」失败 | 工具 schema 里只有一句举例（「例如 financial-report」），没告诉模型**实际**可用的 code；技能正文里也不一定写着自己的 code | `tests/test_skill_exec.py::test_skill_param_lists_the_actual_bound_codes` |

第 3、4 条暴露了一个方法论问题：**SQLite 上的迁移测试抓不到服务端会拒绝的 DDL**。
现在的守卫是「AST 静态扫」+「**离线渲染 MySQL DDL** 再检查」两条——后者用
`alembic upgrade head --sql`，连数据库都不需要，却能看见发给 MySQL 的每一句 SQL。

第 7 条暴露的是另一个：**假 LLM 的测试替模型做了它做不到的事**。假 LLM 被写死成
「知道正确的技能 code」，于是这个缺口在本地永远看不见——换成真模型，第一轮就撞上了。

### 真实模型端到端（四个场景全过）

真 Docker + 真 MySQL + 真 MiniMax（`MiniMax-M3.1-Flash`，M Plan 专用 key，
端点 `https://api.minimax.cn/v1`）：

| 场景 | 工具结果 | run |
| --- | --- | --- |
| happy | `ok=true`、`exit_code: 0`、stdout 含 `hello from studio container` | ✅ |
| timeout | `ok=false`、`status: timeout`、`exit_code: -1`、`已杀掉进程组` | ✅ 没失败 |
| busy | `ok=false`、`studio 忙不过来（studio:9000）：等待队列已满（4）` | ✅ 没失败 |
| down | `ok=false`、`连不上 studio（studio:9000）：… Connection refused` | ✅ 没失败 |
| oom | `ok=false`、`status: limited`、`exit_code: -1`、`脚本被 OOM killer 杀掉…：内存超出容器限额` | ✅ 没失败 |

happy 场景里模型把工具返回的退出码与 stdout **逐字转述**了——这就是 §22.13 那条
验收项在真部署上的证据。oom 场景（后补）里，模型把 `LIMITED` 原样转述并读懂了原因。

### 还没验到的

- **K8s 侧**：NetworkPolicy、headless + `round_robin` 摊流量、drain 与
  `terminationGracePeriodSeconds` 的配合——依赖集群行为，Docker 上无法等效验证。
- **studio 自己成为最大占用者时的 OOM**：实测里被杀的永远是吃内存的脚本、studio 活着；
  反过来的情况没测（也不该靠 `oom_score_adj` 兜——它只加在脚本进程上）。

---

## 遗留与已知限制

1. **K8s 清单未在真实集群验证**（Docker 侧已实测，见上）。
2. **studio 服务侧是参考实现**：没有指标导出、没有结构化日志、没有多档隔离。
3. **`isolated` 隔离档未实现**：当前只有 `fast` 档，同一容器内先后两次执行之间理论上存在残留。
4. **容器级 OOM 已补**：§13.2 的判定已实现并在真容器里验过（256 MB 限额 + 申请 1 GB
   → `LIMITED` + `exit_code=-1` + 「内存超出容器限额」，studio 存活，cgroup 的
   `oom_kill` 计数 +1）。剩下的尾巴是「studio 自己成为最大占用者」那一种情形。
5. **本机没有 bash**：studio 的 bash 运行时在测试里被跳过（生产镜像里是齐的）。
6. **`oom_score_adj` 只能写 `/proc`**：`prctl` 那条路在容器里返回 0 却不生效——
   实现走 proc 路径并回读验证（skill-service §13.2）。
7. **K8s 侧未实测**：NetworkPolicy、headless 摊流量、drain 与 grace period 的配合。
