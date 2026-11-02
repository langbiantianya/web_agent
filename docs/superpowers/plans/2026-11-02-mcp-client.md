# MCP 客户端（V4 Phase 2）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 MCP 接成本项目的**第二个工具来源**——外部服务器声明的 schema 与执行器天然同源，于是「模型能调的」与「服务端能执行的」在快照层面仍然对得上。闭环本身（§17.3）一个字不改。

**Architecture:** 两张表（`mcp_server` 注册表 / `mcp_tool` 工具目录快照）承载配置与发现结果；`app/core/mcp/` 六个模块各管一段（命名映射、结果映射、传输、连接管理、目录发现、传输策略）；`app/core/tools.py` 的四个对外函数按 `mcp__` 前缀分流，MCP 工具与内置工具共用同一张「工具名即契约」的表；路由层提供管理员专属的服务器管理接口。

**Tech Stack:** Python 3.11+ / FastAPI / SQLAlchemy 2.0 async / Alembic / 官方 MCP Python SDK `mcp>=2.3`（stdio + Streamable HTTP）/ pytest-asyncio（`asyncio_mode = auto`）

## 全局约束

- **三方方言**：MySQL / PostgreSQL / SQLite 同一份代码。建表主键必须写 `sa.BigInteger().with_variant(sa.Integer, "sqlite")`（SQLite 只有 `INTEGER PRIMARY KEY` 才自增）。
- **JSON 列**用 `app.models.base.JSON_COL`；**毫秒时间列**用 `DATETIME3`。
- **只做客户端**：不做 MCP 服务器，不接 resources / prompts / roots / sampling / elicitation。
- **只做两种传输**：stdio 与 Streamable HTTP。旧的 HTTP+SSE 双端点传输**不实现**（2025-03-26 废弃）。
- **写权限**：全部 MCP 接口都是管理员专属（共享密钥 `ADMIN_KEY` + `X-Admin-Key` 头，**fail-closed**）。
- **读宽松、写严格**：Agent 绑一个没发现过的 MCP 工具 → 保存期 400；读路径遇到快照里没有的名字一律丢弃，**绝不让列表接口 500**。
- **工具名超长不导入不截断**：截断会把两个工具撞成同名（OpenAI 函数名上限 64 字符）。
- **工具结果是不可信输入**：必须过 §20 的敏感词前置过滤，命中则**不回灌**，一路抛上去。
- **注释与 docstring 用中文**，解释「为什么」而不是「做了什么」。
- **闭环不改**：`app/core/agent_runner.py` 只加事件字段与白名单口径，不动「模型要工具 → 执行 → 回灌 → 继续」的形状。

## Phase 拆分说明

| Phase | 内容 | 计划 |
| --- | --- | --- |
| Phase 1 | Skill 基础：表 + CRUD + 导入导出 + 审计 + 提示词注入 + `skill_resource` 工具 | `2026-11-02-skill-basics.md` |
| **Phase 2（本计划）** | MCP 客户端：连接管理、工具目录快照、`mcp__` 命名分发、API | 本文件 |
| Phase 3 | studio 服务 + `skill_exec`：proto、gRPC 客户端、容器部署 | 待 Phase 2 落地后写 |

---

### Task 1: 数据模型与迁移

**Files:**
- Create: `app/models/mcp_server.py`、`app/models/mcp_tool.py`
- Modify: `app/models/__init__.py`
- Create: `alembic/versions/0007_mcp.py`
- Modify: `tests/test_migrations.py`、`tests/test_postgres_compat.py`

- [x] `McpServer`：`code`（业务主键，**不是**协议里的 `serverInfo.name`——后者是服务端自报的，不同服务器可能重名）、`transport`、`command`/`args`/`env`、`url`/`headers`、`allow_tools`、`timeout_sec`、`enabled`、`version`。
- [x] `McpTool`：`server_id` + `qualified_name`（唯一键）+ `tool_name` + `description` + `input_schema` + `output_schema` + `annotations`。
- [x] `env` / `headers` 的**值加密存储**（与 `agent.api_key_cipher` 同一套 Fernet），响应里只回显**键名**。
- [x] 迁移 revision `e8b4c2f61d93`，down_revision `d5a91c3b7e42`（接在 0006 后面）。

### Task 2: 命名映射与结果映射

**Files:**
- Create: `app/core/mcp/naming.py`、`app/core/mcp/mapping.py`

- [x] `build_qualified_name(server_code, tool_name)` → `mcp__<server_code>__<tool>`；超长或含非法字符**抛错**（不截断）。
- [x] `parse_qualified_name` / `is_mcp_name`。
- [x] `map_call_result(result, max_chars)` → `(ok, text)`：多段文本用 `\n` 连接；`structuredContent` 兜底；`is_error` → `ok=False`；图片/音频/资源链接 → **占位说明**而不是空串；`EmbeddedResource` 里的 text 资源提取出来用。

### Task 3: 传输层与出网策略

**Files:**
- Create: `app/core/mcp/transport.py`

- [x] `open_transport(server)`：stdio → `stdio_client`；http → `httpx2.AsyncClient` + `streamable_http_client`（**httpx2 是 MCP SDK 2.x 用的 httpx 分支**，与本项目自己的 httpx 1.x 并存）。
- [x] `check_command_allowed`：命令路径必须在 `MCP_ALLOWED_COMMAND_PREFIXES` 内，**默认为空 = 禁止 stdio 注册**（fail-closed）。
- [x] `check_url_allowed`：协议白名单 + 主机白名单 + 私网网段默认拒绝。

### Task 4: 连接管理器与目录快照

**Files:**
- Create: `app/core/mcp/manager.py`、`app/core/mcp/catalog.py`
- Create: `app/repo/mcp_repo.py`

- [x] 每台服务器一条**专属后台任务**跑连接（SDK 的 `ClientSession` 是 anyio 任务组里的上下文，进入与退出必须在同一个任务里），外面用 `asyncio.Queue` 当命令通道。
- [x] 并发靠「每个请求起一个 task」实现 + `Semaphore` 并发闸门；断了按指数退避重连重试。
- [x] `list_tools` **翻完分页**（`nextCursor`），并防住循环 cursor。
- [x] `refresh_catalog` **整体替换**快照（增量合并会留下服务器已删的工具，那些工具会一直躺在勾选框里，点下去必然调用失败）。
- [x] **可用性判定 = 「没被证明不可用」**，不是「已连接」——连接是惰性的，按「已连接」判定会形成「模型看不见 → 调不到 → 永远连不上」的死结。

### Task 5: 接入工具注册表（四处分流）

**Files:**
- Modify: `app/core/tools.py`、`app/core/agent_runner.py`
- Modify: `app/api/routes/agent.py`

- [x] `normalize_tool_names(raw, known_mcp)` / `known_tool_names(raw, known_mcp)` 按 `mcp__` 前缀分流。
- [x] `build_openai_tools` 改 **async**（MCP 工具的 schema 要从快照查）；**离线服务器的工具不展开**（§21.9）。
- [x] `execute_tool` 新增 `_execute_mcp_tool` 分支；MCP 结果过 `screen_text`，命中敏感词 → 抛 `SensitiveContentError` **不回灌**。
- [x] Agent 路由四条（create / list / get / update）接入 `known_mcp`。

### Task 6: API 路由与工具目录

**Files:**
- Create: `app/schemas/mcp.py`、`app/api/routes/mcp.py`
- Modify: `app/api/router.py`、`app/api/routes/tool.py`

- [x] `/api/mcp/servers` 全套：list / create / get / update / delete / refresh / tools，**全部 `AdminDep`**。
- [x] 保存期就校验命令白名单与 SSRF 策略。
- [x] `GET /api/tools` 加 `source`（builtin / mcp）与 `server` 字段。

### Task 7: 事件增强（§21.12）

**Files:**
- Modify: `app/models/enums.py`、`app/core/agent_runner.py`

- [x] `tool_call` 事件加 `source`；`tool_result` 事件加 `source` / `server` / `durationMs`。
- [x] 新增 `mcp_unavailable` 事件（**非终止**）：服务器被判为不可用时发一次。

### Task 8: 测试

**Files:**
- Create: `tests/fixtures/mcp_server_stdio.py`
- Create: `tests/test_mcp_naming.py`、`tests/test_mcp_transport.py`、`tests/test_mcp_api.py`、`tests/test_mcp_e2e.py`

- [x] `tests/fixtures/mcp_server_stdio.py`：用官方 SDK 起的**真的** MCP 服务器（`echo` / `boom` / `binary` 三个工具）。
- [x] e2e 覆盖 §21.15 的验收清单（见下）。

---

## 踩到的坑

### ① SDK 的 `errlog` 要的是**真文件描述符**，不是一个能写字符串的对象

`stdio_client(params, errlog=...)` 把 `errlog` 直接当 `subprocess` 的 `stderr=` 用，
而 `subprocess` 会调 `errlog.fileno()`。最初的实现是个只会 `write()` 的小对象，
结果连上就炸：

```text
'McpStderrWriter' object has no attribute 'fileno'
```

**改法**：开一根 OS 管道——写端交给子进程（`fileno()` 返回它），读端由后台线程读出来、
按行记进 logger。这样子进程的输出仍然被收编成带服务器 code 的结构化日志，而不是
直接落到服务进程的 stderr 上。管道与线程在连接关闭时一起收掉。

### ② 保存期做 DNS 解析，会把**最该用的那类场景**挡在门外

最初的实现在**保存期**就解析 URL，解析失败 → 400。测试环境没有 DNS，于是所有
`https://xxx.example.com` 的注册全部失败。

更要命的是：DNS 解析失败说明的是「这个主机**此刻**查不到」，不是「这个地址危险」。
而「此刻查不到」在实际部署里非常常见——服务还没上线、内外网分裂 DNS
（split-horizon）、DNS 抖动。拿它卡保存期，等于把内网 MCP 服务器（最典型的用例）
挡在门外。

**改法**：把检查拆成两档。

| 时机 | `require_dns` | 解析失败时 |
| --- | --- | --- |
| **保存期**（API 写接口） | `False` | 放行，记一条日志 |
| **连接期**（`open_transport`） | `True` | 拒绝 |

安全上没有缺口：
- **IP 字面量**（`http://127.0.0.1:8931/mcp`）不需要 DNS 就能判定——那是最常见的
  SSRF 载荷，保存期照样拒；
- 真正连上去之前会**再查一次**，那时不容情。

### ③ 命令前缀匹配必须跨平台

原来的写法是 `command.startswith(prefix.rstrip("/\\") + "/")`——拼出来的是正斜杠，
在 Windows 上永远匹配不上 `C:\...\python.exe`。

**改法**：先把路径统一成「正斜杠 + 无末尾分隔符」的形状再比。这样前缀也变成
**目录级**语义：`/opt/tools` 命中 `/opt/tools/run`，但不命中 `/opt/tools-evil/run`
（后者只是名字长得像，不在那个目录里）。

### ④ 不要从限定名里反推 server code

`servers.get(row.qualified_name.split("__")[1])` 看着能用——`server_code` 的正则
不允许出现 `_`，所以按第一个 `__` 切一定切对。但这是**依赖一个约定**的写法：
哪天 code 的正则放宽了（比如允许下划线），反推出来的就是另一台服务器，而且
**不会报错**——只是工具莫名其妙地不展开。

**改法**：用 `mcp_tool.server_id` 关联服务器表。没有前提，没有隐式约定。

### ⑤ `known_mcp=None` 在「写严读宽」里有**两种**含义

- `normalize_tool_names(raw, known_mcp=None)` → **拒**掉所有 `mcp__` 名字（写路径，
  没查库就等于「没有可用的 MCP 工具」）；
- `known_tool_names(raw, known_mcp=None)` → **保留**所有 `mcp__` 名字（读路径，
  宽容优先）。

这不是笔误，是两套方向刻意相反。但它必须在 docstring 里写死，否则后来的人
「顺手统一一下」就会把读路径的宽容毁掉。

### ⑥ pytest 的库是 session 级建表且**用例之间不清理**

同一个 code 第二次注册 → 409。MCP 这批用例还要起**真的子进程**，跨用例残留的
连接状态（`is_available` 是进程内单例里的状态）也会串味。

**改法**：fixture 里先删同名服务器再注册；每个用例后 `await mcp_manager.close_all()`
——stdio 子进程是**真的进程**，不回收会在测试机上攒出一堆孤儿。

---

## 验证结果

```
pytest -q                                   → 全部通过
scripts/check_docstrings.py                 → 缺 docstring: 0
```

新增测试（4 个文件）：

| 文件 | 覆盖 |
| --- | --- |
| `tests/test_mcp_naming.py` | 命名映射（形状、超长、点号、往返）与结果映射（文本、多段、`isError`、`structuredContent`、图片/音频/资源链接占位、限长） |
| `tests/test_mcp_transport.py` | 命令白名单（默认禁止、目录级前缀、相似前缀）、SSRF（协议、私网、IPv6 回环、主机白名单、DNS 解析到内网、保存期 vs 连接期的解析失败差异） |
| `tests/test_mcp_api.py` | 七条接口的 401 门禁、CRUD、传输与字段不匹配、命令白名单、SSRF、凭据只回显键名、code 重复 409、Agent 绑定校验、`GET /api/tools` 的 `source` / `server` |
| `tests/test_mcp_e2e.py` | **真的**起 MCP 子进程：发现 → 快照 → 绑定 → 模型调用 → `role=tool` 回灌 → 离线不展开 → `isError` 不失败节点 → 图片占位 → 敏感词不回灌 → `mcp_unavailable` 事件 |

§21.15 验收清单的对应：

| 验收项 | 状态 | 钉在哪 |
| --- | --- | --- |
| `GET /api/tools` 同时列内置与 MCP，条目带 `source`，按服务器分组 | ✅ | `test_tools_endpoint_lists_builtin_and_mcp` |
| 配一个未注册的 `mcp__xxx__yyy` → 400，且说清是「服务器未发现工具」 | ✅ | `test_agent_cannot_bind_unknown_mcp_tool` |
| 真实 MCP 服务器 → 刷新目录 → 快照出现对应工具行，`qualified_name` 合约束 | ✅ | `test_refresh_discovers_tools_into_snapshot` |
| 真实调用：模型要工具 → 执行 → 回灌 → 模型继续；断言第二轮带 `role=tool` | ✅ | `test_mcp_tool_is_executed_and_fed_back_to_model` |
| 停掉服务器 → 工具不再展开给模型，Agent 仍能跑完 | ✅ | `test_offline_server_tools_are_not_expanded` / `test_agent_still_runs_when_its_mcp_server_is_offline` |
| `isError: true` → 节点不失败，错误文本回灌，`tool_result` 的 `ok` 为 false | ✅ | `test_mcp_is_error_does_not_fail_the_node` |
| 返回图片 → 回灌文本是占位说明，不是空串 | ✅ | `test_mcp_image_result_becomes_placeholder` |
| 工具结果命中敏感词 → 不回灌，run 以敏感内容错误收尾 | ✅ | `test_sensitive_tool_result_is_not_fed_back` |
| 子进程把普通日志打到 stdout → 协议不乱 | ⚠️ | 见「已知限制」 |
| 重启服务后快照仍在，离线服务器的 Agent 不会调它 | ✅ | 快照存在库里（进程重启不受影响）；离线由 `test_offline_server_tools_are_not_expanded` 覆盖 |

---

## 已知限制

1. **子进程往 stdout 写普通日志这条没有专门测**。协议层不会因此乱（SDK 自己只读
   stdout 的协议帧、服务端随便 `print` 会污染帧流），但本轮没有为此加一个
   「脏 stdout」的 fixture 服务器。
2. **Streamable HTTP 传输没有 e2e**。传输层与出网策略有单测，但「真的连一个
   Streamable HTTP 服务器」这件事本轮没有跑通——测试环境没有可用的远程 MCP 服务器，
   也没有起一个本地 HTTP 版 MCP 服务器。stdio 那条路是完整的。
3. **连接是进程内的**。stdio 子进程无法跨实例（物理限制）；多实例部署时每个实例
   都会各起一份子进程。Streamable HTTP 的握手期会话亲和性沿用 §21.14 第 1 条的
   已知限制。
4. **空闲连接靠 `close_idle` 回收，但没人定时调用它**。管理器提供了
   `close_idle()`，当前**没有挂到定时任务上**——需要接进应用的周期任务
   （或进程退出钩子）才会真的回收。这是本轮留的一个明确的缺口。
5. **`mcp_manager` 是进程内单例**，多实例部署下每个实例各有一份「可用性」状态：
   实例 A 连不上标了不可用，实例 B 那边仍认为可用（于是照常展开工具，调用时才失败）。
   不影响正确性（失败会回灌给模型），但前端的「在线状态」在多实例下不准。
