# 多 Agent 协作 Web 服务设计文档（Python + MySQL / PostgreSQL）

> 版本：V4.0　|　状态：V3.4 已实现；V4（MCP 工具 §21 / Skill 指令包 §22）设计完成，实现待排期　|　技术基础：Python 3.11+ / FastAPI / SQLAlchemy 2.0 async / MySQL 8 / PostgreSQL 13+

> 本文归属 `app/` 模块，记录主服务的总体架构、通信协议、编排引擎与演进设计。

---

## 目录

1. [项目目标与设计原则](#1-项目目标与设计原则)
2. [技术选型](#2-技术选型)
3. [总体架构](#3-总体架构)
4. [MySQL 持久化设计](#4-mysql-持久化设计)
5. [SSE 通信设计](#5-sse-通信设计)
6. [Agent 编排引擎](#6-agent-编排引擎)
7. [RunHub 与总线扩展](#7-runhub-与总线扩展)
8. [API 设计](#8-api-设计)
9. [异常处理与边界](#9-异常处理与边界)
10. [项目目录结构](#10-项目目录结构)
11. [演进路线](#11-演进路线)
12. [实现落地说明](#12-实现落地说明)
13. [V2：Redis 多实例](#13-v2redis-多实例)
14. [V2-b：用 LangGraph 替换自研编排器](#14-v2-b用-langgraph-替换自研编排器)
15. [V3：DAG + 条件边](#15-v3dag--条件边)
16. [V3.1：人工审批](#16-v31人工审批)
17. [V3.2：Agent 工具调用闭环](#17-v32agent-工具调用闭环)
18. [性能与容量](#18-性能与容量)
19. [PostgreSQL 适配](#19-postgresql-适配)
20. [V3.4：敏感词前置过滤](#20-v34敏感词前置过滤)
21. [V4：MCP 工具（外部工具来源）](#21-v4mcp-工具外部工具来源)
22. [V4：Skill 指令包（可复用的领域知识）](#22-v4skill-指令包可复用的领域知识)
---

## 1. 项目目标与设计原则

### 1.1 目标

构建一个轻量 Web 服务，支持：

- 通过 MySQL 动态配置多个 Agent（提示词、模型、参数、工具），无需改代码即可上线新 Agent
- 多个 Agent 按可配置的 Workflow 协作完成一次任务
- 大模型统一走 OpenAI 兼容协议（`/v1/chat/completions`），可对接 OpenAI / DeepSeek / Qwen / vLLM 等
- 前端通过 SSE 接收流式输出，具备心跳保活能力
- 网络中断或页面刷新后，可通过 `Last-Event-ID` 或 `last_seq` 恢复上下文并继续接收后续吐字
- 会话、消息、Run、事件全部持久化到 MySQL，支持审计与恢复

### 1.2 设计原则

| 原则 | 说明 |
| --- | --- |
| 生成与连接解耦 | LLM 生成任务独立于 HTTP 连接运行，连接断开不中断生成，这是续传的前提 |
| 事件即日志 | 所有流式事件落库并带单调递增 `seq`，数据库即回放源 |
| 配置即数据 | Agent / Workflow 全部存 MySQL，运行期热加载 |
| 全异步纪律 | 所有 I/O 必须使用异步库，禁止同步阻塞调用卡死事件循环 |
| 单实例可跑，多实例可扩 | 核心状态在 MySQL，进程内总线通过 Protocol 预留 Redis 扩展位 |

---

## 2. 技术选型

| 层次 | 选型 | 理由 |
| --- | --- | --- |
| Web 框架 | FastAPI | 原生异步、Pydantic 校验、SSE 支持完善 |
| SSE 实现 | `sse-starlette`（`EventSourceResponse`） | 自动心跳、`Last-Event-ID` 注入、Nginx 缓冲头处理 |
| ORM | SQLAlchemy 2.0 async | 类型安全、`AsyncSession` 非阻塞 |
| MySQL 驱动 | `asyncmy` | Cython 重写协议核心，大批量查询性能优于纯 Python 驱动 |
| 数据库迁移 | Alembic | 版本化迁移，配合 SQLAlchemy 元数据 |
| LLM SDK | openai Python SDK（`AsyncOpenAI`） | `base_url` 可覆盖，直接兼容 OpenAI 协议 |
| Agent 编排 | LangGraph（V2 起） | V1 自研线性流水线；V2 把控制流交给图，节点逻辑保留 |
| 事件总线 | `BusBackend` Protocol，Redis Pub/Sub / 进程内队列双实现 | 同一份 SSE 代码跑单实例与多实例 |
| 分布式扩展 | Redis（V2） | 回放源换 Stream、跨实例事件换 Pub/Sub、归属换租约 |

> **实现修正**：原文示例中的 `from fastapi.sse import EventSourceResponse` 有误，FastAPI 并未内置 SSE 模块。正确导入为 `from sse_starlette.sse import EventSourceResponse, ServerSentEvent`。

---

## 3. 总体架构

### 3.1 分层架构图

```mermaid
graph TB
    subgraph Client["前端 / 客户端"]
        FE["React / 原生 JS<br/>EventSource + last_seq"]
    end

    subgraph API["API 层 FastAPI"]
        AR["agent / workflow 路由<br/>配置管理"]
        CR["conversation 路由<br/>会话与消息"]
        RR["run 路由<br/>SSE / cancel"]
        AU["auth 路由<br/>SSE ticket"]
        SSEW["sse_writer<br/>回放 + 实时合并去重"]
    end

    subgraph Core["核心编排层"]
        ORCH["orchestrator<br/>run 生命周期 + 调度"]
        HUB["RunHub<br/>seq 分配 + 事件广播"]
        RUNNER["AgentRunner<br/>AsyncOpenAI 流式调用"]
        WRITER["EventWriter<br/>批量落库 50ms / 20 条"]
        BUS["BusBackend<br/>InMemory / Redis"]
    end

    subgraph Data["持久化层"]
        REPO["repo 层<br/>数据访问"]
        DB[("MySQL 8<br/>agent / workflow / conversation<br/>message / run / run_step / run_event")]
    end

    subgraph LLM["外部模型服务"]
        OA["OpenAI / DeepSeek<br/>Qwen / vLLM<br/>OpenAI 兼容协议"]
    end

    FE -->|"POST /messages"| CR
    FE -->|"GET /stream (SSE)"| RR
    CR --> ORCH
    ORCH --> RUNNER
    ORCH --> HUB
    RUNNER -->|"stream=True"| OA
    HUB --> WRITER
    HUB --> BUS
    SSEW --> BUS
    SSEW --> REPO
    WRITER --> REPO
    ORCH --> REPO
    AR --> REPO
    CR --> REPO
    REPO --> DB
```

### 3.2 一次 run 的执行时序

```mermaid
sequenceDiagram
    participant C as 前端
    participant API as FastAPI
    participant O as Orchestrator
    participant R as AgentRunner
    participant L as LLM 服务
    participant H as RunHub
    participant DB as MySQL

    C->>API: POST /conversations/{id}/messages
    API->>DB: INSERT run(status=pending)
    API->>DB: INSERT message(role=user)
    API-->>C: { runId, streamUrl } 立即返回
    Note over API: asyncio.create_task 后台执行<br/>生成不绑定请求生命周期
    API->>O: execute_run(run_id)

    O->>DB: UPDATE run SET status=running
    O->>DB: 加载会话历史 + Agent 配置
    loop 每个 workflow 节点
        O->>H: emit(node_start)
        H->>DB: 批量写 run_event
        H-->>C: 广播 token（经 SSE 订阅者）
        O->>R: run(context, user_input)
        R->>L: chat.completions.create(stream=True)
        loop 每个 chunk
            L-->>R: delta.content
            R->>H: emit(token)
            H->>DB: 批量写 run_event
            H-->>C: 实时推送到 SSE 连接
        end
        O->>DB: finish_step + INSERT message
        O->>H: emit(node_end)
    end
    O->>H: emit(done)
    O->>DB: UPDATE run SET status=done, last_seq
    H-->>C: done 后前端关闭 EventSource
```

### 3.3 执行模型关键点

1. `POST /messages` **只创建 run 并立即返回 `runId`**，生成在后台 `asyncio.create_task` 中执行，不绑定请求生命周期。
2. `AgentRunner` 加载会话历史、Agent 配置、上游节点输出，拼装 messages 后调用 `AsyncOpenAI`。
3. 每个 token 通过 `RunHub.emit` 分配 `seq`、写入 `run_event`、广播给订阅者。
4. 节点失败按指数退避重试 2 次；仍失败则 `run.status = error`，发 `error` 事件并结束。

---

## 4. MySQL 持久化设计

### 4.1 持久化边界原则

| 类别 | 是否持久化 | 说明 |
| --- | --- | --- |
| Conversation / Message | ✅ 必须 | 会话与消息的最终事实 |
| Run / RunStep | ✅ 必须 | 一次协作任务的完整生命周期 |
| ToolCall | ✅ 必须 | 工具调用参数与结果，审计必需 |
| `run_event` | ✅ 进行中必须 | 进行中 run 的续传源；run 结束后可归档清理 |
| SSE 实时缓冲 | ❌ 仅内存 | `asyncio.Queue` 中未持久化事件 |
| 心跳注释 | ❌ 不落库 | 不占 seq，纯连接保活 |
| 配置缓存 | ❌ 内存 LRU | 可从 MySQL 重建 |

### 4.2 表结构

#### `agent` — Agent 配置

```sql
CREATE TABLE agent (
  id             BIGINT PRIMARY KEY AUTO_INCREMENT,
  code           VARCHAR(64)  NOT NULL UNIQUE,
  name           VARCHAR(128) NOT NULL,
  description    VARCHAR(512) DEFAULT '',
  system_prompt  TEXT         NOT NULL,
  model          VARCHAR(128) NOT NULL,
  base_url       VARCHAR(255) NOT NULL,
  api_key_cipher VARCHAR(512) NOT NULL,
  temperature    DECIMAL(3,2) DEFAULT 0.70,
  max_tokens     INT          DEFAULT 2048,
  tools          JSON         DEFAULT NULL,   -- 工具名数组（V3.2），如 ["calc","now"]
  extra_params   JSON         DEFAULT NULL,
  enabled        TINYINT(1)   DEFAULT 1,
  version        INT          DEFAULT 1,
  created_at     DATETIME     DEFAULT CURRENT_TIMESTAMP,
  updated_at     DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

#### `workflow` — 编排定义

```sql
CREATE TABLE workflow (
  id          BIGINT PRIMARY KEY AUTO_INCREMENT,
  code        VARCHAR(64) NOT NULL UNIQUE,
  name        VARCHAR(128) NOT NULL,
  description VARCHAR(512) DEFAULT '',
  definition  JSON NOT NULL,
  enabled     TINYINT(1) DEFAULT 1,
  created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

Workflow `definition` 格式：

```json
{
  "entry": "planner",
  "nodes": [
    { "id": "planner",    "agentCode": "planner",    "outputKey": "plan"   },
    { "id": "researcher", "agentCode": "researcher", "outputKey": "facts"  },
    { "id": "writer",     "agentCode": "writer",     "outputKey": "answer" }
  ],
  "edges": [
    { "from": "planner",    "to": "researcher" },
    { "from": "researcher", "to": "writer"     }
  ],
  "inputTemplate": "用户问题：{{user_input}}\n上游产出：{{upstream}}"
}
```

> V1 支持线性流水线与单节点；`edges` 预留 DAG / 条件路由扩展位。

#### `conversation` — 会话

```sql
CREATE TABLE conversation (
  id          BIGINT PRIMARY KEY AUTO_INCREMENT,
  session_id  VARCHAR(64) NOT NULL,
  workflow_id BIGINT NOT NULL,
  user_id     VARCHAR(64) DEFAULT 'anonymous',
  title       VARCHAR(255) DEFAULT '',
  status      ENUM('active','archived') DEFAULT 'active',
  created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_session (session_id),
  KEY idx_user (user_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

#### `message` — 消息（会话历史事实来源）

```sql
CREATE TABLE message (
  id              BIGINT PRIMARY KEY AUTO_INCREMENT,
  conversation_id BIGINT NOT NULL,
  run_id          BIGINT DEFAULT NULL,
  agent_code      VARCHAR(64) DEFAULT NULL,
  role            ENUM('user','assistant','system','tool') NOT NULL,
  content         MEDIUMTEXT,
  status          ENUM('pending','streaming','done','error') DEFAULT 'done',
  token_count     INT DEFAULT 0,
  created_at      DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3),
  KEY idx_conv (conversation_id, id),
  KEY idx_run (run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

> `(conversation_id, id)` 联合索引是加载会话历史的主路径；`id` 自增保证同会话内消息顺序。

#### `run` — 一次协作任务

```sql
CREATE TABLE run (
  id              BIGINT PRIMARY KEY AUTO_INCREMENT,
  conversation_id BIGINT NOT NULL,
  workflow_id     BIGINT NOT NULL,
  status          ENUM('pending','running','done','error','cancelled') DEFAULT 'pending',
  current_node    VARCHAR(64) DEFAULT NULL,
  last_seq        BIGINT DEFAULT 0,
  error_msg       TEXT DEFAULT NULL,
  started_at      DATETIME DEFAULT NULL,
  finished_at     DATETIME DEFAULT NULL,
  created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
  KEY idx_conv_status (conversation_id, status),
  KEY idx_status (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

> `idx_conv_status` 支撑 `GET /conversations/:id/active-run`；`idx_status` 用于启动时扫描遗留 `running` run。

#### `run_step` — 节点执行记录

```sql
CREATE TABLE run_step (
  id          BIGINT PRIMARY KEY AUTO_INCREMENT,
  run_id      BIGINT NOT NULL,
  node_id     VARCHAR(64) NOT NULL,
  agent_code  VARCHAR(64) NOT NULL,
  status      ENUM('pending','running','done','error') DEFAULT 'pending',
  input_json  JSON DEFAULT NULL,
  output_json JSON DEFAULT NULL,
  error_msg   TEXT DEFAULT NULL,
  started_at  DATETIME(3) DEFAULT NULL,
  finished_at DATETIME(3) DEFAULT NULL,
  UNIQUE KEY uk_run_node (run_id, node_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

#### `run_event` — 续传事件流

```sql
CREATE TABLE run_event (
  id         BIGINT PRIMARY KEY AUTO_INCREMENT,
  run_id     BIGINT NOT NULL,
  seq        BIGINT NOT NULL,
  event_type VARCHAR(32) NOT NULL,
  payload    JSON NOT NULL,
  created_at DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_run_seq (run_id, seq),
  KEY idx_run_created (run_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

> `uk_run_seq` 保证 seq 唯一且回放幂等；`idx_run_created` 支撑按时间窗口清理。
> 清理策略：`DELETE FROM run_event WHERE created_at < NOW() - INTERVAL 7 DAY`，已 done 的 run 最终内容以 `message` 为准。

#### `run_approval` — 人工审批记录（V3.1）

```sql
CREATE TABLE run_approval (
  id           BIGINT PRIMARY KEY AUTO_INCREMENT,
  run_id       BIGINT      NOT NULL,
  node_id      VARCHAR(64) NOT NULL,
  status       VARCHAR(16) NOT NULL DEFAULT 'pending',
  prompt       TEXT        NOT NULL DEFAULT '',
  comment      TEXT        DEFAULT NULL,
  decided_by   VARCHAR(64) DEFAULT NULL,
  requested_at DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3),
  decided_at   DATETIME(3) DEFAULT NULL,
  UNIQUE KEY uk_run_approval_node (run_id, node_id),
  KEY idx_approval_status (status, requested_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

这张表是**审计账本，不是暂停点**：暂停点（图状态）在 checkpointer 里，见 §16.6。
`uk_run_approval_node` 保证一个 run 里的同一个审批节点只会有一条记录——DAG 里节点最多跑一次。

> SQLite 上主键必须写成 `INTEGER PRIMARY KEY`（rowid 别名）才会自增，迁移里用
> `sa.BigInteger().with_variant(sa.Integer, "sqlite")` 表达，见 §16.8 第 4 条。

### 4.3 SQLAlchemy 异步配置

```python
# config/db.py
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

engine = create_async_engine(
    "mysql+asyncmy://user:pass@host:3306/agentdb",
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    pool_recycle=1800,
)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def get_db():
    async with SessionLocal() as session:
        yield session
```

**要点**：使用 `mysql+asyncmy://` 方言；`expire_on_commit=False` 避免异步上下文中意外触发懒加载；所有数据库访问必须通过 `AsyncSession`。

> **同一段代码支持三种方言**（MySQL / PostgreSQL / SQLite），差异全部收敛在
> `app/config/db.py` 的 `_engine_kwargs()` 里：SQLite 用 NullPool 且带 WAL
> pragma；PostgreSQL 额外下发会话时区的启动参数（原因见 §19.3）；MySQL 则在
> 连接事件里 `SET time_zone = '+00:00'`。详见 §19。

### 4.4 会话历史加载与上下文拼装

```python
async def load_context(db: AsyncSession, conversation_id: int, max_tokens: int = 6000) -> list[dict]:
    rows = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.id.desc())
        .limit(50)
    )
    messages = rows.scalars().all()
    result, used = [], 0
    for m in reversed(messages):
        cost = m.token_count or estimate_tokens(m.content)
        if used + cost > max_tokens:
            break
        result.append({"role": m.role, "content": m.content})
        used += cost
    return list(reversed(result))
```

> 消息量极大时可按 `created_at` 分区，V1 阶段联合索引 + 条数截断通常足够。

### 4.5 流式事件批量写入

`run_event` 不能每个 token 一次 INSERT，采用批量 flush：

```python
class EventWriter:
    def __init__(self, run_id: int, batch_size: int = 20, flush_interval: float = 0.05):
        self.run_id = run_id
        self.batch: list[RunEvent] = []
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self._task = asyncio.create_task(self._flush_loop())

    async def append(self, evt: RunEvent):
        self.batch.append(evt)
        if len(self.batch) >= self.batch_size:
            await self._flush()

    async def _flush_loop(self):
        while True:
            await asyncio.sleep(self.flush_interval)
            await self._flush()

    async def _flush(self):
        if not self.batch:
            return
        batch, self.batch = self.batch, []
        async with SessionLocal() as db:
            db.add_all([
                RunEventRow(run_id=self.run_id, seq=e.seq,
                            event_type=e.event_type, payload=e.payload)
                for e in batch
            ])
            await db.commit()
```

> **seq 分配与写入解耦**：`seq` 在内存中由 `asyncio.Lock` 保护的原子计数器分配，保证顺序；DB 写入异步批量，不阻塞 token 推送。`run.last_seq` 仅在 run 结束时统一更新，避免写放大。

---

## 5. SSE 通信设计

### 5.1 端点

```python
from sse_starlette.sse import EventSourceResponse, ServerSentEvent
from fastapi import Header, Query

@router.get("/runs/{run_id}/stream")
async def stream_run(
    run_id: int,
    last_seq: int = Query(0),
    last_event_id: int | None = Header(None, alias="last-event-id"),
    ticket: str = Query(...),
):
    resume_from = last_event_id if last_event_id is not None else last_seq
    async for evt in sse_writer.stream(run_id, resume_from):
        yield ServerSentEvent(
            id=str(evt.seq),
            event=evt.event_type,
            data=json.dumps(evt.payload, ensure_ascii=False),
        )
```

`EventSourceResponse` 负责：

- 自动发送心跳注释行 `: ping\n\n`，默认 15 秒间隔
- 设置 `Cache-Control: no-cache`、`X-Accel-Buffering: no`
- 将 `Last-Event-ID` 请求头注入参数

**鉴权**：`EventSource` 无法自定义 header，通过 `ticket` query 传入短期 JWT（5 分钟有效），由 `/auth/sse-ticket` 接口签发。

### 5.2 事件协议

```text
id: 12
event: node_start
data: {"node":"planner","agentCode":"planner"}

id: 13
event: token
data: {"node":"planner","content":"好的"}

: ping

id: 14
event: node_end
data: {"node":"planner","outputKey":"plan"}

id: 15
event: done
data: {"status":"done","usage":{"prompt_tokens":120,"completion_tokens":340}}
```

| 事件类型 | 说明 | 是否占 seq |
| --- | --- | --- |
| `node_start` / `node_end` | 节点边界 | 是 |
| `token` | 文本增量 | 是 |
| `reasoning` | 推理过程增量 | 是 |
| `tool_call` | 工具调用请求 | 是 |
| `tool_result` | 工具执行结果（V3.2），见 §17.7 | 是 |
| `error` | 错误 | 是 |
| `approval_required` / `approved` / `rejected` | 人工审批（V3.1），见 §16.4 | 是 |
| `done` | 正常结束，流关闭 | 是 |
| `heartbeat` | 心跳 | 否，使用 SSE 注释行 |

> 心跳必须使用 SSE 注释行（以 `:` 开头），不触发 `onmessage`、不占用 `id`，避免污染 seq 序列导致回放错位。
>
> **只有 `done` 是终止事件。** V3.1 的三个审批事件都不是终止事件：流程挂在审批节点上时
> 不发 `done`，SSE 连接继续开着，批准后同一条流接着吐字（§16.4）。

### 5.3 seq 分配

- **单实例**：`RunHub` 内每个 run 持有 `asyncio.Lock` 保护的计数器，从 `run.last_seq` 初始化；每次 emit 先 `seq = ++counter`，再写 `run_event` 与广播。
- **多实例**：使用 Redis `INCR run:{runId}:seq` 分配，或 MySQL 事务 `UPDATE run SET last_seq = last_seq + 1` 后读取。

> 单实例场景下不必每 token 更新 `run.last_seq`，只写 `run_event`，run 结束时统一更新。

### 5.4 回放 + 实时合并去重（续传核心）

**问题**：重连时若「先查 DB 回放，再订阅实时」，两步之间产生的新事件会丢失；若「先订阅，再回放」，则事件乱序。

**解法**：先订阅并缓冲 → DB 回放 → 按 seq 去重合并。

```python
async def stream(self, run_id: int, resume_from: int) -> AsyncIterator[RunEvent]:
    buffer: list[RunEvent] = []
    max_sent = resume_from

    queue = hub.subscribe(run_id)          # 1. 先订阅，事件进 buffer
    history = await repo.list_events_after(run_id, resume_from)   # 2. DB 回放
    for evt in history:
        if evt.seq > max_sent:
            yield evt
            max_sent = evt.seq

    while buffer:                            # 3. 合并 buffer
        evt = buffer.pop(0)
        if evt.seq > max_sent:
            yield evt
            max_sent = evt.seq

    run = await repo.get_run(run_id)        # 4. 已结束则补 done 并关闭
    if run.status in ("done", "error", "cancelled") and max_sent >= run.last_seq:
        return

    while True:                              # 5. 实时流
        evt = await queue.get()
        if evt.seq > max_sent:
            yield evt
            max_sent = evt.seq
```

**要点**：

- 去重依据是 `seq > max_sent`，seq 单调递增且 `run_event` 有唯一索引，天然幂等。
- 回放期间 buffer 通常只差几十到几百 token，可接受；若差距过大，直接跳到 `run.last_seq`，只下发 done 和最终内容。
- 心跳由 `EventSourceResponse` 自动发送，不占用 seq，不参与回放。

### 5.5 前端接入

```ts
let lastSeq = Number(localStorage.getItem(`run:${runId}:seq`) || 0);

function connect() {
  const es = new EventSource(`/api/runs/${runId}/stream?last_seq=${lastSeq}&ticket=${ticket}`);

  es.addEventListener('token', e => {
    const { content } = JSON.parse(e.data);
    lastSeq = Number(e.lastEventId);
    localStorage.setItem(`run:${runId}:seq`, String(lastSeq));
    appendText(content);
  });

  es.addEventListener('done', () => {
    es.close();
    localStorage.removeItem(`run:${runId}:seq`);
  });

  es.onerror = () => {
    es.close();
    setTimeout(connect, 1000);   // 重连时带上 last_seq，服务端回放补齐
  };
}
```

服务端优先读 `Last-Event-ID` 头，回退到 `last_seq` query，兼容浏览器内置重连与前端主动重连。

### 5.6 上下文恢复的两个层次

| 层次 | 场景 | 实现 |
| --- | --- | --- |
| 流级别续传 | 网络抖动 / 短暂断线 | `last_seq` + `run_event` 回放，无缝续接吐字 |
| 会话级别恢复 | 页面刷新 / 隔天回来 | 前端用 `session_id` 调 `GET /conversations/:id/messages` 拉历史；若存在 running 的 run，调 `GET /conversations/:id/active-run` 拿到 runId，再走流级别续传 |

---

## 6. Agent 编排引擎

### 6.1 V1：自研轻量编排器

```python
async def execute_run(run_id: int, workflow: Workflow, user_input: str):
    context = await load_context(...)
    for node in workflow.linear_order():
        await hub.emit(run_id, "node_start", {"node": node.id})
        step = await repo.create_step(run_id, node)
        result = await AgentRunner(node).run(context, user_input)
        await repo.finish_step(step, result)
        context.append({"role": "assistant", "content": result.answer})
        await hub.emit(run_id, "node_end", {"node": node.id, "outputKey": node.output_key})
    await hub.emit(run_id, "done", {...})
```

### 6.2 V2：改用 LangGraph 表达控制流

V2 把「按什么顺序执行节点」这件事交给 LangGraph，节点执行逻辑本身原样保留：

```python
graph = build_run_graph(definition, NodeRunner(...))
final = await graph.ainvoke(initial_state, {"recursion_limit": recursion_limit(definition)})
```

拆分点是 `app/core/run_graph.py` 里的 `NodeOps` / `NodeRunner`：**「节点要干什么」在
NodeRunner，「图怎么连」在 `build_run_graph`**。换图结构不会碰到执行逻辑，反过来也一样。

`recursion_limit` 取 `len(nodes) + 10`：LangGraph 按「超步」计数，线性链每节点一超步。
留 10 的余量是为了将来加条件边时不必回来改——触发这个上限意味着图里出现了环，
早失败比跑出重复执行要好。详细取舍见 §14。

> **V3 补充**：条件边与并行分支已落地（见 §15）。`recursion_limit` 仍取
> `len(nodes) + 10`——拓扑序给出的是「最坏情况下会经过哪些节点」，这正是上界。

### 6.3 LLM 调用

```python
from openai import AsyncOpenAI

client = AsyncOpenAI(base_url=agent.base_url, api_key=decrypt(agent.api_key_cipher))

stream = await client.chat.completions.create(
    model=agent.model,
    messages=messages,
    temperature=float(agent.temperature),
    max_tokens=agent.max_tokens,
    stream=True,
    tools=agent.tools or NOT_GIVEN,
)

async for chunk in stream:
    delta = chunk.choices[0].delta
    if delta.content:
        await hub.emit(run_id, "token", {"node": node_id, "content": delta.content})
    if getattr(delta, "reasoning_content", None):
        await hub.emit(run_id, "reasoning", {"node": node_id, "content": delta.reasoning_content})
    if delta.tool_calls:
        accumulate_tool_calls(delta.tool_calls)
```

**取消生成**：`cancel` 接口置取消 flag，`AgentRunner` 检查后 `stream.close()`。

---

## 7. RunHub 与总线扩展

```python
class BusBackend(Protocol):
    async def publish(self, channel: str, payload: str) -> None: ...
    async def subscribe(self, channel: str) -> AsyncGenerator[str, None]: ...

class InMemoryBackend:
    def __init__(self):
        self.queues: dict[str, set[asyncio.Queue]] = defaultdict(set)

    async def publish(self, channel, payload):
        for q in self.queues[channel]:
            await q.put(payload)

    async def subscribe(self, channel):
        q = asyncio.Queue()
        self.queues[channel].add(q)
        try:
            while True:
                yield await q.get()
        finally:
            self.queues[channel].discard(q)
```

> V1 使用 `InMemoryBackend`；V2 替换为 `RedisPubSubBackend` 即可多实例水平扩容，Protocol 边界保证编排器代码无需改动。

---

## 8. API 设计

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/agents` | 创建 Agent |
| GET | `/api/agents` | 列表 |
| GET | `/api/agents/:code` | 详情 |
| PUT | `/api/agents/:code` | 更新，`version+1`，清缓存 |
| DELETE | `/api/agents/:code` | 删除（或置 `enabled=0`） |
| GET | `/api/tools` | 可用的内置工具目录（V3.2） |
| POST | `/api/workflows` | 创建 Workflow |
| GET | `/api/workflows` | 列表 |
| GET | `/api/workflows/:code` | 详情 |
| PUT | `/api/workflows/:code` | 更新 |
| POST | `/api/conversations` | 创建会话 |
| GET | `/api/conversations/:id/messages` | 历史消息 |
| GET | `/api/conversations/:id/active-run` | 进行中的 run 或 null |
| POST | `/api/conversations/:id/messages` | 发送消息，返回 `{ runId, streamUrl }` |
| GET | `/api/runs/:runId` | run 详情（含状态、步骤） |
| GET | `/api/runs/:runId/stream` | SSE，参数 `last_seq`、`ticket` |
| POST | `/api/runs/:runId/cancel` | 取消生成 |
| GET | `/api/runs/:runId/approval` | 当前待审批项，没有则返回 `null`（V3.1） |
| POST | `/api/runs/:runId/approval` | 提交审批决策，批准则继续跑、驳回则终止（V3.1） |
| GET | `/api/health` | 健康检查 |

### 错误码约定

| HTTP | 含义 |
| --- | --- |
| 400 | 请求参数非法（Workflow definition 校验失败、Agent 配置缺失等） |
| 401 | ticket 无效或过期 |
| 404 | run / conversation / agent / workflow 不存在 |
| 409 | 该会话已有 running 的 run，不允许并发发起；**或**审批冲突（run 不在等待审批状态、该审批已被决策过） |
| 410 | run 已归档且事件已清理，无法回放（返回最终消息） |
| 422 | 提示词命中敏感内容，被前置过滤拦下（`sensitive_content`；V3.4，见 §20） |

422 与 400 的分工：400 表示「请求**格式**不合法」（字段缺失、类型不对），422 表示
「格式没问题，但**内容本身**不被接受」。前端可以据此区分「你参数写错了」和
「你这句话我们不接」。

---

## 9. 异常处理与边界

| 场景 | 处理 |
| --- | --- |
| 客户端断开 | 取消订阅，但不取消 run，后台继续生成写入 DB |
| run 已结束后重连 | 回放剩余事件，或直接返回最终消息 + done |
| 同一 run 多连接 | RunHub 支持一对多订阅，各自维护 `max_sent` |
| run 中途服务重启 | 启动时扫描 `status='running'` 的 run 标为 error，提示用户重发 |
| LLM 流中断 | 已生成部分作为 message 内容落库，发 `error` 事件，`run.status='error'` |
| 心跳与 Nginx | 设 `proxy_buffering off` 与 `X-Accel-Buffering: no` |
| MySQL 写入压力 | `run_event` 批量 flush（每 50ms 或 20 条） |
| 取消生成 | 取消 flag + `stream.close()` |
| 提示词命中敏感内容 | API 层 422 `sensitive_content`，**不落库、不建 run**；LLM 边界再兜一次底，编排器**不重试**（V3.4，见 §20） |
| 同步阻塞风险 | 所有 DB/HTTP 使用 asyncmy、`AsyncOpenAI`、`httpx.AsyncClient`，禁止同步调用 |

---

## 10. 项目目录结构

```text
web_agent/
├── app/
│   ├── __init__.py
│   ├── main.py                    # FastAPI app 工厂 + 生命周期
│   ├── api/
│   │   ├── __init__.py
│   │   ├── router.py              # 路由汇总
│   │   ├── deps.py                # get_db, ticket 校验
│   │   └── routes/
│   │       ├── __init__.py
│   │       ├── agent.py
│   │       ├── workflow.py
│   │       ├── conversation.py
│   │       ├── run.py             # 含审批接口（V3.1）
│   │       ├── tool.py            # 内置工具目录（V3.2）
│   │       ├── mcp.py             # MCP 服务器管理（V4，见 §21）
│   │       ├── skill.py           # Skill CRUD / 导入导出 / 提示词预览（V4，见 §22）
│   │       ├── studio.py          # studio 状态（V4，见 §22.8）
│   │       ├── auth.py
│   │       └── health.py
│   ├── core/
│   │   ├── __init__.py
│   │   ├── orchestrator.py        # run 生命周期 + workflow 调度 + 审批挂起/恢复
│   │   ├── run_graph.py           # LangGraph 图构建 + 节点执行（含审批节点）
│   │   ├── workflow_def.py        # definition 解析与校验
│   │   ├── expr.py                # 条件表达式解析与求值（不用 eval）
│   │   ├── tools.py               # 内置工具注册表（now / calc，V3.2）
│   │   ├── mcp/                   # MCP 客户端（V4，见 §21）
│   │   │   ├── manager.py         # 连接池 + 生命周期 + 并发信号量
│   │   │   ├── transport.py       # stdio / Streamable HTTP 两种传输 + 出网策略
│   │   │   ├── catalog.py         # tools/list 发现与快照落库
│   │   │   ├── mapping.py         # CallToolResult → (ok, text)
│   │   │   └── naming.py          # mcp__<server>__<tool> 命名映射
│   │   ├── skill_paths.py          # 资源路径格式规整（V4，§22）
│   │   ├── skill_pack.py           # SKILL.md 解析 / 拼装 / 内容指纹（V4，§22）
│   │   ├── skill_context.py        # 提示词注入与预算 + 技能白名单 contextvar（V4，§22）
│   │   ├── skill_reader.py         # 读技能资源（V4，§22）
│   │   ├── skill_names.py          # Agent 的 skills 字段规整（V4，§22）
│   │   ├── skill_import.py         # 技能包导入导出 + zip 防护（V4，§22）
│   │   ├── admin.py                # 管理员门禁（共享密钥，V4，§22.10）
│   │   ├── studio.py               # studio gRPC 客户端（V4，脚本执行，见 §22.8）
│   │   ├── arith.py               # 算术求值（ast 白名单，不用 eval，V3.2）
│   │   ├── checkpoint.py          # LangGraph checkpointer（审批暂停点，V3.1）
│   │   ├── agent_runner.py        # 单 Agent 执行（AsyncOpenAI）
│   │   ├── sensitive.py           # 敏感词前置过滤（字符 n-gram + 逻辑回归，V3.4）
│   │   ├── sensitive_model.npz    # 过滤模型（48 KB，随仓库分发）
│   │   ├── run_hub.py             # seq 分配 + BusBackend
│   │   ├── sse_writer.py          # 回放 + 实时合并 + EventWriter 批量落库
│   │   ├── event_log.py           # 回放源抽象：MySQL / Redis Stream
│   │   ├── event_writer.py        # 事件批量落库
│   │   ├── bus.py                 # BusBackend Protocol + InMemory / Redis 实现
│   │   ├── lease.py               # 归属租约（多实例）
│   │   ├── redis_client.py        # Redis 连接与实例标识
│   │   ├── ticket.py              # SSE 短期票据
│   │   ├── crypto.py              # api_key 加解密
│   │   └── errors.py              # 领域异常 → HTTP 状态码
│   ├── models/                    # SQLAlchemy ORM 模型
│   │   ├── __init__.py
│   │   ├── base.py                # 共享列类型（PK / DATETIME3）
│   │   ├── enums.py               # RunStatus / StepStatus / EventType
│   │   ├── agent.py
│   │   ├── workflow.py
│   │   ├── conversation.py
│   │   ├── message.py
│   │   ├── run.py
│   │   ├── run_event.py
│   │   ├── run_approval.py        # 审批记录（V3.1）
│   │   ├── mcp_server.py          # MCP 服务器注册表（V4，§21）
│   │   ├── mcp_tool.py            # MCP 工具目录快照（V4，§21）
│   │   ├── skill.py               # Skill 定义（V4，§22）
│   ├── schemas/                   # Pydantic 请求/响应模型
│   ├── repo/                      # 数据访问
│   │   ├── __init__.py
│   │   ├── agent_repo.py
│   │   ├── workflow_repo.py
│   │   ├── conversation_repo.py
│   │   ├── message_repo.py
│   │   ├── run_repo.py
│   │   ├── run_event_repo.py
│   │   ├── run_approval_repo.py   # 审批待办与决策（V3.1）
│   │   ├── mcp_repo.py              # MCP 服务器 + 工具快照（V4，§21）
│   │   ├── skill_repo.py          # Skill / 资源 / 修订历史（V4，§22）
│   ├── cache.py                   # Agent/Workflow 内存 LRU 缓存
│   └── config/
│       ├── __init__.py
│       ├── settings.py            # pydantic-settings
│       └── db.py                  # engine / SessionLocal / get_db
├── web/
│   ├── index.html                 # 功能验证台（单文件，无构建步骤）
│   └── admin.html                 # 后台管理页（单文件，无构建步骤）
├── proto/                         # gRPC 契约（V4，§22.8）
│   └── webagent/studio/v1/
│       └── studio.proto           # service Studio { Exec, Health }
├── alembic/
│   ├── env.py
│   └── versions/                  # 0001 初始 / 0002 workflow version / 0003 run_approval
│                                  # 0004 run_step attempts / 0005 skill 三张表（V4，§22.5）
│                                  # 0006 agent.skills 列（V4，§22.5）
│                                  # 0007 mcp_server / mcp_tool 两张表（V4，§21.6）
├── app/DESIGN.md
├── scripts/
│   ├── seed.py                    # 演示数据
│   └── check_docstrings.py        # docstring 覆盖率检查
├── tests/
├── requirements.txt
├── .env.example
└── README.md
```

---

## 11. 演进路线

- **V1**：单实例、线性流水线、MySQL 持久化、SSE 续传（已完成，见 §12）
- **V2**：Redis Pub/Sub 替换进程内总线支持多实例；`run_event` 走 Redis Stream 作为回放源（已完成，见 §13）
- **V3**：Workflow 支持 DAG + 条件边（**已完成**，见 §15）
- **V3.1**：人工审批节点——checkpointer + `interrupt` 挂起，批准则继续、驳回则终止本次 run
  （**已完成**，见 §16）；Agent RAG / MCP 工具**仍未做**
- **V3.2**：Agent 工具调用闭环——内置工具注册表（`now` / `calc`）、模型要工具 → 执行 →
  结果回灌 → 模型继续、配置期校验未注册的工具名（**已完成**，见 §17）
- **V4**：MCP 工具——把 V3.2 的工具来源从「代码内置」扩到「MCP 服务器」，闭环本身不用改
  （**设计已完成**，见 §21）；Skill 指令包——可复用的领域知识，**内容与资源全部存数据库**
  （与「配置即数据」一致，`SKILL.md` 降为导出格式）、正文进提示词、资源按需读，
  脚本经 gRPC 交给**独立的 studio 容器**执行、不在本进程起子进程（**设计已完成**，见 §22，
  服务侧见 [`services/studio/DESIGN.md`](../services/studio/DESIGN.md)）；
  可观测性（OpenTelemetry 链路追踪、token 成本统计）

  V4 分三个 Phase 落地，各自可交付：

  | Phase | 内容 | 状态 |
  | --- | --- | --- |
  | **Phase 1** | Skill 基础：三张表 + CRUD + 导入导出 + 审计 + 提示词注入 + `skill_resource` 工具 + 提示词预览 | **已完成**（§22.13） |
  | **Phase 2** | MCP 客户端：连接管理、工具目录快照、`mcp__` 命名分发、API | **已完成**（§21.15） |
  | **Phase 3** | studio 服务与 `skill_exec`：proto、gRPC 客户端、容器部署 | 待做（§22.8） |

---

## 12. 实现落地说明

本文档描述的系统已在 `app/` 下完成 V1 实现。实现与设计的关键对应关系与差异如下。

### 12.1 严格遵循设计的部分

- 表结构与索引按 §4.2 落地，`uk_run_seq` 保证回放幂等
- 「先订阅缓冲 → DB 回放 → 按 seq 合并去重」的续传算法按 §5.4 完整实现
- 心跳使用 SSE 注释行，不占 seq（§5.2）
- `seq` 由内存原子计数器分配，`run.last_seq` 仅在 run 结束时统一更新（§4.5 / §5.3）
- `EventWriter` 每 50ms 或 20 条批量 flush（§4.5）
- 节点失败指数退避重试 2 次（§3.3）
- 启动时扫描 `status='running'` 的遗留 run 标记为 error（§9）
- `BusBackend` Protocol 保留 Redis 扩展位（§7）

### 12.2 实现层面的取舍

| 取舍点 | 设计原文 | 实现选择 | 原因 |
| --- | --- | --- | --- |
| SSE 导入路径 | `fastapi.sse` | `sse_starlette.sse` | 原文导入有误，FastAPI 未内置该模块 |
| API Key 加密 | `api_key_cipher` 列 | Fernet 对称加密（随机 IV） | Fernet 自带认证加密与过期语义；密文不可逆推 |
| Workflow 模板 | `{{user_input}}` / `{{upstream}}` | Pydantic 强校验 + `linear_order()` 拓扑排序 | 提前拦截非法 definition，避免运行期报错 |
| 本地开发数据库 | MySQL only | `DATABASE_URL` 可切换 MySQL / SQLite | 无 MySQL 也能跑通全链路与测试；生产仍用 MySQL |
| 主键类型 | `BIGINT AUTO_INCREMENT` | `BigInteger` + SQLite 回落 `Integer` | SQLite 只有 `INTEGER PRIMARY KEY` 才是 rowid 别名、才会自增 |
| 消息状态机 | enum 含 `streaming` | 新建 assistant message 用 `streaming`，节点结束转 `done` | 便于前端区分「正在吐字」与「已完成」 |
| run 并发控制 | 409 不允许并发 | 创建 run 时行级锁检查 running | 避免同一会话两个 run 同时写事件 |
| 参数校验失败 | 默认 422 | 统一 400 | 与 §8 的错误码约定保持一致 |
| 工具调用 | 发 `tool_call` 事件 | 只记录不下发执行器 | V1 无工具注册表，执行属 V3 范围（V3.2 已补上，见 §17） |

### 12.3 实现中发现并修复的三个真实缺陷

这几个问题都不是「照着设计写就自然对」的，必须显式处理，因此单独记录：

#### ① 回放读到的是「还没落库」的 DB —— 会漏掉最后几个 token

`EventWriter` 是批量异步落库的（每 20 条 / 50ms）。新连接「先订阅 → 回放 DB」时，
DB 里可能还没有最后几个已 emit 的事件；它们既不在 DB，也不会再进本连接的队列
（订阅发生在它们之后）。表现是：重连后尾部 token 丢失。

**修法**（`core/sse_writer.py` + `core/event_writer.py`）：

1. 订阅之后、回放之前，先 `hub.flush(run_id)` 把内存里的事件立刻写下去；
2. `_flush()` **整段（含 DB 写）都在锁内**，让并发的 `flush()` 一定排在正在进行的
   写入之后，拿到的是最新批次；
3. `RunHub.finalize()` 改为**先冲刷 writer、再摘状态**——否则状态一摘除，并发的
   `hub.flush()` 会静默 no-op，回放又读回一份缺尾部的 DB。

#### ② 已收到终止事件、但 run 行还没回写状态 —— 连接永久挂起

编排器的顺序是「先 `emit(done)` → 再冲刷 → 再回写 `run.status`」。重连恰好落在这个
窗口时，回放会拿到 `done` 事件，而快照里 run 仍是 `running`；旧逻辑据此进入实时流
等待一个再也不会来的事件。

**修法**：`yielded_terminal` 为真时**立即返回**，不再看 run 状态（`sse_writer.py` ⑤）。

#### ③ 实时流在「run 已结束但无事件可推」时不会自行收尾

兜底安全网：实时流空闲超过 `SSE_IDLE_RECHECK`（默认 2s）就回查一次 run 状态，
若已终止则补一个合成 `done` 后关闭。

#### 附：设计文档 §4.4 示例代码本身有一处顺序 bug

```python
for m in reversed(messages):   # messages 已按 id DESC 排序 → 反向即时间正序
    result.append(...)
return list(reversed(result))  # ← 这一句会把顺序又翻回去
```

实现中已修正为直接 `return result`。

### 12.4 验证要点

落地时优先保证两处正确性，测试用例即围绕它们设计：

1. `run_event` 批量写入不丢事件、不重复、seq 严格递增；
2. 「先订阅 → 冲刷 → 回放 → 按 seq 合并」在断线重连时不漏字、不重复、不乱序。

实测结果：单元 + 集成 + 端到端共 72 个用例全绿；对真实 MiniMax 模型做断线续传冒烟，
1210 个事件从 seq 7 续到 1210，流式文本与最终落库内容逐字一致（3939 字）。

---

## 13. V2：Redis 多实例

V1 的目标形态是单实例。V2 要做的是让它能在**多个进程之间**正确工作——不是简单地把
uvicorn 起两台，而是把「哪些状态不能留在进程内存里」这个问题系统性地解决掉。

### 13.1 先定位：多实例下哪些状态会出问题

逐条审查 V1 的六处进程内状态，结论如下。前两项是**硬伤**：

| 状态 | V1 位置 | 多实例下的后果 | V2 方案 |
| --- | --- | --- | --- |
| 事件广播 | `InMemoryBackend` | B 实例收不到 A 实例吐的字 | Redis Pub/Sub |
| **落库缓冲** | `EventWriter._batch` | **B 实例的 `hub.flush()` 是空操作 → 尾部 token 丢失**，§12.3 ① 修的 bug 原样复活 | 回放源换成 Redis Stream，`flush()` 不再是正确性前提 |
| **启动恢复** | 扫全表把 running 标 error | **B 实例一启动就把 A 实例正在跑的 run 全杀掉** | 归属租约，只回收无主的 |
| 取消标志 | `_RunState.cancel_requested` | B 实例点取消，A 实例收不到 | 控制频道 |
| 活跃判断 | `_states` 里有键 | 跨实例判断不了 | 租约 TTL |
| seq 计数器 | 本地计数 | 单 run 只由一个实例生成，不冲突 | 保留本地，Stream ID 冲突作护栏 |

其中「启动恢复误杀」最严重：多活反而比单活更糟，用户眼看着自己的回答在生成中途被判死刑。

### 13.2 回放源：为什么必须是 Redis Stream

V1 里「回放读 MySQL」直接导致 `hub.flush()` 成为续传正确性的前提——因为
`EventWriter` 批量异步落库。单进程时 flush 就在同一进程里，没问题；**多实例做不到**：
SSE 在 B 实例、EventWriter 在 A 实例，B 调 `flush()` 是空操作。

所以 V2 把回放源换成 Redis Stream。`emit()` 时事件就**同步** `XADD` 进 Stream，
任意实例的 SSE 都能 `XRANGE` 到它——`flush()` 从此只是「让 MySQL 归档更及时」的优化。

Stream 条目 ID 固定写成 `<seq>-0`。Redis 要求新 ID 严格大于末位，而 seq 在单个 run 内
单调递增，于是断点续传直接就是 `XRANGE key (42-0 +`，不必按字段过滤。万一两处同时给
同一个 run 发号，Redis 会拒绝较小的 ID——这是单写者约定的运行时护栏，而不是悄悄写进
一条乱序数据。

数据流：

```text
emit(seq=42)
  ├─ XADD    P:run:<id>:ev   42-0 …     ← 持久化 + 回放源（同步，跨实例可见）
  ├─ PUBLISH P:run:<id>      {...}      ← 实时推送（跨实例）
  └─ 本地 EventWriter → MySQL           ← 长期归档（批量，保留 7 天）
```

**顺序不能换**：先落再播，保证订阅者收到的任何事件都必定已经可被回放。

MySQL 仍保留为长期归档，因此 `RedisEventLog.replay` 在 Stream 为空时会**回落到 MySQL**——
这不是可选兜底而是必需的：Stream 有 TTL，而启用 Redis 之前产生的 run 压根没有 Stream，
只读 Stream 会让这些 run 一被续传就读不到内容。

### 13.3 续传正确性的时序论证（以及它被打破过一次）

设 `t1` = Redis 确认 SUBSCRIBE 的时刻，`t2` = 回放执行 XRANGE 的时刻。任意事件 E 满足
`publish_time > append_time`，于是

> E 不在回放里（`append_time > t2`）与 E 不在实时队列里（`publish_time < t1`）
> **不可能同时成立**——前提是 `t1 < t2`。

所以「回放 ∪ 实时队列」是完整的，`hub.flush()` 不再需要。

**但这个前提曾经被打破，而且真的漏了字。** 初版实现里 `subscribe()` 只创建 pump 任务就
返回，SUBSCRIBE 还在网络上飞。此时 `t2 < t1` 完全可能（XRANGE 与 SUBSCRIBE 是两条连接上
的并发往返，没有先后保证），落在 `(t2, t1)` 窗口里的事件两头落空。

双实例实测抓到了：一次「读 30 条就断开、重连接 415 条」的续传里，`seq=33` 没送达，
拼接文本少 2 个字。查 Redis Stream 与 MySQL 都是完整的 445 条——**服务端没丢，是订阅
窗口**。修法是恢复 V1 的不变量：`BusBackend.subscribe` 改为协程，**必须 await 到 Redis
确认 SUBSCRIBE 才返回**，`sse_writer` 第 ① 步随之 await。

> 这也是为什么 `subscribe` 不能图省事写成同步的：它必须是「返回即已生效」的语义，
> InMemory 实现内部没有任何 await 所以零开销，Redis 实现则老实等一次网络往返。

### 13.4 归属租约与取消信号

「谁在管这个 run」变成 Redis 里一把带 TTL 的租约：

| 键 | 值 | TTL | 用途 |
| --- | --- | --- | --- |
| `P:run:<id>:owner` | 实例标识（`主机名:PID:随机后缀`） | `REDIS_ACTIVITY_TTL`，按 TTL/3 续期 | 判断 run 是否还有主 |
| `P:run:<id>:cancel` | `1` | 较大 | 兜住「取消早于订阅到达」的窗口 |
| `P:run:<id>:ctl` | Pub/Sub | — | 取消等控制指令 |

- **启动恢复**只回收**没有主**的 run。实例被 `kill -9` 时没人续期，键自然过期，
  不需要任何额外的清理动作、心跳表或实例注册表。
- **取消**走控制频道，同时写取消键、订阅方上线后主动补查。只做前者会漏取消，
  只做后者会有延迟（用户点了停止却要等几秒才停）。
- **`is_cancelled` 仍然只读进程内的布尔字段**——它每个 chunk 都要轮询一次，那里绝不能
  出现 await 或 Redis 往返。跨实例的部分由控制频道在**收到信号的那一刻**解决掉。
- **`is_active` 跨实例语义**单独提供 `is_active_anywhere()`；`is_active()` 保持只看本实例。

### 13.5 配置与降级策略

```dotenv
REDIS_ENABLED=false          # 默认关：单实例行为，测试全部依赖它
REDIS_URL=redis://192.168.1.5:6379/0
REDIS_KEY_PREFIX=webagent    # 多套环境共用一个 Redis 时做隔离
REDIS_STREAM_MAXLEN=20000    # 每个 run 的 Stream 条数上限（近似裁剪）
REDIS_ACTIVITY_TTL=120       # 租约 TTL（秒）
REDIS_POLL_INTERVAL=1.0      # 订阅轮询间隔，决定退订收尾的快慢
```

**开启后没有降级路径**：Redis 连不上时服务直接启动失败。这是刻意的——静默退回进程内总线
会造成「服务能跑、跨实例收不到事件」的分裂大脑，比启动失败难查得多。单实例开发请显式关掉。

### 13.6 抽象方式

延续 V1 已有的「Protocol + 双实现 + 单例」套路，让业务代码里没有一处 `if redis_enabled`：

| 抽象 | V1 实现 | V2 实现 |
| --- | --- | --- |
| `BusBackend` | `InMemoryBackend` | `RedisPubSubBackend`（Pub/Sub 实时） |
| `EventLog` | `DbEventLog`（读 `run_event`） | `RedisEventLog`（读 Stream，空则回落 MySQL） |
| `RunLease` | `NoopLease` | `RedisLease`（租约 + 取消频道） |

`get_bus()` / `get_event_log()` / `get_lease()` 按配置选实现，三处必须一致——否则会出现
「事件写进 Redis、回放却去 MySQL 找」这种错配，症状是续传大面积失败且极难定位。

### 13.7 单写者约定

一个 run 只由接下 `POST /messages` 的那个实例负责编排，别的实例只能读。这条约定撑起了两处
设计：seq 分配用本地计数器（省掉每个 token 一次 Redis 往返），以及 Stream 条目 ID 直接取
seq。V2 **没有实现 failover**：实例挂掉后它持有的 run 由租约过期 → 启动恢复标 error，
不会被另一个实例接管重跑。

### 13.8 实现过程中发现并修复的缺陷

除 §13.3 那个漏字 bug 外，V2 落地时还踩到三处，都已修复：

| # | 缺陷 | 根因 | 修法 |
| --- | --- | --- | --- |
| 1 | **Redis 订阅 / 回放之间漏事件** | `subscribe()` 不等 SUBSCRIBE 确认，见 §13.3 | `subscribe` 改协程，必须 await |
| 2 | **cancel 掉阻塞在读上的订阅任务会让后续关闭永久挂起** | pump 阻塞在 `pubsub.get_message()`，强行 cancel 打断这次读，redis-py 的连接锁留在不一致状态，之后的 `unsubscribe()/aclose()` 抢同一把锁就死等。现象是**挂在几十行之外的用例上** | 退订只置关闭标志、让 pump 在下一个轮询周期自行退出；`REDIS_POLL_INTERVAL` 决定收尾快慢 |
| 3 | **`PUT /api/workflows/{code}` 带 definition 必 500** | `payload.model_dump()` 已把嵌套模型转成 dict，代码又对该 dict 调 `.model_dump()` | 从 `payload.definition`（模型对象）重新 dump |
| 4 | **测试套件被开发者本地 `.env` 污染** | `env_file=".env"` 是相对路径，pytest 从项目根跑会命中本地 `.env`；开了 Redis 后整个套件切到 Redis 模式，不同用例的 run id 从 1 开始互相撞车 | `conftest.py` 强制 `REDIS_ENABLED=false` |

其中 #3 是 V1 就存在的 bug，此前测试只覆盖了 POST 建 Workflow，一直没人发现。

### 13.9 验证结果

- **19 个 Redis 集成测试**（`tests/test_redis.py`）：键布局、Stream 断点续传、重复 seq
  护栏、TTL、MySQL 回落、跨实例 Pub/Sub、跨实例取消、取消早于订阅到达的补查、启动恢复
  归属判定，以及两条跨实例续传主线。
- **V1 回归 74 个用例全绿**，确认默认关闭 Redis 时行为逐字不变。
- **双实例端到端**：两个独立 uvicorn 进程（实例标识不同），在 B 实例发消息、A 实例收流，
  中途断开后带 `last_seq` 在 A 实例重连——445 个事件 seq 严格连续、无重复、无乱序，
  拼接文本与 MySQL 落库、与假模型输出三方逐字一致（873 字）。

外部依赖抖动会伪装成代码故障，因此做了两件事：

- `connect_redis` 带 3 次退避重试。实测遇到过一轮测试跑到一半 Redis 拒连
  （`Error 22 ... 远程计算机拒绝网络连接`），两秒后它自己恢复；没有重试的话，
  那一次会变成 19 个用例同时 ERROR。
- 测试夹具在连不上时**跳过**而不是报错。真正的回归应当表现为「Redis 通着但用例不过」。

---

## 14. V2-b：用 LangGraph 替换自研编排器

V1 的 `execute_run` 是一段 `for node in workflow.linear_order()`。它能跑，但「控制流」
这件事被写死在了 Python 循环里——想加分支、判断路由、并行汇聚，都得回来改这个循环。
V2-b 把控制流交给图，节点执行逻辑一行不动。

### 14.1 拆分点：NodeOps / NodeRunner

```python
graph = build_run_graph(definition, runner)
final = await graph.ainvoke(initial_state, {"recursion_limit": len(nodes) + 10})
```

`RunState` 只放**跨节点流动**的数据（context / upstream / usage）。每 run 固定不变的量
（run_id、agents、hub……）不进 state，它们是 `NodeRunner` 的构造参数——放进 state 只会让
LangGraph 每个 super-step 都去序列化一遍本不需要持久化的东西。

`NodeOps` 是 Protocol，`Orchestrator` 实现它（`set_current_node` / `run_with_retry` /
`persist_message` 三个方法），因此 `run_graph.py` 不依赖 `orchestrator.py`，反之也不依赖。
节点闭包只做一件事：把 state 转交给 `execute_node`。

### 14.2 连线来自 definition.edges，不是 nodes 声明顺序

这是本次迁移最关键的一处。`build_run_graph` 按 `edges` 连线，而不是假设「按 nodes 顺序
串起来」。于是 V3 加条件边 / DAG 时，改的只有这一个函数，`execute_node` 一行都不用动。
`tests/test_run_graph.py` 里 `test_execution_order_follows_edges_not_declaration_order`
专门钉住这一点：nodes 声明为 `[b, a]`、edges 为 `a→b`，必须按 `a→b` 执行。

节点名用 `node.id`，它同时是事件里的 `node` 字段、`upstream` 的键和 `run_step` 的外键——
用它当图节点名能让日志里的图和业务数据对得上。

### 14.3 刻意不挂 checkpointer

`langgraph-checkpoint-redis` 提供了 `AsyncRedisSaver`（支持 `.with_ttl()`），技术上完全
可以接。**不接**，理由有三条，每条都能用本项目的实测数据说明：

**粒度差两个数量级。** `EventType` 七种里 `TOKEN` / `REASONING` 是逐块推进的。§13.9 的
双实例实测里，一次三节点流水线产生 **445 条事件**；而 checkpointer 的粒度是 super-step，
一个节点执行完存一份，即 4 个 checkpoint。**445 : 4 ≈ 每个 checkpoint 里压着 110 条事件。**
换成它，客户端在第 137 个 token 断线就只能从节点边界重放，前端打字机退化成「整段冒出来」
——这正是 V1 花力气修好的「尾部 token 丢失」要换个方向再犯一次。

**存的东西类型不同。** `run_event` 存的是 `RunEventDTO(run_id, seq, event_type, payload)`
——追加、不可变、带全局序号的事件流。LangGraph 存的是 `Checkpoint(channel_values,
channel_versions, pending_writes, next)`——**状态快照**，每个 super-step 覆盖上一份。
快照不能当事件流读：打字机、重放、以及「流文本 == 库文本」这类断言，全都建立在能按 seq
逐条重放上。

**checkpointer 不提供互斥。** 读 `BaseCheckpointSaver` 的 docstring，通篇只讲状态持久化
与 `thread_id` 主键，没有任何并发控制语义。而 §13.4 的归属租约解决的正是「别让两个实例
同时编排同一个 run」。换过去的结果是事件流换掉了，租约、取消控制频道、启动时回收无主 run
三样一个不少，架构复杂度反而多一份。

「用 `get_state_history()` 从快照差分推事件」这条路也堵死了：要让 LLM 节点变成逐 token
checkpoint 的子图，每个 token 一次写——比现在每 token 一次 `XADD` **更贵**；token 增量
必须进 state channel 才能被差分到，于是每次 token 都要重写累积全文，检查点体积 O(n²)；
差分推出来的 `seq` 也没有「无缺口」性质，因此没有 `XADD <seq>-0` 那道天然护栏。

**结论**：`run_event` + Redis Stream 是唯一权威事件日志。它服务的是「对外事件流」这个
消费者，checkpointer 服务的是「恢复执行」这个消费者，两者不冲突也不可互相替代。

> **V3.1 兑现说明**：本节的三条论证**一条都没变**——checkpointer 依然不适合当事件日志，
> 事件流依然只由 `run_event` + Redis Stream 负责。V3.1 挂了 checkpointer，但它服务的是
> 另一个消费者：「图停在哪、恢复时从那儿继续」。本节的结论限定在「事件流」这个用途上，
> 见 [§16.3](#163-暂停机制原生-checkpointer--interrupt)。

### 14.4 保留在节点内的重试与取消检查

LangGraph 有 `RetryPolicy`，但它和我们要的不是一回事：

- **重试**：我们要「指数退避 + 写 `run_step` 记录每次尝试 + 取消时立即中止」。`RetryPolicy`
  只管重试次数，退避策略、每次尝试的可观测性都要绕出去自己实现。留在节点函数里更可控。
- **取消检查**：必须放在**每个图节点的开头**。放到图外就没有「进入下一节点前」的统一
  拦截点——图不会给你一个节点间的钩子。这里保留 `hub.is_cancelled`，它只读本地布尔字段，
  每 chunk 轮询一次，那里不能出现 await（见 §13.4）。

### 14.5 V1 的 DAG 拒绝校验仍然保留

`len(edges) != len(nodes) - 1` 即拒绝，尽管 LangGraph 让 DAG 变得容易。路线图把 DAG 放在
V3，本次不擅自扩大范围。识别为 V3 的低成本改动点：校验规则放宽 + `build_run_graph` 支持
条件边，两处互不干扰。

> **V3 兑现说明**：V2-b 写下的这两个低成本改动点已在 V3 全部落地，见 [§15](#15-v3dag--条件边)。
> 本节保留原样，作为「当时为什么不擅自扩大范围」的记录。

### 14.6 依赖副作用

`pip install langgraph` 把 `websockets` 从 17.2 降到 16.1.1（langchain-core 1.6.12、
langgraph-checkpoint 4.3.0 一并引入）。已实测 `uvicorn.protocols.websockets.websockets_impl`
仍可正常导入，但会打 `UvicornDeprecationWarning`——uvicorn 已计划把默认实现换成
`websockets-sansio`。不影响 SSE（SSE 走的是 `sse-starlette`），仅此记录。

### 14.7 验证结果

- **9 个图构建测试**（`tests/test_run_graph.py`）：含执行顺序按 edges、空图、单节点、
  节点名对齐、异常原样冒泡。
- **全量 102 个用例全绿**（43.6s），默认关闭 Redis 时行为与 V1 逐字不变。
- 堆栈确认 `graph.ainvoke` 真的在调用链上，不是「import 了但没走到」——
  节点报错时堆栈是 `langgraph/_internal/_runnable.py:522 ainvoke → run_graph.py:195
  _run → execute_node`。
- **迁移后重跑双实例端到端**（B 实例编排、A 实例收流、中途断开后带 `last_seq` 在 A 重连）：
  277 个事件 seq 严格连续、无重复、无乱序，拼接文本 540 字与 MySQL 落库、与假模型三段
  输出三方逐字一致，run 终态 `done`、`last_seq=277` 与流末 seq 相等。

**已知代价**（诚实记录，非缺陷）：每个 token 一次 `XADD`，一个万字 run 就是万条 Stream
条目，直到 `MAXLEN ~` 裁剪。这不是架构问题而是调参问题（`redis_stream_maxlen`）——换
checkpointer 只会更贵，不会更省。

---

## 15. V3：DAG + 条件边

V3 只做一件事：**让 Workflow 从「一条链」变成「一张有分支的图」**。人工审批（checkpointer
+ interrupt）、RAG、MCP 都顺延到 V3.1，本轮一律不碰。

### 15.1 为什么先做 DAG，而不是先做人工审批

人工审批需要 checkpointer（暂停点）与 interrupt（挂起/恢复），而 checkpointer 会在图执行的
每一步落盘状态——**并行分支下，一个超步里可能同时挂起多个节点**，断点语义要重新设计。
相比之下，DAG 是纯控制流问题：LangGraph 已经支持条件边与并行分支，缺的只是「我们自己的
校验与图构建」这一层。把审批和 DAG 一起做，等于同时引入两个新维度，出问题时很难定位是哪
一个引起的。

### 15.2 边的新语义

`WorkflowEdge` 增加一个可选字段 `when`：

| 边的类型 | 含义 | 例子 |
| --- | --- | --- |
| 无 `when` | 无条件走。**同一节点的多条无条件出边 = 并行分叉**；**同一节点的多条无条件入边 = 汇聚** | `root → left`、`root → right` |
| 有 `when` | 表达式成立才走。同一节点的多条条件出边 = 分支（互斥选择） | `root → deep  when: len(upstream.plan) > 0` |

### 15.3 条件表达式

新增 `app/core/expr.py`：一个**手写的递归下降解析器**，不用 `eval`、不引第三方库。

- **不用 `eval` 是硬要求**：条件表达式来自用户配置，`eval` 等于把 RCE 暴露给任何能编辑
  工作流的人。手写解析器虽然多几百行，但换来「求值能力完全由白名单决定」。
- 文法：`== != > < >= <=`、`and / or / not`、括号、字符串 / 数字 / `true` / `false` /
  `null`、变量路径（`upstream.plan`）、白名单函数（`len` / `contains` / `lower` / `is_empty`）。
- **类型不同一律返回 False，不抛错**；变量不存在返回 `None`；空串与 `None` 算假。
  求值器抛错意味着整条 run 失败，代价太大；「条件不成立」是安全的降级方向。
- 函数元数在**解析期**校验——`contains(x)` 这种少参数的写法在保存时就被拒，而不是等到
  运行期求值才炸。

### 15.4 配置期一次拦干净

DAG 最大的风险是「配置能存进去，但跑不起来」。V3 把所有能静态判定的错误都挡在**保存时**：

| 校验 | 拒绝什么 | 为什么 |
| --- | --- | --- |
| 无环 | 存在环 | 执行顺序无法确定，且会撞 `recursion_limit` |
| 无孤立节点 | 从 entry 走不到的节点 | 死配置：保存不报错，但那个节点永远不跑 |
| 汇聚形状 | 汇聚节点的入边里混了条件边 | LangGraph 对多入边节点是「等齐所有入边才触发」，条件跳过一条就永远等不到 |
| 出边一致 | 同一节点的出边里条件与无条件混用 | 语义不明确（「无条件那部分还走不走」要靠读者推断） |
| 条件语法 | `when` 表达式解析不过 | 拼错在运行期才发现，token 已经花出去了 |
| 条件变量 | `when` 引用了不存在的 `outputKey` | 表达式照样能过语法检查，运行期安静求值成 `None` → 条件恒假 → **走错分支且不报错** |
| 模板变量 | `inputTemplate` 引用了不存在的变量 | 同上：`{{拼错}}` 会原样发给模型看 |

条件表达式里**只认两种写法**：`user_input` 与 `upstream.<outputKey>`。刻意不放行裸
`plan`——求值期的 scope 只有 `upstream` 与 `user_input` 两个根，裸名会静默求值成 `None`；
放行它还会引出「某个节点恰好叫 `user_input`」这类遮蔽问题。与其留一个静默为假的写法，
不如保存时直接告诉正确写法。

### 15.5 并行下 RunState 必须带 reducer

并行分支会在**同一个超步**里返回结果。LangGraph 的 state 合并规则决定了：没有 reducer
的字段会被**后返回的那个值覆盖**，前一个分支的产出直接消失。

因此 `RunState` 的三个字段全带 reducer：

- `context`：拼接（两个分支的产出都要进上下文）；
- `upstream`：`dict.update`（不同 outputKey 互不覆盖）；
- `usage`：**逐 key 相加**（token 数要真的加起来）。

配套地，**节点必须返回增量而不是全量**。全量 + reducer 会双倍计数：两个节点各花
10/5 token 时，全量写法得到 30/15（应为 20/10）。这个 bug 是被测试抓出来的，不是想出来的。

### 15.6 运行时发现的两个实现缺口

V3 的端到端验证顺带查出两处「docstring 承诺了、代码没做」：

1. **`render_input` 没实现 `{{outputKey}}` 替换**。它只做 `{{user_input}}` 与
   `{{upstream}}` 两个固定串的替换，于是 `{{left_out}}` 原样进了发给模型的 prompt。
   DAG 下这是刚需——并行分支同时产出多份内容，用 `{{upstream}}` 一把全塞进去，模型分不清
   哪段对应哪个分支。已改为逐键替换 + 保存期校验未知变量。
2. **条件里的裸 outputKey 静默求值为 None**。校验器放行了 `plan`，运行期 scope 里却没有
   裸名，条件恒假 → 永远走 else 分支，且**没有任何报错**。已改为保存期拒绝并提示
   `upstream.plan`。

两个都属于「配置期拦住」的同一类问题，只是靠端到端跑真实流程才暴露出来——单元测试全都
只测了「语法对不对」。

### 15.7 验证结果

- **图构建与执行测试**（`tests/test_run_graph.py`）：并行双产出都在、汇聚节点看到双上游、
  usage 只计一次、条件只走命中分支、全 false 直接到 END、条件分支可喂下游无条件边。
- **配置校验测试**（`tests/test_workflow_def.py`）：环、孤立节点、条件语法错误、未知
  outputKey、出边混用、条件汇聚、全条件汇聚、模板变量与裸 outputKey 拒绝。
- **端到端**（真实 HTTP 服务 + 项目自带 `FakeLLM`，17/17 通过）：单节点、DAG 并行汇聚
  （4 节点、usage 与实际吐字一致、汇聚节点同时看到两条分支产物、模板无残留占位符）、
  条件命中、条件全落空、保存期拒绝 4 类错误配置。
- **内置浏览器**（验证台 `/`）：DAG 并行的事件日志显示 seq 18/19 两个分支同一超步启动、
  token 交错（证明真并发）、seq 37 汇聚节点在两条分支都结束后才启动、usage 44 = 4×11
  无重复计数；条件分支只启动命中的 `deep`，`when: "false"` 的 `quick` 被跳过。
- **全量 129 个用例全绿**（51.3s）。

### 15.8 顺带修掉的：Redis 瞬时不可达会打死 run

这不是 V3 的功能，但被 V3 的浏览器验证撞上了，值得单独记一笔。

外部 Redis 瞬时不可达时，`lease.claim` 的 `SET` 抛 `ConnectionError`，**run 在第一个节点
都还没开始前就夭折了**——用户看到的是「什么都没发生就失败了」。同一个模块里的租约续期循环
早就写着「Redis 抖动不该杀掉生成」，只是 claim 这条路没遵守自己的约定。

三处按「抖动降级、绝不杀生成」的既有模式补齐：

| 位置 | 改前 | 改后 |
| --- | --- | --- |
| `RedisLease.claim` | `SET` 失败 → run 夭折 | 重试一次；仍失败则**降级为无租约运行**（续期循环会在 Redis 恢复后把归属键补上） |
| `RunHub.emit` | 写 Stream / 广播失败 → run 夭折 | 记告警继续。MySQL 归档照旧进行，回放读不到 Stream 时回落到 MySQL |
| `RedisEventLog.replay` | 只读 Stream | 取 **Stream ∪ MySQL 归档** 的并集：Redis 抖动期间写失败的事件只进了归档，只读 Stream 会漏掉那一段（而且漏的是尾部） |

补 6 个降级测试（`tests/test_redis_degradation.py`），全部用假件模拟抖动，不需要真实 Redis。

### 15.9 已知限制

- **汇聚节点不能混条件入边**：这是 LangGraph 的语义限制（等齐所有入边才触发），配置期
  明确拒绝并给出可执行的改法（把条件前移到分叉处）。
- **全条件汇聚被拒绝**：所有入边都是条件边时，任何一条不成立就永远等不到。等价写法是
  把汇聚点改成「无条件边 + 节点内部判断」。

> **V3.1 兑现说明**：本节原先写的「人工审批不在 V3，`checkpointer` + `interrupt` 顺延到
> V3.1」已在 V3.1 落地，见 [§16](#16-v31人工审批)。审批节点挂起时**事件流仍由
> `run_event` + Redis Stream 唯一负责**（理由同 §14.3，checkpointer 只管「停在哪」）。

---

## 16. V3.1：人工审批

### 16.1 目标与边界

流程跑到「需要人拍板」的地方停下来，等人给个决策，然后继续往下走，或者就此终止。

本轮范围**只有**人工审批。Agent 的 RAG 检索、MCP 工具调用仍然不做——那是给 agent
加新能力，与「流程中途等人」是两件事，不该混在一轮里（见 [§11](#11-演进路线)）。

**老 definition 一行都不用改**：节点类型由 `type` 字段表达，省略即 `agent`。

### 16.2 节点类型：`type` 字段

```json
{
  "entry": "plan",
  "nodes": [
    {"id": "plan",  "agentCode": "planner", "outputKey": "plan_out"},
    {"id": "gate",  "type": "approval", "prompt": "这份计划可以执行吗？", "outputKey": "gate_out"},
    {"id": "write", "agentCode": "writer",  "outputKey": "answer"}
  ],
  "edges": [{"from": "plan", "to": "gate"}, {"from": "gate", "to": "write"}]
}
```

审批节点**必须**有 `prompt`（审批人看什么？），**不能**有 `agentCode`（它不跑模型）。
两条都在配置期拒掉，不留到运行时。

**为什么用节点类型，而不是另外两种表达方式**：

| 方案 | 问题 |
| --- | --- |
| 用某个特殊 `agentCode` 表示审批 | definition 就不再自洽了——「这个节点会停下来」这件事要反查 `agent` 表才知道。审批节点根本不是 agent，凭什么占一个 agentCode？ |
| 在边上加标记 | 停不停是**节点自己的性质**，不是边的性质。同一个节点从不同边进来都会停，标记在边上表达不了这件事。 |

节点类型是节点自己的属性，放对地方了。

### 16.3 暂停机制：原生 checkpointer + `interrupt()`

§14.3 写过「刻意不挂 checkpointer」，理由是它做不了事件流。V3.1 挂了，但**用途完全不同**：

> checkpointer 不适合当**事件日志**（§14.3 的三条理由一条没变）；而它恰好就是
> **「存下状态快照、从暂停处原样继续」**这件事的标准解。

两个消费者，两份数据，不重叠：

| 数据 | 回答的问题 | 谁负责 |
| --- | --- | --- |
| `run_event` + Redis Stream | 「发生过什么」 | §14.3 的那套，一字未改 |
| checkpointer | 「图停在哪、那一刻状态是什么」 | LangGraph |

**为什么不自己写「把状态存进 MySQL 再从头重放」**：

- 重放会**重新求值条件边**，求值时机会与原始执行不同（边角情况会出错）；
- 重放会**重花已经花掉的 LLM token**；
- 而 checkpointer 的语义是「原样延续」：恢复时只有被中断的那个节点重跑，前面的节点不重跑。

实测（`tests/test_approval_e2e.py`）：一个「规划 → 审批 → 写作」的流程，挂起再批准之后，
模型只被调了 **2 次**（规划 1 次 + 写作 1 次），规划没有重跑；`usage.prompt_tokens = 22`
正好是 2 × 11。

**为什么有两套实现**：

| 场景 | 实现 | 理由 |
| --- | --- | --- |
| 单实例开发（`REDIS_ENABLED=false`） | `AsyncSqliteSaver` | 零外部依赖，一个本地文件 |
| 多实例（`REDIS_ENABLED=true`） | `AsyncRedisSaver` | 暂停点必须**所有实例都能看到**：A 实例上挂起、B 实例上收到审批要能续上 |

选 Redis 而不是 MySQL：LangGraph 没有官方 MySQL saver，而本项目已经在用 Redis 做多实例
协调；再引一个 saver 实现等于多一套要维护的代码。

**checkpointer 不可用时直接报错，不静默降级**：没有 checkpointer 就没有「暂停」，run 会卡
在审批节点上，用户看到的是「一直在等审批」却永远等不到结果。那种状态比明确报错难查得多。

**`thread_id = f"run-{run_id}"`**：暂停点属于**这一次 run**，不是会话——一个会话可以有多次
run，第二次 run 不该看到第一次的暂停点。

**首次执行前删 thread、终态时删 thread**：前者的实测症状是「同 thread_id 上残留的通道值
与本次输入合并，context 被追加两遍」；后者是别让暂停点在 Redis / SQLite 里无限堆积。

### 16.4 状态机与事件

| 变更 | 取值 | 说明 |
| --- | --- | --- |
| `RunStatus.WAITING` | 非终态 | 挂在审批节点上等人决策 |
| `RunStatus.REJECTED` | 终态 | 被驳回，本次 run 就此结束 |
| `StepStatus.WAITING` | — | 审批节点在 `run_step` 里的状态 |
| `EventType.APPROVAL_REQUIRED` | 非终止事件 | 流程要人决策了 |
| `EventType.APPROVED` | 非终止事件 | 人批准了，继续往下跑 |
| `EventType.REJECTED` | 非终止事件 | 人驳回了 |

**只有 `done` 才是终止事件。** 这一点很要紧：暂停时**不 emit done**——run 还没结束，SSE 流
要继续开着，批准后**同一条流接着吐字**。若在这里 emit done，客户端会以为整个 run 结束了。

**`waiting` 算「进行中」**：`get_active_by_conversation` 包含它，审批没结束前同一个会话不能
再发消息；同时启动恢复（只扫 `running` / `pending`）不会误杀它。

**`approval_required` 事件由编排器发，不在节点里发**：LangGraph 恢复时会把节点函数**从头
重跑一遍**，`interrupt()` 之前的代码会执行两次，那里**不能有任何副作用**（事件、落库都不行）；
`interrupt()` 之后的代码只在恢复时跑一次。事件因此放在编排器收到 `__interrupt__` 时发。

### 16.5 驳回 = 终止本次 run

**决策**：驳回不「跳过这个节点继续跑」，而是**终止本次 run**（`status=rejected`，意见写进
`error_msg`）。

理由：驳回意味着「这件事不该按现在的样子做下去」。让它继续跑下去，会产出一个基于错误前提
的结果，而人已经明确说了「不行」。终止 + 记下原因，让发起人看到「这次没成，原因是 X」，
比让它跑完再给一个基于错误前提的答案有用。

节点里抛 `RunRejected` → 编排器捕获 → `emit done{status: rejected}` → `_finish(REJECTED)`。
审批意见同时进 `upstream`（按 `outputKey`）与上下文，落一条 assistant 消息——**万一**驳回
发生在 DAG 的另一条分支上，那条分支的节点虽然不会跑，审计信息仍然留得下来。

### 16.6 暂停点与审计记录：两份数据

| | checkpointer | `run_approval` 表 |
| --- | --- | --- |
| 回答 | 图停在哪、状态是什么 | 谁在什么时候批准/驳回了什么 |
| 需要它的是 | 恢复执行 | 审计 + 前端展示审批卡片 |
| 删了会怎样 | run 卡住，批了也续不上 | 丢审计历史，不影响任何在跑的 run |

`run_approval` 表因此是**可以重建**的：它不参与恢复，只是「谁批的」这件事的账本。

### 16.7 恢复可以发生在任意实例

决策通过 `Command(resume=decision)` 携带，**不依赖跨实例频道**——暂停点在共享的
checkpointer 里，图状态与事件流分工明确（前者管「停在哪」，后者管「发生过什么」）。

恢复前 `hub.register(run_id, start_seq=...)` 把 seq 计数器接上，`last_seq` 从暂停处续接。

### 16.8 踩到的坑

1. **`AsyncSqliteSaver.from_conn_string()` 只认纯文件路径**，不认 `sqlite+aiosqlite://` URL
   ——它把字符串原样交给 `aiosqlite.connect()`，后者只认路径或 `file:` URI，传 URL 会报
   "unable to open database file"。
2. **`state["__interrupt__"]` 是 list 不是元组**，取值要 `[0].value`。
3. **节点函数在恢复时会从头重跑**（见 §16.4），`interrupt()` 之前不能有副作用。
4. **迁移里的主键必须带 SQLite 回落**：`sa.BigInteger().with_variant(sa.Integer, "sqlite")`。
   漏掉的话，SQLite 上 `BIGINT` 主键不是 rowid 别名、**不会自增**，插入直接报
   `NOT NULL constraint failed: run_approval.id`。
   这个坑特别阴险：模型用的是带回落的 `PK`（`app/models/base.py`），ORM 建表（`create_all`）
   又是对的，于是**只有走迁移建出来的库会出问题**，而测试套件的库恰恰是 `create_all` 建的，
   根本测不到。为此在 `tests/test_migrations.py` 补了一条断言：迁移产物里每个表的主键列
   都必须声明成 `INTEGER`。

### 16.9 验证结果

- **9 个单测**（`tests/test_approval.py`）：5 个配置期校验 + 4 个挂起/恢复/驳回语义。
  用**真实的 LangGraph + 真实的 SQLite checkpointer**，不 mock——挂起的语义完全由框架决定，
  mock 掉它等于什么都没测。
- **6 个全链路 e2e**（`tests/test_approval_e2e.py`）：起**真实的 uvicorn**（不是
  ASGITransport——它会把整个响应体缓冲完才返回，SSE 这种「中途要交互、很久才结束」的流会被
  彻底堵死），加真实的 OpenAI 兼容假模型。覆盖批准走完、驳回终止、重复决策 409、
  非 waiting 状态 409、无待审批时返回 null、配置期拒绝。
- **浏览器实测**：批准 → 同一条流接着往下跑 → done；驳回 → run 终止、下游节点没跑；
  断线重连后审批卡片不重复出现。
- **全量回归**：144 passed。

### 16.10 已知限制

- **审批只有一个决策人**：没有「会签」「多人都批才算通过」。要支持得把 `run_approval` 的
  唯一约束从 `(run_id, node_id)` 改成 `(run_id, node_id, decided_by)` 并引入「凑够 N 票」的
  判定，那是另一种设计。
- **审批节点不能有超时自动通过/驳回**：`approval_timeout` 配置项已预留，但语义（超时算通过
  还是算驳回）没有定，因此没有启用。
- **`checkpointer` 的数据没有清理策略**：只有 run 进终态时删对应 thread。进程崩溃留下的孤儿
  thread 会一直在 Redis / SQLite 里——量很小，但确实没有兜底清理。

---

## 17. V3.2：Agent 工具调用闭环

### 17.1 目标与边界

补上一个一直存在的断口：**模型要了工具，run 就结束了，模型从没见过工具结果。**

项目里 `tools` 字段、`tool_choice`、流式 `tool_calls` 分片累积、`tool_call` 事件
早就在了，唯独缺**执行端**——模型说「我要调 calc」，服务端把这句话记进事件流，
然后就 `done` 了。模型下一轮根本没机会拿到那个结果，于是它要么编一个数字，
要么干脆不调。整条链路只差最后一环。

本轮范围：

- **内置工具注册表**：`now`（当前时间）、`calc`（算术求值）；
- **工具调用闭环**：模型要工具 → 服务端执行 → 结果以 `role=tool` 回灌 → 模型继续；
- **配置期校验**：未注册的工具名保存期就拒；
- **`tool_result` 事件** + 前端展示。

本轮**不做**：MCP（V4）、HTTP 工具、用户自定义工具。前两者是「工具来源」的问题，
本轮先把「来源是代码内置」这条最基础的路走通；用户自定义工具则**刻意不做**——理由见
§17.2。

> MCP 的设计见 **§21**：它扩的是「来源」，不改本节的闭环；本节立下的「工具名即契约」
> 与「读写宽严相反」两条规矩在 §21 里继续成立。

### 17.2 工具从哪来：只从代码里来

**agent 的 `tools` 字段存的是工具名**（`["calc", "now"]`），不是 OpenAI 的 function
schema。服务端从注册表（`app/core/tools.py`）里取 schema 展开给模型，执行逻辑也从同一张
表取。

**为什么不透传 function schema**：透传等于让**配置决定执行逻辑**。配置是数据，数据不该
有能力决定「服务端要执行什么代码」。本项目已有同样的先例——条件表达式不用 `eval`
（§15.3），而工具参数的危险等级只比条件表达式更高：参数直接来自**模型**，而模型会被
提示词注入影响。

| 方案 | 问题 |
| --- | --- |
| 透传 function schema | 配置里可以塞任意函数定义 ≈ RCE；且「模型能调的」与「服务端能执行的」是两套东西，对不上 |
| 远程 HTTP 工具 | 同上，只是把 RCE 换成 SSRF；且需要额外的出网策略与超时治理 |
| **代码内置注册表**（本轮） | 「模型能调的」与「服务端能执行的」是**同一张表**，不可能对不上 |

**工具名即契约**：`tools` 里写名字，schema 与执行器都从注册表取，所以「模型看到的
描述」与「服务端实际做的事」不可能分叉。加一个新工具只需要在注册表里加一条——
不用改配置格式、不用改前端。

**读的时候宽容，写的时候严格**：

- 写（`normalize_tool_names`）：未注册的名字**保存期就拒**（400）。配置根本进不了库，
  也就不会有「模型看得见、服务端执行不了」的工具；
- 读（`known_tool_names`）：历史脏数据（V3.2 之前 `tools` 是没人用的占位字段）一律丢掉
  不认识的部分，**不能因为一个脏值让整个 Agent 列表接口 500**。

另有一个只读接口 `GET /api/tools` 列出当前部署可用的工具，后台据此渲染勾选框——
用户不必凭记忆敲工具名，敲错了要等保存时才被拒。

### 17.3 闭环：模型要工具 → 执行 → 回灌 → 继续

`AgentRunner.run_with_tools()` 的形状：

```
第 1 轮：模型返回 tool_calls
   ↓ 执行工具（错误也不抛，作为结果）
   ↓ 追加 assistant 消息（带 tool_calls，id 必须配对）
   ↓ 追加 tool 消息（tool_call_id 与上面一致）
第 2 轮：模型看到工具结果，继续回答（可能再要工具）
   ↓ ……
最后一轮：模型不再要工具 → 收工
```

几个必须说清楚的决定：

- **结果要回灌，而不是直接结束**：模型要工具时，它的意图是「拿到数据再回答」。直接结束
  等于让它话说一半就没了。
- **文本是各轮拼接，不是只取最后一轮**：流里 emit 的 token 就是各轮的拼接，落库的
  assistant 消息也是同一个拼接，于是「流文本 == 库文本」这条 §14.3 依赖的性质依然成立。
  代价是中间轮模型的过程性发言（「我先算一下」）也会进最终产出——那本来就是它说出口的话。
- **assistant 消息要原样带回去**：OpenAI 协议要求 assistant 消息带上与后续 tool 消息配对的
  `tool_calls`（`id` 必须一致），否则端点会拒。
- **usage 跨轮累计**：多轮调用的 token 要加总，否则账单与统计会少算。
- **工具结果不进持久化 context**：它只是节点内的中间步骤，最终产出会以 assistant 消息进
  上下文。工具结果全量入上下文会让下一轮 prompt 迅速膨胀。

### 17.4 算术求值：不用 eval，用 ast 白名单

`calc` 工具的后端是 `app/core/arith.py`。

**为什么不用 `eval`**：`eval("__import__('os').system('rm -rf /')")` 是一个完全合法的
「表达式」。

**为什么用 `ast` 模块，而不是手写递归下降**：`ast.parse` 只**解析**、不求值，求值走本模块
自己的白名单遍历。能力上等价于手写解析器，但边界情况由 Python 自己的语法定义——手写
解析器最容易漏的恰恰是那些奇怪的合法写法（科学计数法、Unicode 数字、隐式括号……）。

**安全边界是白名单而不是黑名单**：只有列出的运算（`+ - * / // % **`、一元正负）与数字常量
被接受，其余节点类型一律拒绝。将来 Python 出现新的 AST 节点时也不会被静默放行。

**资源上限**（表达式求值看着无害，但 `2 ** 10000000` 能把 CPU 和内存吃光）：

| 上限 | 值 | 挡住什么 |
| --- | --- | --- |
| `MAX_EXPR_LEN` | 200 字符 | 超长表达式 |
| `MAX_NODES` | 100 个 AST 节点 | 深度嵌套 |
| `MAX_EXPONENT` | 100 | `2 ** 10000000` |
| `MAX_RESULT_BITS` | 1024 位 | 大整数滚雪球 |
| `MAX_RESULT_ABS` | 1e308 | 浮点溢出 |

**指数在求值之后、幂运算之前检查**：`10 ** 10 ** 10` 的内层先算成 `10^10`，正好被这条
挡住，不会真的去算那个天文数字。

### 17.5 工具报错不失败节点

工具执行失败时把错误文本**回灌给模型**，而不是让节点失败。理由：模型看到「参数错了」
有机会自己纠正（换个参数、或者干脆不用这个工具），比直接让整个节点挂掉有用得多。

**工具执行器用抛异常表达失败**，而不是返回一段错误字符串——返回值里混着「算出来的内容」
和「出错的说明」，调用方分不清，`ok` 标志也会失真。异常统一在 `execute_tool()` 里被转成
`(ok=False, 说明)`。

真正兜不住的是「模型一直调同一个必失败的工具」，那由轮次上限收口（§17.6）。

### 17.6 轮次预算与死循环

`tool_max_rounds` 是**模型调用次数**的上限，不是工具执行次数。模型在最后一轮还想要工具，
就判定它陷住了——这时**不执行**那次工具：执行了也没有下一轮去读结果，白跑一次。

于是预算的实际效果是：模型最多能用上 `max_rounds - 1` 个工具结果，并在第 `max_rounds`
次调用里给出最终答案。

**陷住时直接失败，不重试**：重试同一个节点，模型大概率还是同样地陷住，白烧一轮 token。
节点抛 `ToolLoopExceeded` → 编排器记 error step → run 以 `error` 收尾，`error_msg` 写明
「工具调用超过 N 轮，模型在工具上陷住了」。这条路径**绕开**了 §3.3 的通用重试。

### 17.7 事件与前端

| 事件 | 载荷 | 说明 |
| --- | --- | --- |
| `tool_call` | `{"toolCalls": [...]}` | 模型要调工具（V3.0 就有，本轮开始有后续） |
| `tool_result` | `{"tool", "ok", "summary"}` | 工具执行完了：调了哪个、成没成、结果摘要 |

两个都是**非终止事件**（与 §16.4 的结论一致：只有 `done` 才是终止事件）。

前端把工具行**插在节点的输出流里**，位置就是它发生的时刻——模型先问工具、拿到结果才
继续答，这条因果链在流里才看得出来。实现上文本与工具行混排在同一个容器里，因此
`appendToken` 不能再用 `textContent +=`（那会把工具行抹掉），改成「末尾还是文本节点就
接着写，否则新起一个」。

后台的 Agent 编辑器把 `tools` 从「手写 JSON」改成**工具勾选框**，选项来自
`GET /api/tools`——后端加了新工具，前端自动多一项。

### 17.8 踩到的坑

1. **模块级 async fixture 与函数级事件循环打架**：pytest-asyncio 报 loop scope 不一致。
   `server` 这类要起真实服务的 fixture 必须是**函数级**作用域。
2. **假模型按调用序取 `texts`**：不是「取没被工具轮用掉的那一个」。第 0 次调用走
   `tool_calls_plan`（不吐文本）时，`texts[0]` 其实是没用的，答案在 `texts[1]`。
3. **工具返回值不能混错误文本**：`_calc` 早期直接 `return f"错误：{exc}"`，于是
   `execute_tool` 报 `ok=True`——`ok` 标志失真，`tool_result` 事件也就说谎了。改成工具
   抛 `ToolCallError`、`execute_tool` 统一转成 `(False, 说明)`。
4. **`bd.textContent +=` 会抹掉子元素**：工具行是 div，文本追加会把它连同内容一起清掉。
5. **`<label>` 不能嵌套**：工具勾选框本身要是 label（点文字能勾上），外层就不能再是
   label，改用 `div.tool-sec`。
6. **指数检查的位置**：先算 `10 ** 10` 再检查指数，才拦得住 `10 ** 10 ** 10`；反过来
   （先检查后算）拦不住。

### 17.9 验证结果

- **15 个单测**（`tests/test_tools.py`）：算术求值（算得对 + 危险构造一律拒 + 四道资源
  上限）、工具注册表（工具名即契约、读写方向相反的宽严）、工具执行（出错不抛）。
- **6 个全链路 e2e**（`tests/test_tool_e2e.py`）：起**真实的 uvicorn**（不是
  ASGITransport——它会把整个响应体缓冲完才返回，SSE 会被堵死），加真实的 OpenAI 兼容
  假模型。覆盖：配置期拒绝未注册工具名、工具目录接口、闭环全链路（**断言了第二轮请求里
  确实带着 `role=tool` 的结果**）、工具报错不致命、轮次上限不重试、`now` 工具。
- **前端 jsdom 实测**：后台的勾选渲染与提交收集、验证台的工具行渲染与**顺序**（工具行
  夹在两段文本之间，没有被挤到末尾）。
- **全量回归**：166 passed（145 既有 + 21 新增）。

### 17.10 已知限制

- **只有两个内置工具**：`now` 与 `calc`。它们是「够用来验证闭环」的两个，不是「够用」的一批。
- **工具权限是全局的**：所有 agent 看到的是同一张注册表，没有「A agent 能用、B agent
  不能用」这层隔离。工具的**参数**危险（模型给的）已经处理了，工具**本身**的权限分级
  还没有。
- **工具串行执行**：模型一次要了三个工具，会一个一个跑完再回灌，不并发。
- **工具结果不持久化**：run 结束后再回看那条 run，看不到当时工具返回了什么（只在
  `run_event` 里有摘要与 `run_step.output_json` 里的调用记录）。
- **没有工具调用的审计表**：谁在什么时候调了哪个工具、参数是什么，只有事件流，没有像
  `run_approval` 那样的账本表。

---

## 18. 性能与容量

完整的实测报告与推算见 **[`scripts/PERFORMANCE.md`](../scripts/PERFORMANCE.md)**，压测脚本是
`scripts/bench.py`。这里只放结论与设计含义。

### 18.1 实测出来的成本模型

| 量 | 实测值 | 怎么来的 |
| --- | --- | --- |
| 每字的 CPU | 0.7–1.7 ms（规划取 0.8 / 1.1 / 1.5） | 服务端进程 CPU 时间 ÷ 产出的字数（1 字 ≈ 1 个 model token） |
| 每条并发流的内存 | 0.12–0.27 MB（规划取 0.3） | 每档的 RSS 增量 ÷ 该档成功流数 |
| 基线占用 | 155 MB | 空载 RSS |
| SQLite 的并发天花板 | 约 64 条流 | 64 并发 0 失败，128 并发 28–30 个 500 |

**服务端不是 CPU 密集型**：64 条并发流也只用掉不到 1 核——它绝大部分时间在等模型。

### 18.2 4c4g 能带多少并发

| 口径 | 4c4g |
| --- | --- |
| CPU | 2000–3750 字/s ≈ **40–75 人**（每用户 50 字/s） |
| 内存 | 约 9700 条短流（200 字）/ 约 970 条（2000 字） |
| SQLite | 约 64 条流 |
| 模型配额 | 取决于供应商，**通常是真正的天花板** |

中文 1 token ≈ 1～1.5 字，按 50 **model** token/s 的用户折算，CPU 口径实际是
**30～45 人**——这是规划时该用的数。

本服务的开销不是瓶颈；**模型供应商的并发配额才是**。要再往上走就加实例——
本服务无状态，V2 的 Redis 架构支持多实例，扩容是线性的。

### 18.3 压测带出来的两个真问题

1. **SQLite 并发写直接 500**（`database is locked`）。修法是 WAL + busy_timeout，
   详见 `app/config/db.py` 的说明。**试过 `BEGIN IMMEDIATE` 并放弃**——它把
   「报错」变成了「死锁」。结论：SQLite 只适合开发，生产用 MySQL。

2. **SSE 流一直占着 DB 连接**。FastAPI 的 yield 依赖在响应发完之后才关闭，
   而 SSE 响应一直开着——于是**每条开着的流都占住连接池里的一个连接**。
   MySQL 默认池是 `pool_size=10 + max_overflow=20`，也就是**整个服务最多 30 条
   并发流**，第 31 个请求堵在等连接上。

   这个比 SQLite 的 64 更早撞上，是并发上限的真正杀手。修法：SSE 路由把数据取完
   之后，**在返回流之前就 `await db.rollback()`**，连接立刻归还池子。
   回归测试见 `tests/test_db_concurrency.py`。

### 18.4 读这份报告时该记住的

压测客户端、假模型、服务端**同机抢 4 核**：实测的服务端 CPU 占用是**下界**，
延迟数据被大幅放大（瓶颈在假模型，不在服务端）。MySQL 路径**未实测**（本机没有
凭据）。完整的偏差清单见 `scripts/PERFORMANCE.md` §8。

---

## 19. PostgreSQL 适配

### 19.1 目标与边界

让 PostgreSQL 成为与 MySQL **平级**的生产数据库：改一行 `DATABASE_URL` 就能切，
业务代码一行不动。

**边界先说清楚**：本机没有 PG 实例，所以这套适配**没有在真 PG 服务器上端到端
跑过**。验证做到的是「方言编译 + 离线迁移 SQL + 引擎装配」这一层——它能盖住
「写出来的 DDL 在 PG 上根本执行不了」这类致命问题，但盖不住「约束行为、
JSON 往返、并发写」这些只有真库才暴露的差异。上线前必须在真实 PG 上跑一遍
`alembic upgrade head` 与全套测试。

### 19.2 为什么几乎不用改代码

repo 层（`app/repo/`）**全是 ORM**，没有一句方言专属的 SQL；模型层只用
`with_variant` 表达类型差异。所以适配面收敛在三处：引擎参数、连接事件、
类型变体。

| 维度 | MySQL | PostgreSQL | SQLite |
| --- | --- | --- | --- |
| 自增主键 | `BIGINT AUTO_INCREMENT` | `BIGSERIAL` | `INTEGER PRIMARY KEY`（rowid 别名） |
| 时间列 | `DATETIME(3)` | `TIMESTAMP WITHOUT TIME ZONE` | `DATETIME` |
| JSON 列 | `JSON` | `JSON` | `JSON`（SQLite 里是 NUMERIC 亲和性，只用于开发） |
| 布尔 | `SMALLINT` | `SMALLINT` | `SMALLINT` |

主键那条**不需要为 PG 加变体**：SQLAlchemy 的 PG 方言会把自增的 `BigInteger`
主键渲染成 `BIGSERIAL`（自带序列），已实测确认。

时间精度上 PG 是微秒、MySQL 是毫秒。本项目排序从不信时间（靠自增 id 与
`run_event.seq`），Python 侧写入也一律截断到毫秒，所以这个差异无害。

### 19.3 会话时区：PG 为什么走启动参数而不是 `SET`

这是本次适配里唯一需要动脑子的地方。

**问题**：PG 的 `now()` 返回带时区的 `timestamptz`，落进无时区的
`TIMESTAMP` 列时按**会话时区**折算。会话时区是本地时区的话，中国本机写的
时间戳会整整差 8 小时——而且**不报错**，只是时间线错乱。

**为什么不能沿用 MySQL 那套「连接后 `SET`」**：

- MySQL 的 `SET time_zone = '+00:00'` 是**非事务性**的，连接池归还连接时的
  `ROLLBACK` 撤销不了它，钉得住。
- PG 的裸 `SET TIME ZONE 'UTC'` 是**事务性**的。连接池归还连接时执行
  `ROLLBACK`，这条 `SET` 一起被撤销——下次取用同一条连接时，时区又变回
  服务器本地时区了。等于钉了个寂寞。

**采用的方案**：libpq 的 `options` **启动参数**，即
`options=-c timezone=UTC`。它在**连接建立时**由服务端生效，不属于任何事务，
`ROLLBACK` 撤销不了它 ✓。落地方式是 psycopg 的 `connect_args`，不需要用户改
URL：

```python
# config/db.py
if url.startswith("postgresql") and settings.db_pg_timezone:
    if "options" not in make_url(url).query:
        kwargs["connect_args"] = {"options": f"-c timezone={settings.db_pg_timezone}"}
```

**为什么还要检查 URL 里有没有 `options`**：用户可能自己带了
`?options=-c timezone=Asia/Shanghai`。psycopg 收到两个同名 `options` 会直接
`TypeError`，连接都建不起来——所以带了就不重复下发。

`DB_PG_TIMEZONE` 置空则完全不下发（服务器时区本来就是 UTC 时的省事写法）。

### 19.4 驱动选型：psycopg3

| 方案 | 结论 |
| --- | --- |
| `asyncpg` | 性能最好，但**没有 Python 3.14 的 Windows 轮子**，装它要源码编译（要 C 编译器）。 |
| `psycopg2` | 没有异步支持，与「全异步纪律」冲突。 |
| **`psycopg3`** | 纯 Python 部分 + `psycopg[binary]` 二进制轮子（cp314 有），同一套代码同时支持同步与异步，且是 SQLAlchemy 2.0 之后的官方推荐。 |

URL 形如 `postgresql+psycopg://user:pass@host:5432/agentdb`。

### 19.5 验证方式（不需要 PG 服务器）

`tests/test_postgres_compat.py`（12 条）覆盖五层：

1. **DDL 编译**：把同一套模型按三种方言各渲染一遍。PG 侧不得出现
   `AUTO_INCREMENT`、反引号、`DATETIME(` 等 MySQL 写法，且必须出现
   `BIGSERIAL` / `JSON` / `TIMESTAMP WITHOUT TIME ZONE`。
2. **结构等价**：三种方言的**表与列必须完全一致**（列名归一化掉引号后比较）。
   差异只允许出现在类型渲染上——少一列、改个名这类问题在单库测试里永远
   看不见。
3. **离线迁移**：`alembic upgrade head --sql` 在 PG 方言下生成 DDL。不连库、
   不建表，却能把三个迁移脚本连同方言分支一起验一遍——迁移脚本里写了 MySQL
   专属类型这类问题当场就暴露。
4. **引擎装配**：时区启动参数、连接池参数、URL 已带 `options` 时不重复下发、
   MySQL 与 SQLite 的参数不被波及。
5. **配置项映射**：`DB_PG_TIMEZONE` 这个环境变量名必须真的落到配置项上。
   名字写错的话配置项一直保持默认值，功能看着「正常」，用户怎么改环境变量
   都不起作用——这种坑要到部署到别的时区时才暴露。

### 19.6 已知限制

1. **未在真 PG 服务器上实测**（本机无 PG 实例）。上线前必须跑一遍真库的
   迁移与测试。
2. PG 的 `TIMESTAMP` 是微秒精度，MySQL 是毫秒（本项目不依赖时间排序）。
3. 走 PgBouncer **事务池**模式时，`options` 启动参数可能被中间件忽略——那种
   部署下需要 DBA 在库/角色层面设好 `timezone`。

---

## 20. V3.4：敏感词前置过滤

### 20.1 目标与硬约束

**提示词在进入 LLM 之前先过一遍**，命中就拦下，不让它到模型那里。

用户给的三条约束，逐条对应到设计：

| 约束 | 做法 |
| --- | --- |
| 资源占用要低 | 纯 numpy，**不加载任何模型框架**、不起任何外部服务；模型文件 48 KB，稠密权重 256 KB |
| 速度要快 | 单条判定 **0.12～0.33 ms**（毫秒级，比一次 LLM 请求快两个数量级） |
| 不用特别严格 | 宁可漏检不可误杀；阈值按「验证集误报率 ≤ 2%」挑，实测种子语料上**误报 0** |

### 20.2 为什么是机器学习而不是关键词表

关键词表（AC 自动机那套）只能挡住**字面完全一致**的词：写「怎么制作炸药」能挡，
写「怎么搞点炸的」挡不住，「杀 人」中间插个空格就漏。规则表永远追不上变形。

这里用的是**字符 n-gram + 逻辑回归**，纯 numpy 实现：

- **特征**：文本的字符 1/2/3-gram，用 `zlib.crc32` 哈希到固定维度的稀疏空间；
- **模型**：逻辑回归，权重是一维数组，推理就是一次稀疏点积 + 一次 `bincount`；
- **打分**：文本上滑窗，取**最高窗口**的分——窗口给出的是「哪一段有问题」。

哈希用 `zlib.crc32` 而不是 Python 内置 `hash()`：内置 hash 对 str 是**按进程加盐**
的（PYTHONHASHSEED），同一个词在不同进程里哈希值不同，模型文件存下来就没意义了。

**两个视图**：原文视图（保空格结构，seed 0）+ 压紧视图（去掉非字母数字，抗
「杀 人」「s-e-x」这类插空混淆，seed 1）。两个视图用不同种子，避免同一段字在两个
视图里撞到同一个槽位、权重互相污染。

### 20.3 三个把召回率拉上来的设计

这部分是实测出来的，不是拍脑袋的。三个改动每一个都有对照实验。

**（1）窗口级打分，不是整句级。** 一句话里提到敏感话题不等于在提敏感请求，整句
打分会把这种情况一起误杀。窗口还顺带提供「哪一段有问题」的定位。

**（2）窗口聚合用求和，不是平均。** 平均会把证据稀释掉——同一个敏感短语，窗口里
多一个字就掉一截分。实测召回从 0.88 掉到 0.3～0.6。

**（3）长度归一化指数 alpha。** 这一条是反直觉的，也是最关键的。

直接求和有个系统性的**长度偏置**：gram 总数越多，和越大——文本越长越容易被判
敏感；而短文本（「哪里能买到冰毒」7 个字）只有几十个 gram，同样的证据被摊薄成很低
的分，于是**系统性漏检**。

修法是给每个窗口乘一个系数 `(参考长度 / 实际 gram 数) ** alpha`，参考长度取**满窗**
的 gram 总数（`3L-3`），于是满窗系数恰好是 1，只有短窗被抬起来。

alpha 的最优值**随特征层变**。种子语料时代（无停用字掩码）实测 0.7 最好、1.0
反而回落（等于按平均证据量判，把「窗口里恰好有一段敏感」这个稀疏证据也一起稀释
了）；加了停用字掩码之后（见 §20.10）结论反过来了，0.9 明显更好：

| alpha | 袋外召回 | 袋外短句召回 | 袋外误报率 |
| --- | --- | --- | --- |
| 0.7 | 50.62% | 60.58% | 1.95% |
| **0.9（默认）** | **54.56%** | **68.88%** | 1.95% |

（K 折袋外、804 条语料、正例加权 16 倍。alpha 与正例加权是**互相影响**的，
所以这两个数必须在同一组配置下比。）

另外还有一条与召回直接相关的设计：**正例加权**（`--pos-weight`）。难负例一多，
语料正负比变成 1:3，模型学到的先验跟着偏负，正例分数被整体压低、召回跟着掉。
给正例加权把偏置拉回「敏感」一侧，实测袋外召回 47.9%（4 倍）→ 54.6%（16 倍）。

### 20.4 训练与推理必须同口径

训练脚本 `scripts/train_sensitive.py` **直接 import** `app/core/sensitive` 的特征
函数（`_normalize` / `_compact` / `_window_grams`）。两边口径不一致时，训出来
的权重在推理时全是噪声——而这种错在测试里很难看出来。

关键在于连**停用字掩码**也共用同一条路径：掩码一旦只在一侧生效，训练时模型
以为「这个字不算证据」、推理时却照算，权重就整体偏了，而且这种错不会报错，
只会让指标莫名其妙地差一截。

同理，模型文件里记着**窗口长度、步长、归一化指数**，推理时从 meta 里读（配置可以
显式覆盖）。重训换了窗口而推理没跟着换，是这类模型最典型、也最难查的错。

### 20.5 两道防线

1. **API 入口**（`app/api/routes/conversation.py::send_message`）：**落库之前**查。
   命中直接 422 返回，用户消息不入库、不建 run、不消耗任何 token。拦在后面就等于
   「先跑完模型再把结果扔掉」，白烧一次模型调用，还把敏感内容送到了模型那里。
2. **LLM 边界**（`app/core/agent_runner.py`）：把消息交给模型之前再查一遍。

第二道不是多余的：第一道只能拦住「走 API 的新消息」，而**历史消息**（早于过滤功能
入库的）、**绕开 API 的内部调用**、**复用本模块的别的服务**都逃得掉。有第二道，
「进入 LLM 的内容一定被过滤过」才是一条**对所有路径成立**的不变量，而不是一句约定。

编排器对 `SensitiveContentError` **不重试**：重试同一段内容只会得到同样的判定，
纯属白等两轮退避——与工具死循环同理，不可恢复的错误要立刻失败。

### 20.6 命中之后：为什么是拒绝而不是遮蔽

默认动作是**拒绝**（API 返回 422 / `sensitive_content`）。

遮蔽（把命中词替换成 `*` 再放行）看起来更「温和」，但它是**最危险**的中间态：
模型拿到的是语义完整的请求，只是词被替换了——那种「看起来处理过了其实没处理」
的状态，既骗过了用户也骗过了自己。过滤器是「安全前置」，它的价值在于**确定性**
地不把内容送出去。

命中后只有一条路径（拒绝），不做「遮蔽放行」的第二种行为——多一种行为就多一套要
测的分支，而遮蔽那一支的价值又恰恰在这里最可疑。

### 20.7 失败模式：模型读不出来就启动失败

`init_filter()` 在 lifespan 里调，模型文件缺失或损坏直接让服务**起不来**。

理由和其它几处一致（Redis 连不上、checkpointer 初始化失败都是直接失败）：静默放行
等于把「安全前置」变成一句空话，而这类失败恰恰是**最难在事后发现的**——没有报错、
没有告警，只是过滤没生效，等到出事时才发现它从来没工作过。

要关就显式设 `SENSITIVE_ENABLED=false`，那是有记录的、可审计的（配置里有痕迹，
启动日志里也有一行）。

### 20.8 推理提速：整段哈希一次，窗口间切片

窗口互相重叠（窗口 20 / 步长 4 → 每个 gram 被算 5 遍），最初实现是**逐窗哈希**，
等于把同样的哈希重复做 5 遍。实测单视图打分 0.27 ms 里有 0.22 ms 花在这上面。

改成**整段文本算一次哈希表、窗口之间再切片**之后：

| 提示词长度 | 优化前 | 优化后 |
| --- | --- | --- |
| 7 字（短命令） | 0.10 ms | 0.12 ms |
| 48 字（典型请求） | 0.59 ms | **0.33 ms** |

短文本反而慢了一点点（numpy 的拼接与 `repeat` 开销摊到很少的 gram 上）——典型的
「优化对大输入友好、对小输入不划算」。这个取舍是划算的：真实提示词大多是几十字
以上，而短文本那 0.02 ms 的差别无关紧要。

**等价性是验证过的**：重构前后对 186 条文本逐条比分数，最大差 4.96e-5，正好是
分数保留 4 位小数的舍入量级。

### 20.9 语料与能力边界

仓库里带了一份训练好的模型（`app/core/sensitive_model.npz`，158 KB），开箱即用。
它是用 `scripts/sensitive_corpus.py` 的语料训的——**804 条**短句，覆盖五大类
（违法暴力 / 色情 / 政治敏感 / 隐私诈骗 / 仇恨歧视），外加两类负例。

三类样本各司其职：

- **正例**：真正的敏感请求，五大类。含**间接说法**（「炸药一般是怎么做的」
  「我想了解制毒需要哪些材料」）——直白命令只是真实提示词的一部分。
- **普通负例**：日常请求。它们的作用是**压住误杀**。
- **难负例**（463 条，占语料一半以上）：**含敏感词但无害**的请求（「帮我写一个
  敏感词过滤器的实现思路」「我要举报一个诈骗网站，怎么举报」）。它们是这份语料
  里最关键的一部分：模型要学的不是「这个词敏感」，而是**「这个请求在做什么」**。

**难负例还必须和正例在句式上对称**。踩过的坑：负例清一色是「帮我写**个** X」，
正例清一色是「帮我写**一个** X」，模型于是学到了一个纯语法的假特征——「写一个」
= 敏感。结果「帮我写一个单元测试」被拦、「怎么申请护照」被拦。补齐两侧的句式之后
误报才归零。

**难负例要按误杀模式来扩，不是按数量来扩。** 旧模型在 72 条难负例上零误杀，
扩到 463 条之后实测误杀率 **10.15%**——新难负例就是按旧模型的实际误杀点写的
（密码、个人信息、禁毒科普、AI 内容识别、成人教育……）。一条难负例要教会模型
一件事：「这个词无害」取决于它**和什么搭配**，不取决于词本身。

### 20.10 实测指标（如实给出，包括不好看的那部分）

#### 怎么量

误报率是会漂的指标，测量口径必须写死，否则每次重训都只能凭感觉说「好像好一点」。

**三个互相独立的观测角度**：

1. **留出集**（`scripts/sensitive_eval_corpus.py`，118 条负例 + 39 条正例）——
   **从不参与训练，也不参与选阈值**，是这份报告里最可信的两个数字。
2. **训练集**（已拟合）——只作对照，看有没有过拟合的迹象。
3. **仓库文档段落**（486 段真实中文长文本）——第三个独立角度，用来交叉验证。
   这一项的样本是**仓库自己的文档**，所以改文档就会改它（本次改写之后它从
   469 段变成 486 段，旧模型的误报率也从 2.56% 变成 3.50%——新写的这段
   「功能字权重爆炸」正好把旧模型的误伤点又演示了一遍）。引用这个数字时
   要连同文档修订一起说。

**阈值用 K 折袋外（OOF）预测选**。每条文本都有一个「模型没见过它」的分数，
样本量是全量语料。比单次 20% 切分稳得多：实测单次切分选出的阈值是 0.870，
把召回压到 0.46——800 多条语料切一次只剩 160 条验证样本，误报率每 1% 只对应
两条样本，选出来的阈值纯粹是噪声。

**留出集绝不参与选阈值**。拿了它挑阈值，报告出来的数字就是自己调出来的，
不算独立评测。

#### 数字

| 指标 | 旧模型 | 新模型 |
| --- | --- | --- |
| 留出集 · 误报率 | 0.00% | 0.85% |
| 留出集 · 召回率 | 79.49% | **84.62%** |
| 仓库文档（486 段中文长文本） · 误报率 | 3.50% | **0.82%** |
| 难负例（463 条） · 误报率 | 10.15% | **0.22%** |
| 普通负例（100 条） · 误报率 | 0.00% | 0.00% |
| 敏感请求埋进长句 · 召回率 | 56.41% | **66.67%** |
| 分类召回（暴力/色情/政治/诈骗/仇恨/间接） | 86%～94% | **六类全 100%** |

阈值 0.815，由「袋外误报率 ≤ 2%」这个约束挑出（袋外实测 1.95%）。

**必须说清楚的边界**：

1. **这是「开箱可用」级别，不是生产级内容审核。** 804 条语料训出来的模型只
   覆盖常见形态，挡不住所有变形。
2. **敏感请求埋进长句里，召回会明显下降**（留出集 0.67，袋外只有 0.40）。原因在
   §20.3 (3)：窗口里那些正常字的权重是**负的**（模型要靠它们压住普通请求），
   累加起来会把敏感证据稀释掉。训练侧要根治它得给窗口打**逐窗口**的标签
   （只有盖住敏感短语的那几个窗口算正例），那是另一套标注口径的事。
3. **留出集只有 118 条负例**，误报率的分辨率是 0.85% 一格。这个精度下
   「0.85% vs 0.00%」的差别并不显著——真正硬的数字是难负例上的 10.15% → 0.22%。
4. **袋外召回 0.55 与留出召回 0.85 差得远**，因为袋外集里有一半是「埋进上下文」
   的长句形态（0.40）。两个数都在这儿，别拿 0.85 去承诺什么，也别拿 0.55 去
   否定什么。

#### 误报率是怎么降下来的

三层根因，按贡献排序：

1. **功能字的单字特征权重爆炸（主因）**。字符 n-gram 模型学的是「词与标签的
   共现频率」，而正例句式高度同质（「教我怎么怎么X」「帮我查一下这个人的Y」），
   于是「人」「的」「怎」「家」这些字在正例里出现得远比负例多，逻辑回归给它们
   分配了大正权重——单字「人」+0.64。后果是任何含「人」「的」「怎么」的普通文本
   都能凑出高分：「每用户输出速率」被拦（0.94）、「没人用」被拦（0.84）、
   「返回本进程的实例标识」被拦（0.82）。

   修法是**停用字掩码**：把虚词与泛指字的单字特征标成无效证据（2/3-gram 词组
   原样保留——意图信息在词组里，「色情」「制毒」「获取」「煽动」都是 2-gram）。
   停用表里**刻意不含**「毒」「杀」「枪」「偷」「抢」「炸」「性」这些核心信号字。

2. **正负比失衡导致先验偏负**。难负例一多，语料正负比变成 1:3，模型学到的先验
   跟着偏负，正例分数被整体压低、召回跟着掉。修法是**正例加权**（`--pos-weight`，
   实测最优 16 倍）：袋外召回 47.9% → 54.6%。

3. **哈希冲突重复计权（次要）**。同一个字落在两个哈希槽位上、各自学成高权重，
   20 字窗口里 5 个「人」累加出 3.2 的贡献。修法是把维度从 2^16 提到 2^18。

   **但实测这一条几乎没有效果**——把维度从 2^16 升到 2^18，两组指标完全相同。
   之前文档里写的「2^18 反而让模型背下训练样本」是在**几百条的小语料**上量的，
   语料扩到近千条之后那个结论不再成立。真凶是第 1 条，不是这一条。

### 20.11 用自己的数据重训

```bash
# 用内置语料训练（默认，K 折选阈值，约 80 秒）
python -m scripts.train_sensitive --dry-run          # 只看评估，不写文件
python -m scripts.train_sensitive --out app/core/sensitive_model.npz

# 用自己的语料重训
python -m scripts.train_sensitive --data my_corpus.jsonl --out app/core/sensitive_model.npz

# 评测任意一个模型（留出集 / 训练集 / 仓库文档 / 难负例 / 分类召回 + 阈值曲线）
python -m scripts.eval_sensitive --model app/core/sensitive_model.npz
```

语料格式是 JSONL，每行 `{"text": "...", "label": 1|0}`，label=1 表示敏感。
**难负例很重要**：含敏感词但无害的样本教模型区分「词」和「意图」，没有它们，
误杀率会高得没法用。训练几百到几千条只要一两分钟。

可调超参：`--epochs` / `--dim` / `--window` / `--stride` / `--norm-alpha` /
`--max-chars` / `--pos-weight` / `--lr` / `--l2` / `--cv-folds` / `--seed`。

- `--dim` 是**容量**。旧文档写「给大反而更差」，那是几百条语料上的结论；
  语料扩到 804 条后实测 2^16 与 2^18 指标完全相同，2^18 只少了重复计权。
- `--pos-weight` 是**正例样本权重**，语料正负比越偏负越该调大。
- `--cv-folds` 是**选阈值用的折数**，默认 5；调成 1 会退回单次切分（快，但阈值不稳）。
- `--norm-alpha` 实测 0.9 优于 0.7（袋外召回 54.6% vs 50.6%，误报率相同）。

### 20.12 已知限制

1. **纯 CPU、同步计算**。单条 < 0.4 ms，但它是**同步**调用，在极高并发下会占用
   事件循环的时间片。本项目是 IO 密集型（每次生成都要等 LLM），这个量级不影响吞吐；
   如果哪天要做十万级 QPS 的内容审核，应该把它挪到独立的过滤服务。
2. **单条上限 4000 字**（`SENSITIVE_MAX_CHARS`）。超长的只扫前面这段——敏感内容
   几乎总在开头，且这一步是为了**控住资源占用**。
3. **不记命中词**。推理日志只记分数与位置，不记命中片段——把敏感词写进日志等于给
   日志库造一份副本，审计与合规都会变成笑话。响应体里会回显一小段（用户自己的
   原文），帮用户知道该改哪句。
4. **误报率只在本仓库的语料上测过**。留出集与难负例都是自己写的，覆盖的是「我
   想到的表达方式」，不是真实用户输入的分布。上生产前应该拿**真实日志**（脱敏后）
   再量一遍，并把线上误杀样本回流成语料。
5. **袋外分数与最终模型的分数尺度不完全一致**。袋外模型各训 80% 语料，最终模型
   用全量，实测差异在 1～2 条样本量级，可以接受，但它意味着阈值是**近似**最优
   而不是严格最优。
6. **不处理图片、语音、多模态内容**。本项目目前也没有这些入口。

---

## 21. V4：MCP 工具（外部工具来源）

### 21.1 目标与边界

V3.2 把「模型要了工具，run 就结束了，模型从没见过工具结果」这个断口补上了，但工具的**来源**
被刻意钉死在代码里：注册表 `TOOL_REGISTRY` 里就 `now` 与 `calc` 两个（§17.2）。本轮把来源扩到
**MCP 服务器**，让「工具从哪来」不再等于「工具必须是我们自己写的」。

本轮要做的：

- **MCP 客户端**：本项目作为 MCP **host / client**，连接外部 MCP 服务器（stdio 子进程 /
  Streamable HTTP 远程端点）；
- **工具目录并表**：内置注册表与 MCP 工具目录合成一张表，`agent.tools` 仍然是「工具名列表」，
  只是允许出现 `mcp__` 限定的名字；
- **调用链复用**：模型要工具 → 服务端执行 → 结果以 `role=tool` 回灌 → 模型继续——§17.3 的闭环
  **一个字都不改**，只在「工具名 → 执行器」这步多一条分支；
- **工具目录接口**：`GET /api/tools` 的形状不变，条目多一个 `source` 字段。

本轮**不做**（每一条都有理由，不留「以后再说」）：

| 不做的东西 | 为什么 |
| --- | --- |
| 实现 MCP **服务器** | 本项目是客户端。做一个 server 是另一个产品线的事 |
| MCP 的 **resources / prompts** | 协议里还有这两类能力。本轮只接 tools；resources 的接入留给 V4.2，与 Skill 的资源读取一起做（§22.7） |
| 服务端**主动推送**（`notifications/tools/list_changed`、`subscriptions/listen`） | 工具清单在**连接建立时快照**，之后不变；要变就重连。推送要维护长连接与订阅状态，收益远小于成本 |
| **OAuth 授权流** | MCP 的授权是一整套（OAuth 2.1 + 动态注册 + RFC 9728 资源元数据 + `iss` 校验）。本轮只支持管理员配好**静态 header**（如 `Authorization: Bearer …`）的服务器 |
| **MRTR**（多轮往返请求） | 2026-07-28 新增的 `input_required` 结果类型。本轮一律当失败处理（§21.9） |
| MCP 工具的**代码级沙箱** | 见 §21.10 第 1 条——这是本设计里唯一一处「配置能决定执行什么代码」的地方，必须正面处理而不是绕过 |

### 21.2 MCP 解决的是「来源」问题，不是「能力」问题

§17.2 立的规矩是：**「模型能调的」与「服务端能执行的」必须是同一张表**，否则透传一份
function schema 就等于让配置决定执行逻辑（≈ RCE）。

MCP 不推翻这条规矩，它换的是**这张表由谁填**：

| | V3.2 内置工具 | V4 MCP 工具 |
| --- | --- | --- |
| 表项由谁提供 | 我们自己写的代码 | 第三方进程 / 第三方服务 |
| schema 与执行器 | 同一处（`ToolSpec`） | 同一处（MCP 服务器：它给的 `inputSchema` 就是我们要发的 `arguments`） |
| 配置能否决定执行什么 | **不能**——配置只能填名字 | **stdio 能**——配置里就有 `command` / `args`（见 §21.10） |
| 保存期能否校验 | 能，注册表即全集 | 只能对**已发现过的**服务器校验（§21.8） |
| 参数危险等级 | 低（`now` / `calc`） | **未知**，取决于那台服务器 |

关键点是第二行：MCP 工具的 schema 与执行器**天然同源**——服务器自己声明参数形状，自己执行。
这正是它相对「用户自定义 HTTP 工具」的根本优势：后者要么透传 schema（回到 RCE 问题），要么
服务端自己猜参数形状（模型看到的与执行的会对不上）。

所以本轮的设计目标不是「把 MCP 接进来」，而是**在不动 §17.2 那条契约的前提下，把表的另一半
交给外部**。为此需要一张**工具目录快照表**（§21.6）：它让「模型能调的」与「服务端能执行的」
在**快照层面**仍然对得上。

### 21.3 协议版本：钉在哪一版

MCP 规范按日期版本化，且**版本之间有破坏性变更**。截至本设计定稿，规范的最新修订是
**2026-07-28**，它在 2025-11-25 基础上做了大改：

| 维度 | 2025-11-25 及更早（握手时代） | 2026-07-28（现代时代） |
| --- | --- | --- |
| 连接建立 | `initialize` 握手协商版本 | **无握手**，每个请求在 `_meta` 里带协议版本与客户端能力 |
| 会话 | `Mcp-Session-Id` 头维持会话状态 | **移除协议级会话**；跨调用状态改由服务端自己铸一个 handle 当普通参数传 |
| 服务端身份 | `initialize` 结果里返回 | 每个结果的 `_meta.io.modelcontextprotocol/serverInfo` |
| 能力发现 | `initialize` | 新增 `server/discover`（服务端必须实现） |
| 服务端推送 | GET 长连接 SSE + `resources/subscribe` | 改为 `subscriptions/listen`（单一长连接 POST 响应流） |
| 结果形状 | 无此字段 | **所有结果必带 `resultType`**：`complete` / `input_required` |
| 列表结果缓存 | 无 | `tools/list` 等结果必带 `ttlMs` + `cacheScope` |
| HTTP 必填头 | `MCP-Protocol-Version` | 再加 `Mcp-Method`、`Mcp-Name` |

**对本项目最要紧的一条是「无会话」。**

§13 的多实例架构里最难啃的是「状态归属」：Redis Stream 把事件归属解决了，但任何**有状态的
外部连接**都会带来会话亲和性问题——实例 A 建立的会话，实例 B 接不上。审批暂停点能跨实例恢复
（§16.7），是因为它存在 Redis 里；而 MCP 会话是协议层的活连接状态，没地方放。

2026-07-28 把会话从协议里删掉，正好撞上本项目「核心状态在 MySQL / Redis，进程内只放无状态
计算」的原则。所以：

- **目标协议版本：2026-07-28**（无状态）。Python SDK 会优先使用它。
- **最低可接受：2025-06-18**（握手时代）。更早的不支持。
- **本项目不自己写版本协商代码**，只声明「支持哪些版本」，协商交给 SDK
  （`mcp.types.SUPPORTED_PROTOCOL_VERSIONS` 覆盖两个时代）。

**但现实是握手时代的服务器仍然占多数**，它们会拿到 `Mcp-Session-Id`，也就把会话亲和性问题带
了回来。本设计的处理口径是：

> 架构上按 2026-07-28 的无状态模型设计（**不依赖**会话存在）；握手时代服务器能连，但接受
> 「同一会话要落在同一实例」这个代价，并在 §21.14 第 1 条如实列出。

具体做法：握手时代服务器的 `Mcp-Session-Id` **存进 Redis**（键 `mcp:sess:{server_code}`），
任何实例都能用它继续对话，而不是把连接钉死在发起它的进程里。连接本身仍是进程内的（stdio
子进程没法跨实例），所以**子进程型服务器天然单实例**——这是物理限制，不是设计选择。

### 21.4 传输：stdio 与 Streamable HTTP

选两种，是因为它们覆盖两类真实用法；旧的 HTTP+SSE 双端点传输**不实现**（它在 2025-03-26
被废弃，2025-11-25 起进入正式 Deprecated 生命周期）。

| 传输 | 典型场景 | 本项目怎么接 |
| --- | --- | --- |
| **stdio** | 本地子进程：文件工具、脚本工具、代码执行器 | 以子进程拉起，stdin/stdout 走 JSON-RPC 消息；**stderr 单独收进日志** |
| **Streamable HTTP** | 远程服务：云 API 封装、内部平台 | 单端点 POST；服务端要主动推消息时走响应流 |

三个必须记住的细节：

1. **stdout 只能有协议消息**。stdio 模式下服务端的普通日志**必须走 stderr**，写 stdout 会污染
   JSON-RPC 帧流。同理，本项目作为客户端拉起子进程时，要**主动把子进程的 stdout 收干净**，
   不能让它继承父进程的 stdout——否则子进程随手一句 print 就把协议打乱了。
2. **`MCP-Protocol-Version` 头**（2025-06-18 起）：每个 HTTP 请求都要带协商好的版本；缺失时
   服务端按 2025-03-26 处理，收到非法值返回 400。
3. **SSE 断流 = 这次请求丢失**。2026-07-28 移除了 SSE 的可续传性（`Last-Event-ID` 与事件 ID
   不再是规范的一部分），规范明确要求客户端**用新的请求 ID 重新发起**。这对工具调用有直接
   含义：**重试一个超时了的 MCP 工具调用，必须假设它可能已经执行过**（§21.9）。

### 21.5 工具命名空间：`mcp__<server>__<tool>`

内置工具叫 `calc`、`now`；MCP 工具有自己的名字，两台服务器的同名工具会撞。

OpenAI function-calling 的函数名约束是 `^[a-zA-Z0-9_-]{1,64}$`——**没有点号**。所以
`weather.get_weather` 非法，`weather__get_weather` 合法。

采用 Claude Code 的约定：

```text
mcp__<server_code>__<tool_name>
```

- `server_code`：服务器在本项目 `mcp_server` 表里的 `code`，**不是** MCP 协议里的 `serverInfo.name`
  ——后者是服务端自报的，不同服务器可能重名，不能拿来当主键；
- `tool_name`：MCP 服务器自己的工具名，原样保留。导入时按 OpenAI 的字符集与长度卡，
  不满足的**不导入**（并在服务器健康检查里报出来）；
- 长度上限 64 是 OpenAI 的限制，不是我们的选择：`mcp__`(5) + `server_code` + `__`(2) +
  `tool_name` ≤ 64。超长的**不导入**，**不截断**——截断会把两个不同的工具撞成同一个名，
  那比不导入更糟。

于是 `agent.tools` 的**形状变了，语义没变**：它还是「工具名列表」，只是现在可能混着内置名与
`mcp__` 限定名。§17.2 立的契约依然成立：**模型能调的名字，必然是服务端能执行的名字**——
只是「服务端」从「本进程里的注册表」扩展到「注册表 ∪ 已发现且在线的 MCP 服务器」。

### 21.6 数据模型

#### `mcp_server` — MCP 服务器注册表

```sql
CREATE TABLE mcp_server (
  id          BIGINT PRIMARY KEY AUTO_INCREMENT,
  code        VARCHAR(64)  NOT NULL UNIQUE,
  name        VARCHAR(128) NOT NULL,
  transport   VARCHAR(16) NOT NULL,        -- 'stdio' | 'http'（不用 ENUM：三方言通用）
  -- stdio：启动命令与参数
  command     VARCHAR(512) DEFAULT NULL,
  args        JSON        DEFAULT NULL,
  -- stdio：子进程环境变量（**值加密**）
  env         JSON        DEFAULT NULL,
  -- http：端点 URL
  url         VARCHAR(512) DEFAULT NULL,
  -- http：请求头（**值加密**）
  headers     JSON        DEFAULT NULL,
  -- 工具白名单：只导入这些工具；为空 = 全导
  allow_tools JSON        DEFAULT NULL,
  timeout_sec INT         DEFAULT 30,
  enabled     TINYINT(1)  DEFAULT 1,
  version     INT         DEFAULT 1,
  created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

`env` 与 `headers` 里装的就是凭据，加密口径与 `agent.api_key_cipher` 完全一致
（`app/core/crypto.py` 的 Fernet）。这两列**只写不读**：任何 API 响应里都不回显明文。

回显的口径是**只给键名**（响应里的 `env_keys` / `header_keys`）——不是把值替换成
`***`。理由是：连键名都没必要藏（管理员本来就知道自己配了哪些键），而给出一堆
`***` 只会让前端分不清「配了但值是空的」和「配了有值」。值一个字节都不回。

#### `mcp_tool` — 工具目录快照

```sql
CREATE TABLE mcp_tool (
  id             BIGINT PRIMARY KEY AUTO_INCREMENT,
  server_id      BIGINT      NOT NULL,
  qualified_name VARCHAR(128) NOT NULL,   -- mcp__<code>__<tool>
  tool_name      VARCHAR(64)  NOT NULL,   -- MCP 服务器自己的名字
  description    TEXT         DEFAULT NULL,
  input_schema   JSON         DEFAULT NULL,
  output_schema  JSON         DEFAULT NULL,
  annotations    JSON         DEFAULT NULL,
  discovered_at  DATETIME(3)  DEFAULT CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_qualified (qualified_name),
  KEY idx_server (server_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

这张表有**三个**用途，缺一不可：

1. **保存期校验**：`normalize_tool_names` 遇到 `mcp__` 前缀的名字，去这张表查——查不到就拒
   （§21.8）；
2. **展开 schema 的数据源**：`build_openai_tools` 从这里取 `input_schema` 拼 OpenAI 的 `tools`
   参数。但**离线服务器的工具不展开**（§21.9）——快照只负责「有哪些工具」，「现在能不能用」
   由在线状态决定；
3. **后台勾选框的数据源**：`GET /api/tools` 要回答「这台部署上有哪些工具可用」，MCP 工具的
   来源就是这张表。

快照是**发现时写入**的，不是实时的。`tools/list` 有分页，而服务端推送本轮不实现（§21.1），
所以工具清单在**连接建立时取一次**，之后以快照为准。

### 21.7 连接管理

`McpClientManager` 是**进程内单例**，按 `server_code` 管理连接。

| 关注点 | 口径 |
| --- | --- |
| 什么时候连 | **惰性**：第一次用到该服务器的工具时才连。服务启动不因为某台 MCP 服务器挂了就失败 |
| 什么时候断 | 空闲超时（默认 5 分钟）后主动关闭——**stdio 子进程必须回收**，否则会攒出一堆僵尸进程 |
| 断了怎么办 | 下次调用时重连，指数退避，最多 3 次；仍失败则该服务器标记为不可用 |
| 同一服务器的并发 | **并发**调用（JSON-RPC 的 `id` 天然区分请求），但每个服务器有并发上限信号量（默认 4），避免把远端打爆 |
| 跨实例 | 连接是进程内的。握手时代服务器的 `Mcp-Session-Id` 存 Redis（§21.3），让任意实例都能续上对话 |

**为什么不预热**：预热会让启动变慢，且一台慢服务器拖慢整个启动。**为什么不因为连不上就启动
失败**：MCP 服务器是**可选依赖**，不是服务运行的前提。一台 MCP 服务器挂掉，影响的是**配了它的
那几个 Agent**，不是整个服务。

> 这与 §13.5 的 Redis 策略**故意不同**，理由要说清楚：Redis 不可用是**全局**能力失效
> （事件收不到、流会卡死），静默退回进程内总线会造成「看起来能跑、实际跨实例收不到事件」的
> 分裂大脑；而 MCP 服务器不可用只让**部分 Agent** 退化成「没有这个能力」，且这个退化是
> **显式且可观测**的（§21.12）——离线服务器的工具不会展开给模型，模型压根看不见它。

### 21.8 工具目录：发现、快照与保存期校验

三段式，和 §17.2 的「写的时候严格，读的时候宽容」是同一个思路，但**严格的对象变了**。

**① 发现**：连接建立后调 `tools/list`（注意分页，`nextCursor` 要翻到底），把每个工具写进
`mcp_tool` 快照。写之前先删掉该服务器旧的快照行再整体插入（快照是全量替换，不是增量合并
——增量合并会留下服务器已经删掉的工具）。

**② 保存期校验**（`normalize_tool_names`）：

| 名字形态 | 校验方式 | 不通过时 |
| --- | --- | --- |
| 内置名（`calc`） | 在 `TOOL_REGISTRY` 里 | 400，配置存不进去 |
| `mcp__<code>__<tool>` | 在 `mcp_tool` 快照里 | 400，配置存不进去 |
| 其它形状 | 既不是内置名也不是 `mcp__` 限定名 | 400 |

**MCP 服务器从未连过时**，`mcp_tool` 里没有它的任何工具 → 保存期直接 400，报错写明
「服务器 X 尚未发现工具，先刷新一次目录」。这是有意的严格：宁可让配置保存失败并给出可执行的
提示，也不要留一个「配了但模型看不见」的哑配置。

**③ 运行期宽容**（`known_tool_names` 的同类）：读历史脏数据时，未知名字**丢掉**而不是报错——
一个脏值不能让整个 Agent 列表接口 500（这是 §17.2 已经踩过的坑）。

### 21.9 工具调用链：从模型请求到结果回灌

```mermaid
sequenceDiagram
    participant LLM as 模型
    participant R as AgentRunner
    participant D as 工具分发
    participant M as McpClientManager
    participant S as MCP 服务器

    LLM->>R: tool_calls: mcp__weather__get_weather
    R->>D: execute_tool(name, args, allowed)
    D->>D: 白名单校验（agent.tools）
    D->>M: 查 server_code / tool_name
    M->>S: tools/call
    S-->>M: CallToolResult(content, structuredContent, isError)
    M->>M: 结果映射 → 文本
    M-->>D: (ok, text)
    D-->>R: (ok, text)
    R->>R: role=tool 消息回灌 + tool_result 事件
    R->>LLM: 下一轮
```

**分发规则**（`execute_tool` 里加一条分支，闭环其余部分不动）：

```
name.startswith("mcp__")
    → 解析 server_code / tool_name
    → 白名单校验（allowed 列表里必须原样含这个限定名）
    → McpClientManager.call(server_code, tool_name, args)
else
    → 现有 TOOL_REGISTRY 路径，一字不改
```

**结果映射**——MCP 的 `CallToolResult` 和我们的 `(ok, text)` 之间需要一个明确的转换：

| MCP 侧 | 我们的侧 | 说明 |
| --- | --- | --- |
| `content: [TextContent]` | `ok=True`，`text` 为其文本 | 常规路径 |
| 多个 `TextContent` | `ok=True`，用 `\n` 连接 | 顺序保留 |
| `structuredContent` 且无 text | `ok=True`，`text` 为其 JSON 序列化 | 规范要求有 structured 时**同时**给一份 text，这条兜底 |
| `isError: true` | **`ok=False`**，`text` 为其内容 | 工具**执行**错误——模型有机会自己纠正，与 §17.5 同一口径 |
| JSON-RPC 协议错误 | **`ok=False`**，`text` 为错误说明 | 未知工具、参数非法等 |
| 超时 / 连接断开 / 服务器不可达 | **`ok=False`**，`text` 为错误说明 | 同样**不失败节点**，回灌给模型 |
| 非文本内容（image / audio / resource_link / embedded_resource） | `ok=True`，`text` 为占位说明 | 见下 |

**非文本内容为什么要占位**：OpenAI 的 `role=tool` 消息只接受字符串内容，图片/音频塞不进去。
所以 `ImageContent` / `AudioContent` / `ResourceLink` 在文本位置写一句占位
（如「返回了 1 张图片（image/png，20480 字节），本通道暂不支持直接回灌」），让模型知道
「有东西但你看不到」，而不是让它以为工具返回了空结果。`EmbeddedResource` 里 `mimeType`
是 `text/*` 时可以把 `text` 成员提取出来当普通文本用。

**所有错误一律回灌，不失败节点**——这是 §17.5 的规矩，MCP 不例外。真正兜不住的是「模型一直调
同一个必失败的 MCP 工具」，那由 §17.6 的轮次上限收口。

**一个例外：服务器被证明不可用时，不把它的工具展开给模型。**

`build_openai_tools` 只展开**可用**服务器的 MCP 工具。不可用服务器的工具仍然在
`mcp_tool` 快照里（保存期校验、后台勾选框都要用），但**不给模型看**——模型看不见
就不会去调一个调不通的工具。这比「照常展开、每次调用都失败」好：后者会让模型反复
重试同一个坏工具，白烧轮次与 token，最后撞上轮次上限失败。

这里的「可用」是「**没被证明不可用**」，不是「已连接」。连接是惰性的（§21.7），一台
注册好但还没被用过的服务器并没有连接，可它的工具是该给模型看的——按「已连接」
判定会形成「模型看不见 → 调不到 → 永远连不上」的死结。所以记的是**失败**：连不上
就标不可用（之后不再展开），连上了就清掉标记（重新展开）。

**幂等性提醒**：§21.4 已经说过，2026-07-28 之后 SSE 断流要求重新发起请求。因此**超时后重试一个
MCP 工具调用是有副作用风险的**——第一次可能已经执行了（比如删了一条记录）。本轮**不做
幂等键**（MCP 协议没提供），列为已知限制（§21.14 第 6 条）。对于有副作用的工具，运维应在
服务器侧自己做幂等。

### 21.10 安全边界

这一节是本章最重要的部分。MCP 把「外部代码」接进了本服务，必须把每一条边界写死。

#### ① stdio 是「配置决定执行什么代码」——本设计唯一的例外

§17.2 立的是「配置不能决定执行逻辑」，因为工具参数直接来自模型、会受提示词注入影响。MCP
stdio 打破了这条例外：配置里写着 `command` 与 `args`，**配置确实决定了执行什么**。

这不是 bug，是 MCP stdio 的本质。处理方式不是「假装它不存在」，而是**改变它的信任级别**：

- **MCP 服务器注册不是普通配置操作，是部署级操作**。只能由管理员在后台完成，需要鉴权、
  需要审计留痕。普通用户能做的只有「在已注册的服务器里勾选工具」。
- **命令路径必须在白名单前缀内**（`settings.mcp_allowed_command_prefixes`，默认空 = 禁止
  stdio 注册）。这挡住「注册一个 `/bin/sh -c '…'`」这类构造。
- `env` 与 `headers` 的值加密存储，响应里永不回显明文。
- **不把 MCP 服务器的配置下放给普通租户**。

一句话对比：内置工具的配置是「名字」，MCP stdio 的配置是「程序」。前者是数据，后者是代码——
所以后者的操作权限必须比前者高一个级别。

#### ② Streamable HTTP 是 SSRF

URL 来自配置，指向 `169.254.169.254` 或内网服务就能打穿内网。缓解：

- 出网**地址白名单**：非白名单地址一律拒绝（默认白名单为空 = 允许全部，但生产部署必须配）；
- 默认**拒绝私网与回环网段**（`10/8`、`172.16/12`、`192.168/16`、`127/8`、`169.254/16`、
  `::1`、`fc00::/7`），除非显式放行；
- **重定向后要重新校验**：跟随重定向到私网的地址同样拒绝；
- 单次请求**超时上限**（`timeout_sec`，默认 30 秒），避免慢速服务挂住事件循环。

#### ③ 工具注解不可信

MCP 规范明确写了：客户端 **MUST** 把工具注解（`readOnlyHint` / `destructiveHint` /
`idempotentHint` / `openWorldHint`）当作**不可信数据**，除非来自可信服务器。

所以：**注解只用于前端展示与提示，永远不用于权限判定**。授权的唯一依据是 `agent.tools`
白名单（§21.8）。一条声称自己 `readOnlyHint: true` 的工具，照样要进白名单才能被调用。

#### ④ 工具结果是不可信输入

工具结果会**直接进 prompt**。它来自第三方服务器，可能包含提示词注入内容，也可能是敏感内容。

- **敏感词前置过滤**（§20）必须同样覆盖工具结果。§20 的过滤点在「用户输入进 LLM 之前」，
  MCP 工具结果是「服务器返回的内容进 LLM 之前」——**位置一样，性质也一样**，不能只过滤用户
  输入。命中时同样按「拒绝」处理（422 语义的等价物：不把该结果回灌给模型）。
- 工具结果的文本要**限长**（复用 `tool_result_max_chars`，默认 4000 字符），否则一次
  超长返回就能挤爆下一轮 prompt。

#### ⑤ 不实现客户端特性

MCP 的 roots / sampling / elicitation 允许**服务端反过来驱动客户端**：让我们的 LLM 去替它
生成内容、让我们的文件系统暴露目录、让它发起交互。这是一条完整的放大链（服务端 → 我们的
模型 → 我们的文件系统/外部 API），在本项目里没有任何一层是需要的。

**本轮一律不实现客户端特性**：MCP 服务器请求这些能力时，一律回 `method not found`。这既是
范围收敛，也是安全收敛。

### 21.11 API 设计

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/tools` | 工具目录（内置 + MCP）。条目加 `source` 字段（`builtin` / `mcp`）与 `server` |
| GET | `/api/mcp/servers` | MCP 服务器列表 + 在线状态 + 工具数 |
| POST | `/api/mcp/servers` | 注册服务器（**管理员**，§21.10 第 1 条） |
| PUT | `/api/mcp/servers/:code` | 更新，`version+1`，失效连接与快照 |
| DELETE | `/api/mcp/servers/:code` | 删除（连带删 `mcp_tool` 快照） |
| POST | `/api/mcp/servers/:code/refresh` | 重连并刷新工具快照 |
| GET | `/api/mcp/servers/:code/tools` | 该服务器已发现的工具 |

错误码沿用 §8 的约定：

| HTTP | 场景 |
| --- | --- |
| 400 | 服务器配置非法（传输与字段不匹配、命令不在白名单前缀、URL 被 SSRF 策略拒绝） |
| 401 | 非管理员尝试注册/更新 MCP 服务器（共享密钥判的是「你是谁」，与 §22.11 同一口径） |
| 404 | 服务器不存在 |
| 409 | 服务器 code 重复 |
| 502 | 服务器可达但协议握手/初始化失败（错误详情写进 `error_msg`，不回显凭据） |

**MCP 的七条接口全部是管理员专属**（含两条只读的）。只读的两条也要密钥——它们虽然
不写，但 MCP 服务器列表本身就透露了部署的内部结构（有哪些外部工具服务、连的什么
地址），没有必要为「看看有哪些服务器」单独开一套鉴权。

**凭据字段只写不读**：`env` / `headers` 的值加密存储，任何响应里都只回显**键名**
（`env_keys` / `header_keys`）。这与 `agent.api_key_cipher` 是同一套密钥边界。

### 21.12 事件与前端

延续 §17.7 的口径：工具事件都是**非终止事件**。

| 事件 | 载荷 | 说明 |
| --- | --- | --- |
| `tool_call` | `{"toolCalls": [...], "source": "builtin" \| "mcp"}` | 源信息让前端能区分「内置工具」与「外部服务器工具」 |
| `tool_result` | `{"tool", "ok", "summary", "source", "server", "durationMs"}` | `server` 是服务器 code，`durationMs` 是外部调用的耗时——外部工具比内置工具慢得多，前端要给不同的超时观感 |
| `mcp_unavailable` | `{"server": "<code>", "reason": "..."}` | 服务器掉线时发一次（非终止）。前端据此提示「该 Agent 的一部分能力不可用」 |

前端：后台的 Agent 编辑器里，工具勾选框从 `GET /api/tools` 取（§17.7 已经这么做了），
MCP 工具在勾选框里**分组显示**（按服务器分组），避免一个长列表里 `mcp__weather__get_weather`
和 `mcp__weather__get_forecast` 混在一起看不出来自哪台服务器。

### 21.13 与 V3.2 的差异清单

「闭环本身不用改」这句话要落到文件上。实现时**只有这几处**会动：

| 位置 | 改动 |
| --- | --- |
| `app/core/tools.py` | 拆成「内置注册表」与「MCP 目录」两个来源。`normalize_tool_names` / `known_tool_names` / `build_openai_tools` / `execute_tool` 四处按 `mcp__` 前缀分流 |
| `app/core/mcp/`（新） | `manager.py`（连接池与生命周期）、`transport.py`（stdio / HTTP）、`mapping.py`（结果映射）、`catalog.py`（发现与快照）、`naming.py`（命名映射） |
| `app/core/agent_runner.py` | **闭环不变**。只有 `_allowed_tools()` 的白名单要能装下 `mcp__` 限定名；`tool_call` / `tool_result` 事件多带 `source` / `server` / `durationMs` |
| `app/core/sensitive.py` | 工具结果回灌前过一遍（§21.10 第 4 条） |
| `app/models/`（新） | `mcp_server.py`、`mcp_tool.py` |
| `app/repo/`（新） | `mcp_repo.py`（`mcp_server_repo` + `mcp_tool_repo`） |
| `app/api/routes/tool.py` | 目录接口加 `source` / `server` 字段 |
| `app/api/routes/mcp.py`（新） | 服务器管理接口 |
| `app/schemas/`（新） | MCP 服务器的请求/响应模型（含凭据字段的写入-only 处理） |
| `requirements.txt` | 官方 MCP Python SDK（`mcp`）+ 它用的 httpx 分支（`httpx2`） |
| `alembic/versions/` | 新增迁移：`mcp_server`、`mcp_tool` 两张表 |

### 21.13.1 实现时与原文的偏差

有几处实现下来发现原案不够细，记在这里：

1. **`build_openai_tools` 必须改成 async**。MCP 工具的 schema 要从 `mcp_tool`
   快照里查，那是一次数据库查询。它是四个对外函数里唯一一个会变成异步的。
2. **可用性判定是「没被证明不可用」，不是「已连接」**。连接是惰性的（§21.7），
   一台注册好但还没被用过的服务器并没有连接，可它的工具是该给模型看的——按
   「已连接」判定会形成「模型看不见 → 调不到 → 永远连不上」的死结。所以记的是
   **失败**：连不上就标不可用，连上了就清掉。
3. **保存期与连接期的 SSRF 检查要分档**。保存期做 DNS 解析并把「解析失败」当
   拒绝，会把内网 MCP 服务器（最典型的用例）挡在门外——内外网分裂 DNS、服务
   还没上线、DNS 抖动，都会「此刻查不到」。所以保存期用 `require_dns=False`
   （解析失败放行并记日志），连接期用 `require_dns=True`（解析失败拒绝）。
   **IP 字面量**（最常见的 SSRF 载荷）不需要 DNS 就能判定，保存期照样拒。
4. **从限定名反推 server code 是脆弱的**。`mcp__<code>__<tool>` 里按第一个
   `__` 切能切对，靠的是「code 的正则不允许 `_`」这个约定。哪天约定破了，反推出
   的就是另一台服务器，而且**不会报错**。所以关联走 `mcp_tool.server_id`。
5. **命令前缀匹配是目录级的，且要跨平台**。前缀 `/opt/tools` 命中
   `/opt/tools/run`，不命中 `/opt/tools-evil/run`；Windows 上路径要先统一成
   正斜杠再比，否则 `C:\...\python.exe` 永远匹配不上 `/` 分隔的前缀。
6. **子进程的 stderr 要接一根真管道**。SDK 把 `errlog` 直接当 `subprocess` 的
   `stderr=` 用，要的是**文件描述符**而不是「一个能写字符串的对象」。开一根
   OS 管道（写端交给子进程、读端由后台线程收进 logger）才能既收编日志、
   又不污染服务的 stderr。
7. **`known_mcp=None` 在「写严读宽」里有两种含义**：`normalize_tool_names`
   （写）把它当「没有可用的 MCP 工具」→ 全部拒绝；`known_tool_names`（读）把它
   当「不筛选」→ 全部保留。这是刻意的不对称，不是笔误。

### 21.14 已知限制

1. **握手时代服务器的会话亲和性**。`Mcp-Session-Id` 存 Redis 后可以跨实例续上**对话**，但
   **连接本身**是进程内的：stdio 子进程无法跨实例（物理限制）。Streamable HTTP 的握手时代
   服务器若在实例 A 建连、请求落到实例 B，B 会用 Redis 里的 session id 重连——通常可行，但
   第一次请求会有额外延迟。
2. **工具清单是快照，不是实时的**。服务端加减工具不会立刻反映；要等下次重连或手工刷新。
   （本轮不实现 `notifications/tools/list_changed` 与 `subscriptions/listen`。）
3. **不接 MCP 的 resources 与 prompts**。只有 tools 能用。
4. **不实现 OAuth 授权流**。远程服务器只能靠管理员配静态 header（`Authorization` 等）认证。
   带 OAuth 的 MCP 服务器接不进来。
5. **工具在节点内串行执行**（沿用 §17.10 的既有限制）：模型一次要了三个 MCP 工具，会一个一个
   跑完再回灌。跨节点的 DAG 并行不受影响。
6. **超时后重试有副作用风险**。MCP 没提供幂等键；一次超时后重试，第一次可能已经执行了。
   有副作用的工具要由服务器侧自己做幂等。
7. **没有工具调用的审计表**（沿用 §17.10）。MCP 工具调用的参数与结果只在事件流与
   `run_step.output_json` 里，没有 `run_approval` 那样的账本表。
8. **工具名长度 64 是硬上限**。长名字的 MCP 工具会被拒绝导入——这是 OpenAI 的约束，不是
   MCP 的。
9. **stdio 子进程的资源回收依赖空闲超时**。进程崩溃或网络卡死时的回收不够干净，需要额外的
   僵尸进程巡检。
10. **没有工具级权限分级**（沿用 §17.10）。MCP 工具的权限与内置工具一样，只靠白名单区分。
11. **空闲连接回收还没挂上定时任务**。管理器提供了 `close_idle()`，但当前**没有周期任务
    调它**——要接进应用的定时任务或进程退出钩子，stdio 子进程才真的会被定期回收。
    在那之前，连接会一直留到进程退出。
12. **可用性状态是进程内的**。`mcp_manager` 是进程内单例，多实例部署下每个实例各有一份
    「不可用」标记：实例 A 连不上标了不可用，实例 B 那边仍认为可用（于是照常展开工具，
    调用时才失败）。不影响正确性（失败会回灌给模型），但前端的「在线状态」在多实例下
    不准。
13. **Streamable HTTP 传输没有端到端验证**。传输层与出网策略有单测，但本轮没有真的连过
    一台 Streamable HTTP 的 MCP 服务器——测试环境里没有可用的远程服务器，也没有起一个本地
    HTTP 版 MCP 服务器。stdio 那条路是完整的 e2e。

### 21.15 实施要点与验收标准

实现时下面每一条都要能验（**Phase 2 已落地**，勾选项后标了钉它的测试）：

- [x] `GET /api/tools` 同时列出内置与 MCP 工具，条目带 `source`；MCP 工具按服务器分组。
      → `tests/test_mcp_api.py::test_tools_endpoint_lists_builtin_and_mcp`
- [x] 配一个未注册的 `mcp__xxx__yyy` → 保存 Agent 返回 400，且错误信息说清是「服务器未发现
      工具」还是「工具不存在」。→ `tests/test_mcp_api.py::test_agent_cannot_bind_unknown_mcp_tool`
- [x] 配置一个真实 MCP 服务器（stdio 与 Streamable HTTP 各一个）→ 刷新目录 → 快照表里出现
      对应工具行，`qualified_name` 与 OpenAI 函数名约束一致。
      → stdio：`tests/test_mcp_e2e.py::test_refresh_discovers_tools_into_snapshot`；
      Streamable HTTP **本轮未做 e2e**（见 §21.14 第 13 条）。
- [x] 一次真实调用：模型要 `mcp__weather__get_weather` → 服务端执行 → 结果回灌 → 模型继续；
      **断言第二轮请求里确实带着 `role=tool` 的结果**（沿用 §17.9 的 e2e 断言方式）。
      → `tests/test_mcp_e2e.py::test_mcp_tool_is_executed_and_fed_back_to_model`
- [x] 停掉 MCP 服务器 → 该服务器的工具**不再展开给模型**（断言发给 LLM 的 `tools` 参数里
      没有它），且已有 Agent 仍能正常跑完（退化成「没有这个能力」）。
      → `tests/test_mcp_e2e.py::test_offline_server_tools_are_not_expanded`、
      `test_agent_still_runs_when_its_mcp_server_is_offline`
- [x] MCP 服务器返回 `isError: true` → 节点**不失败**，错误文本回灌给模型，`tool_result`
      事件的 `ok` 为 `false`。
      → `tests/test_mcp_e2e.py::test_mcp_is_error_does_not_fail_the_node`
- [x] MCP 工具返回图片 → 回灌文本里是占位说明，不是空字符串；`tool_result.summary` 有内容。
      → `tests/test_mcp_e2e.py::test_mcp_image_result_becomes_placeholder`
- [x] 工具结果命中敏感词 → **不回灌**，run 以敏感内容错误收尾（与 §20 的口径一致）。
      → `tests/test_mcp_e2e.py::test_sensitive_tool_result_is_not_fed_back`
- [ ] 子进程把普通日志打到 stdout → 协议不乱（连接仍可用）。
      → **本轮未做**：没有为此加一个「脏 stdout」的 fixture 服务器。协议层不会因此乱
      （SDK 自己只读 stdout 的协议帧，服务端随便 `print` 会污染帧流），但这条没被测到。
- [x] 重启服务后，MCP 工具目录快照仍在，且离线服务器的 Agent 不会调它。
      → 快照在库里，进程重启不受影响；离线部分由
      `test_offline_server_tools_are_not_expanded` 覆盖。
- [x] 全量回归不破：现有 166 个测试（§17.9）全绿。
      → Phase 2 落地时全量 **428 passed / 19 skipped**（新增 67 个 MCP 相关用例）。

---

## 22. V4：Skill 指令包（可复用的领域知识）

### 22.1 目标与边界

Agent 的 `system_prompt` 是一个字符串。它能装下一段话，装不下一套「怎么做某类事」的可复用
流程——因为流程往往要带参考资料、模板、示例、脚本，一段纯文本装不下，而且没法被多个 Agent
复用、没法单独版本管理。

本轮引入 **Skill（Agent Skills）**：一个**技能包**，里面有一个 `SKILL.md`（元数据 + 指令正文）
和若干可选的资源文件。技能包**存放在数据库里**（§22.4），Agent 绑定它，指令正文进系统提示词，
资源文件按需读，`scripts/` 里的脚本交给**独立的 studio 容器**执行。

本轮要做的：

- **Skill 包格式**：采用 [Agent Skills 开放格式](https://agentskills.io/)（`SKILL.md` + YAML
  frontmatter + `references/` `assets/` 等目录），保证技能包能通过导入 / 导出与别处互通；
- **绑定关系**：Agent 绑定技能，绑定表落 DB；
- **正文注入**：绑定技能的 `SKILL.md` 正文作为独立的一条 system 消息追加在 Agent 自己的
  system prompt 之后；
- **资源读取**：内置工具 `skill_resource`，让模型按需读 `references/` 里的文件；
- **脚本执行**：内置工具 `skill_exec`，把 `scripts/` 里的脚本交给**独立的 studio 容器**执行
  （gRPC 调用，见 §22.8）——**不在 web_agent 进程里起子进程**；
- **配置与治理**：技能内容与资源全部存数据库（§22.4、§22.5），后台可增删改查，**仅管理员
  可写**，正文变更全部审计留痕；支持从目录或 zip **批量导入**（§22.11）；
- **绑定关系**：Agent 绑定技能，绑定表落 DB。

本轮**不做**：

| 不做的东西 | 为什么 |
| --- | --- |
| **在 web_agent 进程内执行脚本**（`subprocess`） | 执行任意代码需要沙箱。沙箱是**studio 容器**，不是 web_agent 进程——理由见 §22.8.1 |
| **模型自己挑技能**（动态发现） | 本项目是**配置驱动**的编排服务：运维明确知道每个节点需要哪些技能。模型「挑技能」这一步没有决策价值，只有一类新的失败模式（挑错、挑漏） |
| **多租户技能隔离** | 技能表目前没有租户维度。租户隔离要先把「数据模型 ↔ 租户模型」对齐，而本项目目前没有租户概念（§1.1 的目标是单部署多 Agent） |

### 22.2 Skill 是什么

一个技能包**导出后**的目录形态（§22.4：这是导出格式，不是存储格式）：

```text
financial-report/                    # 导出时的目录名 = skill.code
├── SKILL.md          # 必需：frontmatter + 指令正文
├── scripts/          # 可选：可执行脚本（交给 studio 容器跑，§22.8）
│   └── extract.py
├── references/       # 可选：参考资料（按需读）
│   ├── METRICS.md
│   └── TAX_RULES.md
└── assets/           # 可选：模板、表格、示例
    └── report-template.xlsx
```

**它怎么落在数据库里**：

| 目录里的东西 | 数据库里的位置 |
| --- | --- |
| 目录名 | `skill.code` |
| `SKILL.md` 的 frontmatter | `skill` 表的若干列（§22.5） |
| `SKILL.md` 的正文 | `skill.body` |
| `scripts/` `references/` `assets/` 里的文件 | `skill_resource` 表，一行一个文件 |

`SKILL.md` = YAML frontmatter + Markdown 正文：

```markdown
---
name: financial-report
description: 生成季度财务分析报告。需要产出财报分析、指标解读或经营回顾时使用。
license: Proprietary
metadata:
  author: finance-team
  version: "1.0"
---

# 季度财务分析报告

## 步骤

1. 先用 `get_quarter_metrics` 拉取本季度与去年同期指标；
2. 按 `references/METRICS.md` 里的口径计算同比；
3. …（完整步骤见正文）

模板见 [报告模板](assets/report-template.xlsx)。
```

frontmatter 字段（规范原样，本项目**不做扩展**——扩展字段放 `metadata` 里）：

| 字段 | 必填 | 约束 | 本项目的用法 |
| --- | --- | --- | --- |
| `name` | 是 | 1–64 字符，小写字母/数字/连字符，不以连字符开头结尾，不连续连字符 | 同时是 DB 里的 `skill.code`（两者必须一致） |
| `description` | 是 | 1–1024 字符 | 后台列表展示 + 「这技能干什么用」的说明 |
| `license` | 否 | 许可证名或文件名 | 原样存 |
| `compatibility` | 否 | ≤ 500 字符 | 原样存（如「需要内网访问」） |
| `metadata` | 否 | 字符串键值对 | 原样存（作者、版本等） |
| `allowed-tools` | 否 | 空格分隔，**实验性** | **仅作建议**（§22.9），不用于授权 |

正文部分**没有格式约束**，写什么都行。本项目额外加两条自己的约束（§22.6）：正文长度有上限，
引用文件路径只能相对技能根目录。

> **为什么沿用开放格式而不是自定义一套**：`SKILL.md` 是**可移植资产**——别人写的技能包能直接
> 导入进来用，本项目也能把技能导出成目录给别人。格式一旦自定义，生态就用不上了。
> **注意存的是 DB、不是目录**：可移植性由「导入 / 导出」这对接口保证（§22.4），不需要靠
> 部署目录来保证。参考实现见 [agentskills.io](https://agentskills.io/) 与其校验工具
> `skills-ref`。

### 22.3 渐进披露在本项目里的三种形态

Agent Skills 的标准做法是**三级渐进披露**：

1. **发现**：启动时只加载每个技能的 `name` + `description`（约 100 token），够模型判断「这事跟
   我有没有关系」；
2. **激活**：任务匹配上，才把完整 `SKILL.md` 正文读进上下文；
3. **执行**：按需读 `scripts/` `references/` `assets/` 里的文件。

**第 1、2 级在本项目里都不成立**，得说清楚为什么：

- 本项目的 Agent 是**配置驱动**的：`agent.skills` 里点名的技能才会出现在这个节点的提示词里。
  模型**没有「挑技能」这一步**——运维已经替它挑好了。于是「发现」阶段的产物（目录清单）根本
  不需要进上下文，绑定关系本身就是发现结果。
- 既然绑定即确定，「激活」也没有单独一轮的必要：被绑定的技能，正文直接注入。

于是落地形态是：

| 标准形态 | 本项目的落地 |
| --- | --- |
| ① 发现（name+description 全量） | **替换为绑定表**——不进上下文 |
| ② 激活（SKILL.md 正文） | **绑定即注入**：正文作为独立 system 消息追加在 Agent 的 system prompt 之后 |
| ③ 执行（按需读资源文件） | **保留**：模型用 `skill_resource` 工具按需读 |

第 ③ 级是**成本控制的主力**：`references/` 里的资料往往比 `SKILL.md` 正文长得多，全量注入
既贵又稀释注意力。规范的建议也是「SKILL.md 控制在 500 行以内，详细资料拆到 `references/`」。

省掉 ① ② 的代价是明确的：**技能绑多了，提示词会变长**。这由 §22.6 的预算处理。

### 22.4 存放：全部在数据库里

**技能内容（正文 + 资源文件）全部存在数据库里。没有「部署目录」这回事。**

理由只有一条，但它压过了开放格式的便利：

> 本项目第一条设计原则是「**配置即数据**」（§1.2）——Agent 与 Workflow 全部存 MySQL，运行期
> 热加载。Skill 放在部署目录里，是**同一份配置的两套存储**。两套存储就是两套真相：目录里的改了
> DB 不知道，DB 里改了目录不知道；部署时还得处理「这个目录怎么进镜像」这种跟业务无关的事。

Agent 的 `system_prompt` 在 DB 里、`tools` 在 DB 里、`workflow.definition` 在 DB 里——唯独技能
在磁盘上，这个不一致没有道理。

**开放格式怎么办**：`SKILL.md` 从「**存储格式**」降级为「**导出格式**」。

- DB 里的**规范形态**是「若干列（frontmatter 字段）+ 一段 body（Markdown 正文）」；
- **导出**时按 Agent Skills 规范重新拼出 `SKILL.md`（frontmatter + body），资源按原路径还原
  成文件——导出的目录**就是**别人能直接用的目录，可移植性一点没丢；
- **导入**时反过来解析：读 `SKILL.md` 的 frontmatter 填列，读 body 存正文，读资源文件入库。

导入与导出因此是**同一个格式转换的两个方向**，只有一个实现，不会出现「导出能出、导入进不去」
的不对称。

> **顺带得到的一个好处**：技能可以被**程序化**创建与修改，不必先在本地写好文件、走部署流程。
> 这是「配置即数据」真正的兑现——运营在后台就能改技能，改完立即生效。

### 22.5 数据模型

三张表 + Agent 上的一个 JSON 列。

#### `skill` — 技能定义

```sql
CREATE TABLE skill (
  id            BIGINT PRIMARY KEY AUTO_INCREMENT,
  code          VARCHAR(64)   NOT NULL UNIQUE,   -- 技能标识，须与 frontmatter 的 name 一致
  name          VARCHAR(64)   NOT NULL,          -- frontmatter 的 name
  description   VARCHAR(1024) DEFAULT '',        -- frontmatter 的 description
  license       VARCHAR(128)  DEFAULT '',        -- frontmatter 的 license
  compatibility VARCHAR(500)  DEFAULT '',        -- frontmatter 的 compatibility
  metadata      JSON          DEFAULT NULL,      -- frontmatter 的 metadata（字符串键值对）
  allowed_tools VARCHAR(512)  DEFAULT '',        -- frontmatter 原样存，**仅建议**（§22.9）
  body          MEDIUMTEXT    NOT NULL,         -- SKILL.md 的正文（frontmatter 之外）
  origin        VARCHAR(16)   NOT NULL DEFAULT 'manual',  -- manual | import
  content_hash  VARCHAR(64)   NOT NULL,         -- 正文 + 全部资源一起算的 sha256
  enabled       TINYINT(1)    DEFAULT 1,
  version       INT           DEFAULT 1,        -- 每次变更 +1（与 agent 表同口径）
  created_at    DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3),
  updated_at    DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

`body` 存的是 **frontmatter 之外的 Markdown 正文**——frontmatter 已经拆成列了，不必在 body
里再存一份 YAML。导出时按规范重新拼回去（§22.4）。

`name` 恒等于 `code`：规范要求 frontmatter 的 `name` 等于目录名，而本项目里「目录名」
就是 `code`。所以 `POST /api/skills` 传了 `name` 就必须与 `code` 一致，不一致直接 400
（§22.10 第 5 条）。这样导出的 `SKILL.md` 永远是合规的。

时间列用 `DATETIME(3)`：裸 `DATETIME`（fsp=0）在 MySQL 上把时间截到整秒，而且与 ORM 的
`DATETIME3` 对不上——`alembic` 开了 `compare_type=True`，此后每次 autogenerate 都会持续产出
一条多余的 ALTER 差异。

`content_hash` 的算法：

```text
sha256( body ‖ 逐个资源按 path 排序后的 ( path ‖ sha256(content) ) )
```

它同时管两件事：**变更检测**（后台能看出「技能有没有被改过」）与**幂等键**（`skill_exec` 的
`request_id` 里带上它，同一版脚本的结果可复用）。

#### `skill_resource` — 技能的资源文件

```sql
CREATE TABLE skill_resource (
  id         BIGINT PRIMARY KEY AUTO_INCREMENT,
  skill_id   BIGINT       NOT NULL,
  path       VARCHAR(255) NOT NULL,        -- 相对技能根，如 references/METRICS.md
  mime_type  VARCHAR(64)  DEFAULT 'application/octet-stream',
  is_text    TINYINT(1)   DEFAULT 1,       -- 文本资源才进 SKILL.md 的引用回滚
  content    LONGBLOB     NOT NULL,        -- 文件内容（文本与二进制同列）
  size_bytes INT          NOT NULL,
  updated_at DATETIME(3)  DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_skill_path (skill_id, path),
  KEY idx_skill (skill_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

`references/` `assets/` `scripts/` 里的文件**全部**是这张表的一行。`is_text` 区分「能直接塞进
提示词 / 能回灌给模型」与「只能下载」——`skill_resource` 工具对二进制资源只报元信息，不把
字节塞进上下文。

**一张表存文本与二进制**：MySQL 的 `LONGBLOB` 对两种都够用，且避免了「文本在一张表、二进制
在对象存储」带来的跨源一致性问题。代价是单行不能太大——**单资源上限 5 MB、单技能资源总量
上限 20 MB**，超了拒。

#### `skill_revision` — 变更审计

```sql
CREATE TABLE skill_revision (
  id         BIGINT PRIMARY KEY AUTO_INCREMENT,
  skill_id   BIGINT      NOT NULL,
  version    INT         NOT NULL,
  body       MEDIUMTEXT  NOT NULL,          -- 正文快照
  resources  JSON        DEFAULT NULL,      -- 资源清单：[{path, sha256, is_text, size_bytes, text?}]
  author     VARCHAR(64) DEFAULT '',        -- 谁改的
  note       VARCHAR(255) DEFAULT '',       -- 改了什么（一句话）
  created_at DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_skill_version (skill_id, version),
  KEY idx_skill (skill_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

**为什么需要它**：技能进了 DB 就没有 git 了，改错了没法回滚。这张表是 DB 版的版本历史。

`(skill_id, version)` 是唯一的，所以**每一次写入都要让 `skill.version` 往前走**——改正文、
写资源、删资源、回滚都算。于是「版本号」唯一对应一份内容，回滚到 `v3` 才有确定含义。
回滚本身是**新版本**（内容回到旧样子，版本继续递增），版本号单调递增、不会自我覆盖。

`resources` 快照里**文本资源带内容、二进制资源只带摘要**——所以**文本资源可以回滚，二进制
资源不能**。这是明确的取舍，写进 §22.12 的已知限制。

#### `agent.skills` 列

```sql
ALTER TABLE agent ADD COLUMN skills JSON DEFAULT NULL;
```

与 `tools` **同形**：存技能 code 的数组，不存正文。理由同 §17.2——**配置只放名字，内容从
`skill` / `skill_resource` 取**。这样「模型能用的技能」与「服务端能提供的技能」仍然是同一张
表，对不上就在保存期拒。

**读的时候宽容，写的时候严格**（与 §17.2、§21.8 一致）：

- 写（`normalize_skill_codes`）：未注册的 code **保存期就拒**（400）；被禁用的技能
  （`enabled=0`）也拒——绑一个用不了的技能是配置错误，不该留到运行期才发现；
- 读（`known_skill_codes`）：历史脏数据里的未知 code **丢掉**，不能让 Agent 列表接口 500。

**绑定不级联删除**：删技能**不**自动解绑 Agent。但删的时候会被拦下——还被 Agent 绑着的
技能，`DELETE /api/skills/:code` 直接 **409**，并在报错里列出是哪几个 Agent 绑着它。
理由：删掉技能会让那些 Agent **静默失去**那段指令，而模型行为的变化不会在界面上显式提示，
那是最难排查的一类问题。想停用就 `PUT enabled=false`，或者先解绑。

读路径上仍然是宽容的：技能被删掉之后，Agent 上残留的绑定在读的时候被丢掉（`known_skill_codes`
按「库里已存在的 code」过滤），不让一个 NULL 或失效引用把整个列表接口带崩。

### 22.6 Skill 怎么进入提示词

**拼装顺序**：

```text
messages[0] = { role: "system", content: <Agent 自己的 system_prompt> }
messages[1] = { role: "system", content: <技能 1 的 SKILL.md 正文> }
messages[2] = { role: "system", content: <技能 2 的 SKILL.md 正文> }
...
```

两个决定：

**① 技能作为独立的一条 system 消息，不拼进 Agent 的 system_prompt 字段。**

拼进去的话，后台页面上「提示词」那一栏显示的内容就和实际发给模型的不一致——运营改提示词时
看到的不是模型真正收到的东西。独立成条，差异是**可见的一层**：后台要显示「生效提示词预览」
（§22.11），预览内容与实际发送逐字一致。

**② 技能在前，Agent 的 system_prompt 在后——不对，反过来。**

顺序是 **Agent 的 system_prompt 在前，技能正文在后**。理由：system_prompt 说的是「你是谁、
你在这条流程里干什么」，技能说的是「这类事具体怎么做」。先定身份与目标，再给方法，与人读的
顺序一致；反过来会让技能里的方法论盖过角色设定。

> 技能正文之间按 `agent.skills` 数组的**声明顺序**注入，运营可以靠顺序表达优先级。

**预算与截断**：

| 上限 | 默认 | 挡什么 |
| --- | --- | --- |
| 单个技能正文字符数 | 20000 | 一个巨型技能撑爆上下文 |
| 技能正文**总**字符数 | 4000（`settings.skill_prompt_budget`） | 绑了太多技能 |
| 参与注入的技能数 | 5（`settings.skill_max_count`） | 同上 |

**截断策略是「宁少勿半」**：超过总预算时，**按声明顺序保留靠前的技能，后面的整个不注入**，
并记一条日志与一条 `skill_skipped` 事件说明「因为预算没注入哪个技能」。

**不做半个技能的截断**——半个 `SKILL.md` 里的指令可能自相矛盾（前一半说「用 A 方法」，被砍掉
的后一半说「用 B 方法」），比不给更糟。

`skill_prompt_budget` 与 §4.4 的 `context_max_tokens` 是**两层**预算，不是一层：后者管的是
**会话历史**（用户与助手消息），前者管的是**技能正文**（系统侧配置）。两者都计入最终发给
模型的 prompt，LLM 边界那次裁剪（§14.3 / `_split_turns`）是最后一道。

**技能正文不过敏感词过滤**。§20 的前置过滤拦的是**用户输入**——那是不可信输入。技能正文是
**管理员写进数据库的部署内容**。在它上面套一层敏感词过滤，只会制造「改个技能正文要绕过
过滤器」的荒唐场面。技能正文的准入靠的是**写权限与审计**（§22.10）。

### 22.7 Skill 资源读取工具 `skill_resource`

`references/` 里的文件不进上下文，模型需要时用内置工具读。

```python
# app/core/tools.py（新增一条内置工具）
async def _skill_resource(args: dict) -> str:
    """读取技能的一个资源文件。"""
    code = args.get("skill")
    rel = args.get("path")
    # 1. 校验 code 在该 Agent 绑定的技能里（白名单）
    # 2. 校验 path 的**格式**（相对路径、无 ..、字符集白名单）
    # 3. 按 (skill_id, path) 精确查 skill_resource
    # 4. 文本按 tool_result_max_chars 截断；二进制只回元信息
```

参数：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `skill` | string | 技能 code |
| `path` | string | 相对技能根的路径，如 `references/METRICS.md` |

#### 路径穿越问题消失了

这是「技能从文件系统挪进数据库」最实在的一个收益，必须写清楚。

原来这个工具的**全部**风险都在路径解析上——要防 `../../etc/passwd`、要防符号链接指向目录
外、要防绝对路径。现在**没有任何目录可解析**了：

```python
row = await db.execute(
    select(SkillResource).where(
        SkillResource.skill_id == skill_id,
        SkillResource.path == rel,      # 精确匹配，不是路径拼接
    )
)
```

`path` 是**一个查表的键**，不是一条路径。`../../etc/passwd` 查不到任何一行，就什么也读不到。
**没有 `resolve()`，没有 `is_relative_to`，没有符号链接**——一整类漏洞从根上没了。

但**输入格式校验还是要做**（防御性，且挡住无意义的查询与脏数据）：

| 校验 | 规则 |
| --- | --- |
| 长度 | ≤ 255 |
| 绝对路径 | 以 `/` 开头 → 拒 |
| 上跳 | 任何路径段是 `..` → 拒 |
| 空段 | 出现 `//` → 拒 |
| 反斜杠 | 含 `\` → 拒（Windows 风格分隔符） |
| 字符集 | 只允许 `[A-Za-z0-9._/-]` |
| 深度 | 路径段 ≤ 8 |

**写入期做同样的校验**（`skill_resource` 表的写入路径）：不合规的 path 直接 400，不入库。
读宽松、写严格——与 §17.2 同一口径。

#### 大小与类型

- **文本资源**：最多读 `tool_result_max_chars`（默认 4000 字符）——与工具结果回灌同一条上限，
  避免一次读取把上下文撑爆；
- **二进制资源**（`is_text=0`）：**不把字节塞进上下文**，只回「文件名 + 大小 + mimeType」，
  模型知道有这么个文件在就行。

#### `scripts/` 里的文件

也可以读（当参考资料读）。但**读 ≠ 执行**：执行是另一条路——`skill_exec` 工具把脚本正文交给
studio 容器（§22.8），web_agent 自己**从不**执行它。

### 22.8 脚本执行：gRPC 调用 studio 容器

> 本节是**调用侧**视角。studio 服务自身的内部架构、进程隔离、沙箱加固、资源治理、运行时
> 矩阵、部署扩缩、威胁模型与测试策略，见独立文档
> **[`services/studio/DESIGN.md`](../services/studio/DESIGN.md)**。

#### 22.8.1 为什么不在 web_agent 进程里跑脚本

前面的边界里写着「不执行 `scripts/` 里的脚本」，理由是「执行任意代码需要沙箱」。现在这个
沙箱有了：**沙箱就是容器**。

不在 web_agent 进程里 `subprocess` 跑脚本，有三条硬理由：

1. **逃逸面**：脚本的参数直接来自**模型**，而模型会被提示词注入影响。脚本跑在 web_agent
   进程里，它能看到 web_agent 的一切——环境变量（含 `SECRET_KEY`、数据库口令）、文件系统、
   进程空间；这些都能被 `stdout` 读出来，再由工具结果回灌进 prompt 带出去。
2. **资源失控**：脚本要 CPU、内存、磁盘、时长。在 web_agent 进程里没法真正限制——
   `resource.setrlimit` 是**进程级**的，一个跑飞的脚本能把整个服务拖垮；§13 的多实例设计里，
   一个实例的 CPU 打满就等于该分片不可用。
3. **依赖地狱**：脚本要 numpy、pandas 或某个 CLI。装进 web_agent 的镜像 = 污染服务运行时；
   每个技能各带各的依赖 = 镜像膨胀到不可维护。

容器把这三条一次性解决掉：**studio 容器里没有 web_agent 的任何凭据。**

#### 22.8.2 studio 是什么

| 项 | 口径 |
| --- | --- |
| 形态 | 独立容器、独立镜像，与 web_agent 同级部署 |
| 职责 | 接收「一段脚本 + 一组参数」，在受限环境里执行，回传 stdout / stderr / exit code |
| 状态 | **无状态**：不落盘、不缓存、不认识用户、不认识技能 |
| 与 web_agent 的关系 | 只被 gRPC 单向调用；它不依赖 web_agent 的任何接口 |

#### 22.8.3 契约

```proto
syntax = "proto3";
package webagent.studio.v1;

// studio：技能脚本的受限执行容器。
// 调用侧视角见主设计文档 §22.8。
service Studio {
  // 执行一个脚本，同步返回结果
  rpc Exec(ExecRequest) returns (ExecResponse);
  // 探活：容器是否就绪、装了哪些运行时
  rpc Health(HealthRequest) returns (HealthResponse);
}

// 执行档位。请求里带上它，服务端只能升、不能降；响应里回显**实际**档位。
enum ExecProfile {
  EXEC_PROFILE_UNSPECIFIED = 0;  // 老客户端：沿用服务端路由
  EXEC_PROFILE_FAST = 1;         // 容器内独立进程组（默认）
  EXEC_PROFILE_ISOLATED = 2;     // 每次执行起一次性沙箱
}

// 执行结果的状态。
// **gRPC 层面成功 ≠ 脚本成功**：脚本自己 exit 1 是 COMPLETED + exit_code=1，
// 业务失败要靠 status/exit_code 表达，不能用 gRPC status 报错——调用方要把
// 失败信息回灌给模型（主设计 §17.5）。
enum ExecStatus {
  EXEC_STATUS_UNSPECIFIED = 0;
  EXEC_STATUS_COMPLETED = 1;  // 脚本跑完了，exit_code 有意义
  EXEC_STATUS_FAILED = 2;      // 没能跑起来（运行时缺失、启动失败、非法脚本）
  EXEC_STATUS_TIMEOUT = 3;     // 超时被杀
  EXEC_STATUS_LIMITED = 4;     // 被资源限制杀掉（OOM / cgroup 限额）
  EXEC_STATUS_CANCELLED = 5;   // 调用方取消
}

message ExecRequest {
  string skill = 1;           // 技能 code（审计用）
  string script = 2;          // 相对技能根目录的脚本路径，如 scripts/extract.py
  string language = 3;        // python | bash | node
  string content = 4;         // 脚本正文（调用方读出来随请求带上）
  repeated string argv = 5;   // 传给脚本的参数
  string stdin = 6;           // 标准输入（文本）
  int64 timeout_ms = 7;       // 执行时限；服务端还会再按上限收一道
  string run_id = 8;          // 关联一次 run（审计）
  string node_id = 9;         // 关联一个节点
  string request_id = 10;     // 追踪键；同键在去重窗口内不重复执行
  ExecProfile profile = 11;   // 调用方请求的档位；服务端只能升、不能降
  string image = 12;          // 镜像目录名（不是完整 ref）；空 = default
}

message ExecResponse {
  ExecStatus status = 1;
  int32 exit_code = 2;        // 仅 COMPLETED 时有意义
  string stdout = 3;
  string stderr = 4;
  bool truncated = 5;          // 输出被截断
  int64 duration_ms = 6;
  string error = 7;            // 服务级错误说明（不是脚本的 stderr）
  string stdout_digest = 8;    // stdout 的 sha256，用于审计与去重
  ExecProfile profile = 9;     // **实际**使用的档位，必须回显
  int64 startup_ms = 10;       // 沙箱启动耗时（fast 为 0）
  string error_code = 11;      // 稳定枚举字符串；见 services/studio/DESIGN.md §5.1
}

message HealthRequest {}

message HealthResponse {
  bool ready = 1;
  string version = 2;
  repeated LanguageInfo language_info = 3;
  int32 max_concurrency = 4;
  string profile = 5;          // 当前隔离档：fast | isolated（兼容字段）
  repeated ProfileInfo profiles = 6;   // 按档位报能力
  repeated ImageInfo images = 7;       // isolated 可用的镜像目录
  int64 isolated_startup_budget_ms = 8;  // 沙箱启动预算（毫秒）
}

// 一个档位现在能不能用。`ready=false` 时 `detail` 说明原因。
message ProfileInfo {
  string name = 1;             // fast | isolated
  bool ready = 2;
  int32 max_concurrency = 3;
  int32 queue_depth = 4;
  string detail = 5;
}

// isolated 可用的镜像（目录名，不是完整 ref）。
message ImageInfo {
  string name = 1;             // 如 default / ml-py311
  repeated string languages = 2;   // 该镜像能跑的运行时
  string digest = 3;           // image@sha256:...
}

message LanguageInfo {
  string name = 1;             // python
  string version = 2;          // 3.11.9
  repeated string extensions = 3;   // .py
}
```

> **服务侧展开见 [`services/studio/DESIGN.md`](../services/studio/DESIGN.md)**：容器的内部架构、执行生命周期、
> 进程隔离、沙箱加固、资源治理、运行时矩阵、部署扩缩、威胁模型、测试策略。
> 两份文档贴的 proto 片段**必须逐字一致**——proto 文件在仓库里只有一份
> （`proto/webagent/studio/v1/studio.proto`），CI 会校验一致性。

字段上的四个决定：

- **脚本正文随请求走**（`content`），不共享卷：studio 保持无状态，web_agent 与 studio 之间
  没有卷依赖，两个容器可以独立扩缩、独立升级。代价是请求体变大——脚本通常几 KB 到几十 KB，
  可接受。
- **`script` 与 `content` 分开**：`script` 是**审计用的标识**（哪个技能、哪个文件），`content`
  才是真正被执行的东西。studio **不**校验 `script` 路径，也**不**解析技能——它没有技能的概念，
  技能在 web_agent 的数据库里（§22.4）。路径校验因此必须留在 web_agent 侧（复用 §22.7 的
  格式校验）。
- **`error` 与 `stderr` 分开**：脚本自己写到 stderr 的是「脚本的输出」，原样回灌给模型；
  `error` 是「studio 这次没跑起来」（超时、容器被杀、镜像缺运行时）。两者语义不同，模型
  看到它们的反应也不同。
- **`ExecStatus` 独立于 gRPC status**：脚本自己 `exit 1` 是 `COMPLETED` + `exit_code=1`，
  **不是** gRPC 错误。理由见 §17.5 的口径——业务失败要回灌给模型，服务不可用才是 gRPC
  错误。gRPC status 只用来表达 studio 自己的问题。

#### 22.8.4 新增的内置工具：`skill_exec`

```python
# app/core/tools.py（新增一条内置工具）
async def _skill_exec(args: dict) -> str:
    """在 studio 容器里执行技能脚本。"""
    code = args.get("skill")
    script = args.get("script")
    argv = args.get("args") or []
    # 1. 校验 code 在该 Agent 绑定的技能里（白名单）
    # 2. 校验 script 的路径格式（复用 §22.7 的格式校验）
    # 3. 从 skill_resource 读脚本正文（限长）
    # 4. 调 studio 的 Exec
    # 5. 拼成「退出码 + stdout (+ stderr)」返回给模型
```

返回给模型的文本形状——模型要能据此判断成功与否：

```text
exit_code: 0
--- stdout ---
{"rows": 128, "file": "q3.csv"}
--- stderr ---
(warning) 2 rows skipped
```

失败时：

```text
exit_code: 2
--- stdout ---

--- stderr ---
Traceback (most recent call last):
  ...
```

**脚本输出不落库**：它可能含敏感数据（§22.10）。只作为 `role=tool` 的内容回灌，并截断到
`tool_result_max_chars`。

**`skill` 参数的描述要按当前上下文动态生成**：模型判断「调哪个技能」的唯一依据就是提示词
与工具 schema，而技能正文（§22.6）注入的是**正文**——正文里不一定写着自己的 code。所以
`skill` 参数的描述里必须列出**实际可用的技能 code**（与执行期白名单同一份），否则真模型
只能猜。实测见 §22.14 的第 7 条。

#### 22.8.5 超时、取消与僵尸进程

- 超时由 `timeout_ms` 指定，**上限由 web_agent 侧强制**（`studio_exec_max_timeout_ms`，默认
  60 秒）。模型请求的时限只能在这个上限之内。
- studio 侧必须**自己也有一道时限**，并且超时后要杀掉**整个进程组**——只杀父进程的话，孙进程
  会继续跑并占着容器。
- web_agent 侧同时设 gRPC deadline（`timeout_ms + 2 秒`宽限），先到哪个算哪个。
- 用户点「停止」时，编排器的取消标志要能中断这次 gRPC 调用。但**协议上无法保证容器里的脚本
  被杀掉**（gRPC 的取消不传播到对端的进程组），所以 studio 侧的自设时限不是可选项，是**兜底**。

#### 22.8.6 studio 不可用时

`skill_exec` 返回 `ok=False` + 错误说明，按 §17.5 的口径**不失败节点**（回灌给模型让它自己
换条路）。

但更早一道防线在**启动期**：studio 不可达 → **启动失败**。理由与 §20.7（敏感词模型缺失即
启动失败）一致——studio 是部署的一个组成部分，它挂了说明部署不完整，让问题在部署时暴露，
而不是在用户的一次 run 里暴露。确实需要降级的场景（本地开发没有 studio）可以用
`STUDIO_EXEC_REQUIRED=false` 显式关掉；此时 `skill_exec` **不注册**，且配置期直接拒。

#### 22.8.7 部署拓扑

```mermaid
graph LR
    subgraph deploy["同一编排平台（同一台机器的多个容器也算）"]
        WA["web_agent 容器<br/>FastAPI + LangGraph<br/>持有 SECRET_KEY / DB 口令"]
        ST["studio 容器<br/>无凭据 / 只读根 fs<br/>无出网 / 不挂 Docker socket"]
        DP["studio-dispatcher<br/>只调度 / 不执行 content<br/>持 Docker socket"]
        SB["一次性沙箱容器<br/>零凭据 / 无网络 / 只读 rootfs"]
    end
    WA -->|"gRPC · settings.studio_exec_addr"| ST
    ST -->|"内部 HTTP · /v1/exec"| DP
    DP -->|"create / start / logs / remove"| SB
```

`isolated` 档的信任边界是**两段**的：studio 不持容器运行时凭据（它跑 fast 档和
模型指定的代码），dispatcher 持凭据但**从不执行 `content`**——它只按固定模板
create / start / logs / remove。沙箱本身零凭据、禁出网、只读 rootfs、非 root。

studio 容器应满足：

| 项 | 要求 | 为什么 |
| --- | --- | --- |
| 凭据 | **一个都没有** | 它执行的是模型指定的脚本 |
| 根文件系统 | 只读 | 脚本只往 `/tmp` 写 |
| 可写目录 | 只有 `/tmp`（tmpfs 或空卷），写完即弃 | 不留痕 |
| 出网 | **禁止**（或严格白名单） | 否则它可以从里面往外拉数据 |
| 资源 | CPU / 内存上限 + 时限 | 一个跑飞的脚本不能拖累宿主机 |
| 用户 | 非 root、无特权、无宿主挂载 | —— |

#### 22.8.8 新增配置项

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `STUDIO_EXEC_ADDR` | 空 | studio 的 gRPC 地址（如 `studio:9000`）。为空 = 未启用脚本执行 |
| `STUDIO_EXEC_REQUIRED` | `true` | studio 不可达时是否启动失败 |
| `STUDIO_EXEC_TIMEOUT_MS` | 30000 | 单次执行默认时限 |
| `STUDIO_EXEC_MAX_TIMEOUT_MS` | 60000 | 模型能请求的时限上限（写死在配置里，模型改不了） |
| `STUDIO_EXEC_MAX_SCRIPT_CHARS` | 200000 | 脚本正文长度上限 |
| `STUDIO_EXEC_LB_POLICY` | `round_robin` | gRPC 的负载均衡策略。Phase 3 落地时补的——见下 |
| `STUDIO_EXEC_MIN_VERSION` | `1.0.0` | studio 的最低版本。默认不卡 fast-only 部署；声明 isolated 的技能另要求 `>= 2.0.0` |
| `STUDIO_EXEC_HEALTH_TTL_SEC` | `30` | studio 能力缓存的 TTL；isolated 调用前按它刷新（0 = 每次刷新） |

**「地址为空」与「连不上」是两件事**，别混：

| 情形 | 结果 |
| --- | --- |
| 地址为空 | **未启用脚本执行**：服务正常启动，`skill_exec` 根本不注册（配置期也拒） |
| 地址有 + 连上了 | 启用 |
| 地址有 + 连不上 + `STUDIO_EXEC_REQUIRED=true` | **启动失败**（§22.8.6） |
| 地址有 + 连不上 + `STUDIO_EXEC_REQUIRED=false` | 正常启动，`skill_exec` 不注册，只记一条警告 |

**技能怎么声明档位（2.0.0）**：SKILL.md 的 `metadata` 用扁平键声明——
`runtime-profile: isolated` / `runtime-image: ml-py311` / `runtime-timeout-ms: "60000"`。
档位是脚本的**事实**，随技能走；`STUDIO_ISOLATED_SKILL_CODES` 只是运维的**升级覆盖**。
服务端只能升、不能降，响应里的 `profile` 必须回显实际档位，调用方拿它做最后一道断言
（不对账就拒绝结果，见 skill-service §14.3）。声明 isolated 的技能要求 studio
`>= 2.0.0` 且 `Health.profiles[isolated].ready=true`。

**`STUDIO_EXEC_LB_POLICY` 为什么必须有**（skill-service §13.4）：gRPC 用 HTTP/2
**长连接**，默认的 `pick_first` 会**只连上第一个 DNS 解析结果**，之后再也不换。
配上普通 ClusterIP Service，扩缩容就等于白扩——加了 Pod，流量一点都不过去。而这个
症状极难发现：服务看着完全正常，只是没吃满。

所以客户端建通道时给地址加 `dns:///` 前缀（让 gRPC 的 DNS 解析器返回**全部**
A 记录）并指定 `round_robin`。**Service 侧也必须是 headless 的**——两边配对才有
意义，单改一边等于没改。

#### 22.8.9 落地情况（Phase 3）

| 关注点 | 代码在哪 |
| --- | --- |
| 契约（唯一来源） | `proto/webagent/studio/v1/studio.proto` |
| 生成的桩代码 | `webagent/studio/v1/`（包名就是 proto 的 `package`，生成代码一行都不用改） |
| 契约一致性检查 | `scripts/check_proto_contract.py`（proto 文件 vs 本节 vs `services/studio/DESIGN.md` §5） |
| gRPC 客户端 | `app/core/studio.py`（`StudioClient` / `init_studio` / 错误码翻译） |
| 每次调用的执行上下文 | `app/core/studio_context.py`（`request_id` 靠 run + 节点 + tool_call_id 算） |
| `skill_exec` 工具 | `app/core/skill_exec.py`（校验 + 规整）、`app/core/tools.py`（handler + 注册开关） |
| 技能运行档声明 | `app/core/skill_runtime.py`（`runtime-profile` 解析/校验/启动期审计） |
| 启动期检查与注册联动 | `app/main.py`（lifespan）、`app/core/tools.py::set_optional_tools_enabled` |
| 敏感词兜底 | `app/core/tools.py::_screen_tool_output`（MCP 与 skill_exec 共用） |
| **服务侧参考实现** | `services/studio/`（gRPC server + 进程隔离 + 超时 + 截断 + 限流 + 去重 + 优雅停机） |
| **isolated Docker 后端** | `services/sandbox/`（runner + 控制帧 + 镜像）、`services/dispatcher/`（只调度的控制面）、`services/studio/dispatcher_runtime.py`（studio 侧 HTTP 客户端） |
| 探针 / 启动巡检 | `services/studio/healthcheck.py`、`services/studio/reaper.py` |
| 部署产物 | `services/studio/Dockerfile`、`services/sandbox/Dockerfile`、`services/dispatcher/Dockerfile`、`deploy/k8s/`、`deploy/docker-compose.yml` |

**为什么本仓库同时交付服务侧**：§22.13 的验收项「脚本在 studio 容器里跑、web_agent
进程内没有起任何子进程」必须有一个**真的** studio 才能验证。只做调用侧 + mock，
这条验收就变成空话。

### 22.9 Skill 与 MCP 的关系

三者的分工，一句话讲清：

> **Skill 负责「什么时候用、怎么用」，MCP 负责「能用什么」，白名单负责「准不准」。**

`SKILL.md` 的 frontmatter 里有一个 `allowed-tools` 字段（规范标注为**实验性**），形如
`allowed-tools: Bash(git:*) Read`。本项目对它的口径是：

- **它只是建议，不是授权**；
- **授权的唯一依据仍然是 `agent.tools`**（§17.2 的规矩：模型能调的与服务端能执行的必须是同一
  张表）；
- 实现上：后台在 Agent 编辑页提示「技能 `financial-report` 声明要用工具 `get_quarter_metrics`，
  但本 Agent 的工具白名单里没有它」——**提示，不拒绝**。

为什么是提示不是拒绝：技能的正文里可能只是**提到**某个工具名（"这个报告需要配合
`get_quarter_metrics` 使用"），并不是真的要调它。按名字硬拒会误伤。按名字硬放行则是把授权
交给技能作者——那就把 §17.2 的护栏拆了。

**MCP 工具不知道技能的存在**：MCP 工具的 schema 里没有技能上下文。模型知不知道「这个技能要我
用它」，取决于技能正文有没有把话说清楚。这是技能作者的责任，不是协议的责任。

### 22.10 安全边界

**Skill 是提示词内容。进了 prompt 的文字，模型会当成指令执行。**

所以技能的可信级别 = **能写 `skill` 表的人的级别**。这句话必须写在文档最显眼的地方：

> 能写 `skill` 表的人 ≈ 拿到了提示词注入权限 ≈ 能改模型的行为。
> 这与「拿到代码仓库写权限 ≈ 能改代码」是同一句话。

**但技能进了数据库之后，这句话的重量变了，必须重新说清楚。**

技能放在部署目录里时，「能改技能」需要**改代码 + 走部署**——那是一道物理阻力。现在技能在
DB 里、后台就能改，这道阻力**没了**。所以权限必须从别处补回来，而且要明确补在哪：

1. **写技能是管理员专属**，与「注册 MCP 服务器」同一级别（§21.10 第 1 条）。**能编辑 Agent
   配置的用户不能编辑技能**——Agent 配置影响「用哪些工具」，技能正文影响「模型按什么步骤
   做事」，后者更接近代码。
2. **每一次正文与资源的变更都写审计**（`skill_revision`）：版本号、谁改的、改了什么。
3. **导入路径要单独设一道闸**：导入的技能 `origin='import'` 且**默认 `enabled=0`**，必须由
   管理员显式启用才能被 Agent 绑定。理由：导入内容的来源**不可信**（别人写的技能包），
   而手工创建的技能来源可信（管理员自己写的）。这条是「手工 / 导入」唯一的差别。
4. **`allowed-tools` 不授权**（§22.9）。技能正文不能扩大 Agent 的工具白名单。
5. **frontmatter 与字段在保存期就拒**：缺 `name` / `description`、`name` 超过 64 字符、含
   大写字母、`code` 与 `name` 不一致 → **API 直接 400**，不落库。（原来是「不导入」，现在是
   「不让它进来」。）
6. **长度与预算上限**（§22.6）：正文单文件上限、资源单文件 5 MB / 单技能总量 20 MB、技能正文
   总预算、注入技能数上限，四道都卡死。
7. **web_agent 进程里没有脚本执行器**。正文里的代码块不会被运行；`scripts/` 里的脚本**只**能
   经 `skill_exec` 交给 studio 容器执行（§22.8），web_agent 侧**没有** `subprocess` 这条路。
8. **技能正文不参与敏感词前置过滤**（§22.6）。它是管理员写进数据库的内容，不是用户输入。

**导入路径的三道额外防护**（`POST /api/skills/import`）：

| 防护 | 挡什么 |
| --- | --- |
| **zip slip**：逐个 entry 校验目标路径必须在解压根内，拒绝绝对路径与 `..` | 解压把文件写到目标目录之外 |
| **拒绝符号链接 entry**（zip 里的 symlink） | 软链到宿主文件 |
| **zip bomb**：限制 entry 数、单文件大小、**解压后总大小**、压缩比 | 一个 zip 撑爆磁盘/内存 |
| 无关文件（`.DS_Store`、`__MACOSX/`）**跳过**；路径非法的 entry **报错**（不静默跳过） | 静默跳过会让「导入成功但少文件」 |

**脚本执行是另一条信任边界**，单独说：

> 技能脚本会**真的被执行**。它由**模型指定的参数**驱动，而模型会被提示词注入影响。
> 所以「谁能写 `skill` 表」的级别 = **能让任意代码在 studio 容器里跑**的级别。

studio 容器是这条边界的**唯一**实现手段，它必须满足（§22.8.7）：**零凭据、只读根文件系统、
仅 `/tmp` 可写、禁出网、CPU/内存/时限受限、非 root**。

三条由此而来的硬约束：

1. **凭据不进容器**。studio 容器里**一个凭据都没有**——它不需要数据库口令、不需要
   `SECRET_KEY`、不需要任何 API key。需要凭据的技能**做不了**，这是有意的。
2. **脚本输出不进数据库**。stdout 可能含从容器里读到的数据，落库就是留副本；它只作为
   `role=tool` 的内容回灌并截断（§22.8.4）。
3. **studio 的出网要禁掉**。容器禁出网后，脚本拿不到外网，也没法把容器里的东西往外送；
   需要出网的技能走 MCP 工具，而不是在脚本里 `curl`。

### 22.11 API 设计

技能进了数据库，就有了**读**与**写**两套接口。写接口全部是**管理员专属**（§22.10）。

#### 只读（普通运营可用）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/skills` | 技能列表：`code` / `name` / `description` / `origin` / `version` / `content_hash` / `enabled` / 被哪些 Agent 绑定 |
| GET | `/api/skills/:code` | 详情 + 正文 + frontmatter 字段 |
| GET | `/api/skills/:code/resources` | 资源清单（path / 大小 / mimeType / sha256） |
| GET | `/api/skills/:code/resources/*path` | 单个资源内容（二进制走 `Content-Disposition: attachment`） |
| GET | `/api/skills/:code/export` | **导出标准技能包**：重新拼出 `SKILL.md`（frontmatter + body），资源按原路径还原。`?format=zip`（默认，打包成 zip）/ `?format=md`（只给 SKILL.md 原文） |
| GET | `/api/skills/:code/revisions` | 变更历史（版本 / 作者 / 说明 / 时间） |
| GET | `/api/agents/:code/prompt-preview` | **生效提示词预览**：`system_prompt` + 各技能正文，与实际发给模型的逐字一致 |
| GET | `/api/studio` | studio 状态：可达性、版本、可用运行时（`HealthResponse`）——**Phase 3 才实现** |

#### 写入（**管理员**）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/skills` | 创建技能。`origin='manual'`，写审计 |
| PUT | `/api/skills/:code` | 更新（正文 / frontmatter）。`version+1`，写审计 |
| DELETE | `/api/skills/:code` | 删除技能（含资源与修订历史）。**还被 Agent 绑着 → 409**，先解绑或停用 |
| POST | `/api/skills/:code/resources` | 上传或替换一个资源文件。请求体是**原始字节**（`Content-Type` 别用 `application/json`），路径放 query 参数或 `X-Resource-Path` 头 |
| DELETE | `/api/skills/:code/resources/*path` | 删除一个资源文件 |
| POST | `/api/skills/import` | **导入（zip 形式）**：multipart 传一个 zip。`origin='import'`，**默认 `enabled=0`** |
| POST | `/api/skills/import/entries` | **导入（目录结构形式）**：JSON `{entries:[{path, content_base64}], enabled}`。同样默认 `enabled=0` |
| POST | `/api/skills/:code/rollback` | 回滚到指定历史版本（正文 + 文本资源；二进制资源不回滚，§22.12） |

**导出的对称性**：`export` 出的东西正好是 `import` 能吃进去的东西——`import` 吃一个「导出物」，
`export` 吐一个「导出物」。这条对称性是 §22.4 那段「只有一个格式转换实现」的落地保证。

**导入的两种输入形式**：

| 形式 | 入口 | 注意 |
| --- | --- | --- |
| **zip** | `POST /api/skills/import`（multipart） | 必须防 zip slip 与 zip bomb（§22.10 的三道防护） |
| **目录结构**（JSON：`[{path, contentBase64}]`） | `POST /api/skills/import/entries` | 走**同一套**路径格式校验，不需要解压逻辑 |

两者最终都落到同一组校验上：**path 格式合规 + 内容大小上限 +（仅 zip）zip slip / bomb 防护**。

**为什么分成两个端点而不是一个**：multipart 与 JSON body 在 FastAPI 里没法塞进同一个函数
签名（`UploadFile` 与 JSON 模型二选一）。分成两个端点后，它们仍然落到**同一组**校验上
（同一套 `validate_resource_path` + 同一套体积上限 + 同一个 `parse_skill_package`），
所以「两条路会不会校验得不一样」这个问题不存在。

**版本号与修订快照的对应关系**：`skill_revision` 的唯一键是 `(skill_id, version)`，
所以**每一次写入都要让 `skill.version` 往前走**——改正文、写资源、删资源、回滚，都算。
于是「版本号」唯一对应一份内容，回滚到 `v3` 才有确定的含义。回滚本身是**新版本**
（内容回到旧样子，版本继续递增），所以版本号单调递增、不会自我覆盖。

**请求体上限的按路径放宽**：全局上限是 2 MB（§8），而「单资源 5 MB」这条领域约束在
HTTP 上够不着——技能包根本传不进来。所以 `/api/skills` 前缀有专属上限
（`SKILL_UPLOAD_MAX_BYTES`，默认 8 MB），但**只对带对管理员密钥的请求生效**：
未认证的调用方在同一条路径上仍然受 2 MB 全局上限约束。加个上传功能不该顺手把
未认证流量能撑的缓冲口放大。

#### 工具目录

`GET /api/tools` 里会多出两条内置工具（与 `now` / `calc` 同级），运营可以在 Agent 的工具
白名单里勾上或取消——**不勾模型就用不到**：

| 工具 | 不勾的后果 |
| --- | --- |
| `skill_resource` | 模型读不到技能资源（`references/`、`assets/`） |
| `skill_exec` | 模型执行不了技能脚本（studio 未启用时这条工具**根本不注册**，配置期直接拒） |

### 22.12 已知限制

1. **脚本只能跑在 studio 容器里**（§22.8）。web_agent 进程内没有任何脚本执行器——这既是
   安全设计，也意味着**本地开发必须起一个 studio 容器**（或用 `STUDIO_EXEC_REQUIRED=false`
   显式关掉，那就没有脚本执行能力）。
2. **studio 是单点**。它挂了，脚本执行能力整体不可用；本设计的对策是**启动期就失败**
   （§22.8.6），不是运行期降级。
3. **studio 容器里没有凭据、不能出网**（§22.10）。需要凭据或需要外网的技能**做不了**——
   这类需求应该走 MCP 工具，不该在脚本里硬凑。
4. **取消不保证杀掉脚本**。gRPC 取消不传播到对端的进程组，studio 只能靠自己的时限收口
   （§22.8.5）。
5. **模型不自己挑技能**（§22.3）。技能绑多了只能靠预算截断，靠后被砍掉的技能模型完全看不见。
6. **技能正文占固定成本**。每个绑定的技能，每一轮模型调用都要带一遍（system 消息在多轮里
   会被重复发送）。技能多、正文长时 token 成本上升明显。
7. **二进制资源的回滚不支持**。`skill_revision` 存正文快照 + 资源清单（文本资源带内容，
   二进制资源只带 sha256），所以**文本资源能回滚、二进制资源不能**。这是明确的取舍——
   快照所有二进制会让审计表体积翻倍，不值。
8. **资源总量有上限**。单资源 5 MB、单技能总量 20 MB。这挡得住「把整个数据集塞进技能」
   这类用法，但也意味着**技能不能当数据仓库用**。
9. **技能表没有租户维度**。多租户隔离没做——要先把「数据模型 ↔ 租户模型」对齐，而本项目目前
   没有租户概念（§1.1 的目标是单部署多 Agent）。
10. **正文与资源存在数据库里，没有 git 的 diff/review**。`skill_revision` 给的是「谁在什么时候
    改了什么」与版本快照，**不是**逐行 diff。真要 review 文本改动，得靠导出后在外面比。
11. **没有草稿态**。技能要么是当前版本，要么是历史版本，没有「编辑中未发布」的中间态。
12. **删除是硬删除**。`DELETE /api/skills/:code` 会连同资源与修订历史一起删掉。软删除（`enabled=0`
    之外再加个 `deleted_at`）本轮不做。
13. **`allowed-tools` 不做自动授权**，只做提示（§22.9）。
14. **`skill_resource` 只能读技能自己的资源**，读不了部署里别处的文件——本项目没有通用的
    文件读取工具（模型侧的文件访问能力为零）。
15. **脚本语言受 studio 镜像限制**。镜像里装了哪几种运行时就是哪几种，`HealthResponse.language_info`
    把它报出来；配置期按它校验（§22.8.8），但**新增语言要改 studio 镜像**。
16. **管理员是共享密钥，不是用户体系**。审计里的 `author` 只有一个固定值（`ADMIN_AUTHOR`）——
    多人共用一把密钥时，审计能回答「什么时候改的」，但回答不了「是谁改的」。有了用户
    体系之后把 `require_admin` 换成按用户查权限即可，调用点不用动。
17. **后台页还没有技能管理界面**。API 全齐了（§22.11），但 `web/admin.html` 里没有技能面板：
    创建 / 编辑 / 导入 / 导出 / 绑定都还没有 UI，只能走 API 或脚本。
18. **技能正文不过敏感词前置过滤**。§20 那道过滤只拦**用户输入**；技能正文是管理员写的，
    准入靠 admin 权限 + 审计留痕，而不是靠过滤器。
19. **技能上传的请求体上限与管理员门禁共用同一把密钥**。放宽只对带对 `X-Admin-Key` 的请求
    生效（§22.11），所以密钥一旦泄露，放宽后的上限也一起泄露了——两者是同一条信任边界。
20. **`isolated` 隔离档的 Docker 后端已落地，K8s 后端还没有**。档位路由、独立并发池、
    请求/回显、`Health` 按档位报能力、启动审计都在（skill-service §2.4、§8.4）；
    Phase 1 另加了 `services/sandbox/`（runner + 控制帧 + 镜像）与
    `services/dispatcher/`（只调度的控制面，Docker socket 只在这里）。schema 是
    「studio 不挂 socket，dispatcher 按固定模板起一次性容器」。**真 Docker e2e 尚未跑**
    （开发机沙箱起不了子进程），所以「容器里真的无网络/只读 rootfs」还只是代码与单测
    层面的保证；K8s Job 后端在 Phase 2。
21. **K8s 清单没有在真实集群上验证过**。`deploy/k8s/` 里的清单是照着文档逐条写的。
    **镜像与容器配置已经在真 Docker 上逐条实测过**（§22.14），但 NetworkPolicy 是否
    真的拦住出向、headless + `round_robin` 是否真把流量摊到多个 Pod 上，这两条**仍然
    未实测**——它们依赖集群的 CNI 与 DNS 行为，Docker 上只能用 `internal: true` 的网络
    等效实现，不等于 K8s 的 NetworkPolicy 生效。
    `deploy/k8s/README.md` 里列了部署前要先跑的三条自检命令。
22. **studio 服务侧是参考实现，不是生产就绪的服务**。`services/studio/` 做完了契约、进程隔离、
    超时、截断、限流、去重、优雅停机这些**功能**，指标导出与结构化日志也已补上
    （§12.1、§12.2）；isolated 的 Docker 执行体在 Phase 1 补上，K8s 执行体还没有。
    它的定位是「把调用侧与服务侧的边界钉清楚，并让验收能真的跑起来」，不是直接上生产。
23. **真实模型那一环已在真部署上实测**（§22.14）。用的是 MiniMax 的 **M Plan /
    Coding Plan 专用 key**——它与按量付费接口**不通用**（按量付费那把 key 在国内站与
    海外站一律 `402 insufficient_balance` / `401 invalid api key`），端点与 key 一换就通了。
    四个场景（正常 / 超时 / 忙不过来 / studio 宕机）全部符合预期。

### 22.13 实施要点与验收标准

> 勾选状态对应 **Phase 1（Skill 基础）**与 **Phase 3（studio 与 `skill_exec`）**的落地情况。
> 每条后面标了钉它的测试；Phase 3 的测试在 `tests/test_studio_*.py`。

- [x] `POST /api/skills` 建一个技能 → `skill` 表出现对应行，`content_hash` 与
      `sha256(body ‖ 各资源摘要)` 一致，`version=1`，`origin='manual'`。
- [x] 建一个 `code` 与 SKILL.md 的 name 规则不符（大小写、下划线、连续连字符）的技能 →
      **API 直接 400**，库里没有这行（是「不让它进来」，不是「先存着再说」）。
- [x] **非管理员**调 `POST` / `PUT` / `DELETE /api/skills*` → **401**，且**不写任何数据**。
      （用 401 而不是 403：共享密钥判的是「你是谁」，不是「你能不能」——没带对密钥就是
      没认证过；等有了用户体系，403 才是对的。）
- [x] 管理员改一次正文 → `version` +1，`skill_revision` 多一行（作者、note、时间），资源清单
      也在快照里。
- [x] `GET /api/skills/:code/export` → 拿到的包能被 `POST /api/skills/import` 原样吃回去
      （导出物 = 导入物）。
- [x] 导入一个 zip → 技能 `origin='import'` 且 **`enabled=0`**；Agent 绑它 → **保存期就拒**。
- [x] 导入一个含 `../../etc/passwd` 条目的 zip → **导入失败并报错**，不落库（zip slip）。
- [x] 导入一个解压后远超上限的 zip → **导入失败并报错**（zip bomb：条目数 / 单文件 / 总量 /
      压缩比四道，压缩比在**解压之前**判）。
- [x] 导入一个含符号链接条目的 zip → **导入失败并报错**。
- [x] Agent 绑定技能 → 发给 LLM 的 messages 里出现一条独立的 `role=system` 消息，内容是
      技能正文，**且在 Agent 自己的 system_prompt 之后**。
- [x] 后台的「生效提示词预览」与实际发给 LLM 的内容**逐字一致**（同一份拼装函数
      `Orchestrator._build_skill_plan`）。
- [x] 绑超过上限的技能 → 只有靠前的几个被注入，后面的**整个**不出现，且事件流里有一条
      `skill_skipped` 说明是哪个、为什么。
- [x] `skill_resource` 读 `references/METRICS.md` → 返回文件内容。
- [x] `skill_resource` 读 `../../../../etc/passwd` → **查不到任何资源行**，返回错误说明，
      **不泄露任何内容**（不再有文件系统可穿越）。
- [x] `skill_resource` 读一个没绑定的技能 → 拒绝（白名单校验），且回复**不因技能存不存在
      而不同**（否则那是个探测接口）。
- [x] `skill_resource` 读一个二进制资源 → 只回「文件名 + 大小 + mimeType」，**不把字节塞进
      上下文**。
- [x] 上传一个路径含 `..` 或绝对路径的资源 → **400，不入库**。
- [x] `skill_exec` 执行 `scripts/extract.py` → 脚本**在 studio 容器里**跑，web_agent 进程内
      **没有**起任何子进程（用 `ps` 或打桩断言验证）；退出码与 stdout 原样回灌给模型。
      → `tests/test_studio_e2e.py::test_skill_exec_runs_script_in_studio_and_feeds_model`
      （真 studio + 真 uvicorn + 假 LLM；脚本的 stdout 确实进了下一轮请求的
      `role=tool` 消息）
      → **真部署上的实测**：真 Docker + 真 MySQL + 真 MiniMax，脚本在 studio 容器里
      跑，模型把退出码与 stdout 逐字转述（§22.14.2）
- [x] **模型知道该传哪个技能 code** → `skill` 参数的描述里列着实际可用的 code，
      模型第一次就调对，不用猜。
      → `tests/test_skill_exec.py::test_skill_param_lists_the_actual_bound_codes`
      （这条是**真模型**逼出来的：原先 schema 里只有一句举例，真模型连猜十来个全被拒）
- [x] `skill_exec` 的脚本参数含 `../../etc/passwd` 式的路径 → **在 web_agent 侧**就被拒，
      请求**根本没发出去** studio（studio 收到的一定是已校验过的脚本正文）。
      → `tests/test_studio_e2e.py::test_rejected_request_never_reaches_studio`
      （断言 studio 的 `Exec` 调用次数**一次都没涨**）；另有路径穿越、不在 `scripts/`
      目录、argv 含 `..` 三种形态
- [x] 脚本超时 → studio 侧超时生效、进程组被杀；web_agent 侧拿到 `status=TIMEOUT` 与 `error`
      说明，节点**不失败**（§17.5 口径）。
      → `tests/test_studio_e2e.py::test_script_timeout_is_reported_not_hung`
- [x] studio 不可达 → **服务启动失败**（§22.8.6），而不是运行期才炸。
      → `tests/test_studio_startup.py`
- [x] `STUDIO_EXEC_REQUIRED=false` 且 studio 不可达 → 服务正常启动，但 `skill_exec` 不注册，
      且保存期拒绝把 `skill_exec` 加进 Agent 的工具白名单。
      → `tests/test_studio_startup.py`、`tests/test_skill_exec.py::test_skill_exec_cannot_be_configured_when_unavailable`
- [x] 脚本 stdout 里包含一段敏感内容 → 回灌前**被 §20 的过滤器拦下**（与 MCP 工具结果同一
      条规矩，§21.10 第 4 条）。
      → `tests/test_tool_output_sensitive.py::test_sensitive_script_output_is_not_fed_back`、
      `test_sensitive_script_stderr_is_not_fed_back`（**失败的**脚本其 stderr 同样要筛——
      它照样会被回灌）
- [x] **gRPC 成功 ≠ 脚本成功**：脚本自己 `exit 3` 是 `completed` + `exit_code=3`，不是错误；
      stderr 原样回灌给模型，节点**不失败**。
      → `tests/test_studio_e2e.py::test_nonzero_exit_is_fed_back_to_model_not_a_service_error`
- [x] **studio 挂掉时节点不失败**：工具失败（`ok=false`）+「连不上 studio」回灌给模型，run 正常结束。
      → `tests/test_studio_e2e.py::test_studio_down_fails_tool_but_run_survives`
- [x] **studio 忙不过来时节点不失败**：`RESOURCE_EXHAUSTED` 翻译成「忙不过来」回灌给模型。
      → `tests/test_studio_e2e.py::test_studio_saturated_is_reported_to_model`
- [x] **技能没绑到 Agent 上**：白名单校验先于存在性检查，拒绝且不泄露「它在不在」；
      请求同样没发出去。
      → `tests/test_studio_e2e.py::test_skill_not_bound_to_agent_is_refused`
- [x] **契约在仓库里只有一份**：proto 文件 + 本文档 §22.8.3 + `services/studio/DESIGN.md` §5 三处
      逐字一致，CI 脚本比对不一致时打印 diff 并非 0 退出。
      → `scripts/check_proto_contract.py`、`tests/test_proto_contract.py`
- [x] **优雅停机先摘 readiness 再 drain**（§13.5）：停机时在跑的执行能跑完，不是被腰斩。
      → `tests/test_studio_service.py::test_graceful_shutdown_drops_readiness_before_draining`
- [ ] 技能声明了 `allowed-tools: get_quarter_metrics`，而 Agent 白名单里没有它 → 后台给出
      **提示**，但保存**不被拒**。（字段已存、授权仍只看 `agent.tools`；后台提示 UI 待做，
      见第 17 条。）
- [x] 全量回归不破：现有 166 个测试（§17.9）全绿（Phase 1 落地后为 **358 passed / 19 skipped**）。
      → Phase 3 落地时全量 **546 passed / 22 skipped**（Phase 2 终态是 428 / 19，新增 118 个
      studio 相关用例；22 个跳过里 3 个是本机没有 bash 与没有 `/proc` 导致的）。

---

### 22.14 真实环境实测记录（2026-11-03）

> 这一节记的是**在真 Docker 上跑出来的**结果，不是设计意图。
> 机器：Alpine 3.24 / Docker 29.9.2 / Compose v5.6.1 / 4 vCPU / 1884 MB；
> 数据库用宿主机的 MySQL 8 系与 Redis；编排用 `deploy/docker-compose.lab.yml`。

#### 22.14.1 实测到了什么

| 项 | 结果 |
| --- | --- |
| 迁移链**从零**跑通 | `alembic upgrade head` 在真 MySQL 上从空库建出全部 14 张表，`alembic_version` 落到 head |
| `skill_exec` 真链路 | 真 DB 读脚本 → 真 gRPC → 真 studio 容器 → stdout 回灌，web_agent 进程内**没起任何子进程** |
| 三道校验在 web_agent 侧 | 白名单 / 脚本路径格式 / argv 形态共 20 条用例全过；**校验不过的请求根本没发出去 studio** |
| 容器加固 | 只读 rootfs、非 root、drop capabilities、no-new-privileges、禁出网、零凭据、环境变量白名单——**11 项全过** |
| 禁出网的落地方式 | studio 只挂 `internal: true` 的网络（无默认网关）：容器内解析外网域名失败，但能连到 `web-agent:8000` |
| 进程组隔离 | 脚本起子进程后超时 → **整个进程组被杀**（子进程状态变 `Z`） |
| 队列闸门 | 并发上限 2 + 队列 4，发 12 个请求 → 接受 6 个、拒 6 个（`RESOURCE_EXHAUSTED`） |
| studio 宕机 | `docker stop studio` → web_agent 侧拿到「连不上 studio（studio:9000）」，**不抛异常打断 run** |
| gRPC 成功 ≠ 脚本成功 | 脚本 `exit 1` → `COMPLETED` + `exit_code=1`，stdout/stderr 都保留 |
| 容器级 OOM | 256 MB 限额的容器里跑一个申请 1 GB 的脚本 → 内核 OOM killer **杀脚本、studio 存活** → `LIMITED` + `exit_code=-1` + 「内存超出容器限额」的说明；cgroup 的 `oom_kill` 计数 +1 |

#### 22.14.2 真实模型端到端（五个场景）

用的是 MiniMax **M Plan / Coding Plan 专用 key**，端点 `https://api.minimax.cn/v1`，
模型 `MiniMax-M3.1-Flash`。五个场景全部符合预期：

| 场景 | 模型的第一次工具调用 | 工具结果 | run |
| --- | --- | --- | --- |
| **happy** | `skill_exec(skill=lab-happy-xxxxxx, script=scripts/hello.py)` | `ok=true`，`exit_code: 0`，stdout 含 `hello from studio container` / `cwd=/tmp/studio-xxxx` / `whoami=studio` | ✅ |
| **timeout** | 同上，`scripts/slow.py` | `ok=false`，`status: timeout`、`exit_code: -1`、`已杀掉进程组` | ✅ **没失败** |
| **busy** | 同上（studio 被占满） | `ok=false`，`studio 忙不过来（studio:9000）：等待队列已满（4）` | ✅ **没失败** |
| **down** | 同上（studio 被停掉） | `ok=false`，`连不上 studio（studio:9000）：… Connection refused` | ✅ **没失败** |
| **oom** | 同上，`scripts/hungry.py`（申请 1 GB） | `ok=false`，`status: limited`、`exit_code: -1`、`脚本被 OOM killer 杀掉（退出码 -9（信号 9））：内存超出容器限额` | ✅ **没失败** |

happy 场景的**最终回答**里，模型把工具返回的内容**逐字转述**了：

```text
脚本已执行完成，工具返回内容原样如下：

exit_code: 0
--- stdout ---
hello from studio container
argv1=none
cwd=/tmp/studio-_5jpft4k
whoami=studio
--- stderr ---
```

这就是 §22.13 那条验收项「退出码与 stdout 原样回灌给模型」在真部署上的证据。

**oom 场景**（本轮新增）里，模型把工具返回的 `LIMITED` 原样转述，还自己读懂了原因：

```text
脚本执行失败了，工具返回的内容原样如下：

错误：status: limited
exit_code: -1
error: 脚本被 OOM killer 杀掉（退出码 -9（信号 9））：内存超出容器限额。请减少内存占用、分批处理，或改成流式处理。
--- stdout ---

--- stderr ---
```

关键点：**工具失败没有变成 run 失败**（§17.5）——模型看到「脚本被 OOM 杀了」之后，
还能给出「可以再跑一次并尝试降低内存占用」这样的下一步建议。

#### 22.14.3 实测挖出来的七个 bug

这七个**全都是本地测试全绿、真部署才炸**的——这也是为什么 §22.13 的验收要能在
真环境里跑。

| # | 现象 | 根因 | 修法 |
| --- | --- | --- | --- |
| 1 | studio 容器起不来：`ModuleNotFoundError: No module named 'google'` | 生成的桩 `from google.protobuf import descriptor`，但 studio 镜像只装了 `grpcio`——protobuf 是**运行期依赖**，不能靠 `grpcio-tools` 传递带进来 | `services/studio/requirements.txt` 显式加 `protobuf`；加 `tests/test_studio_deps.py` 用 `importlib.metadata` 静态查（`google`→`protobuf` 靠猜是猜不出来的） |
| 2 | 只读 rootfs 下 `run.py` 崩在写 `.env` | `ensure_env_file()` 无条件 `write_text` | 捕获 `OSError` 并提示「容器化部署请用环境变量注入配置」 |
| 3 | MySQL 建表失败：`1101, "BLOB, TEXT, GEOMETRY or JSON column 'prompt' can't have a default value"` | 迁移给 TEXT 列带了字面量 `server_default`——MySQL 不允许，SQLite 与 PostgreSQL 都接受 | 去掉该列的 `server_default`，改用 ORM 侧的 Python `default`；加 AST 静态守卫 |
| 4 | MySQL 改列失败：`1067, "Invalid default value for 'requested_at'"` | SQLAlchemy 渲染 `CREATE TABLE` 时会把 `sa.func.now()` 包成 `(now())`，渲染 `ALTER TABLE ... CHANGE` 时却原样输出 `now()`——**MySQL 的表达式默认值必须带括号** | 迁移里改用 `sa.text("(now())")`；加一条**离线渲染 MySQL DDL** 的守卫测试（`alembic upgrade head --sql`，不需要数据库） |
| 5 | 脚本间歇性以 `exit_code=-6` 收场，stdout/stderr 全空 | **在跑 gRPC 的进程里 fork**：gRPC 的 `pthread_atfork` 处理器在「fork 时别的线程正在 gRPC 里」时放弃清理，子进程带着坏掉的 epoll 状态去 exec，exec 之前被 glog 的断言 `abort()` 掉 | `GRPC_ENABLE_FORK_SUPPORT=0`，且必须在 `import grpc` **之前**设（`services/studio/__init__.py`）；加 `tests/test_studio_runtime_guards.py` |
| 6 | 容器里攒僵尸进程 | 脚本被超时杀掉后，它起的子进程被重新挂到 PID 1（studio）底下；python 不是 init、不回收孤儿，被杀死的孙进程变成僵尸（`/proc/<pid>` 还在、状态 `Z`），PID 表被慢慢占满 | 镜像里用 `tini` 当 PID 1（`ENTRYPOINT ["/usr/bin/tini", "--"]`） |
| 7 | **真模型不知道该传哪个技能 code**——它连着猜了 `hello`、`hello-script`、`hello-world` 等十来个，全被白名单拒掉，最后撞上「工具调用超过 5 轮」失败 | 模型判断「调哪个技能」的唯一依据是提示词与工具 schema。技能正文（§22.6）注入的是**正文**，正文里不一定写着自己的 code；工具 schema 里原先也只有一句举例（「例如 financial-report」）。**假 LLM 的测试测不到**——它早就被写死成正确的 code 了 | `skill` 参数的描述**按当前上下文动态生成**，列出实际可用的 code（与执行期白名单同一份）。`skill_exec` 与 `skill_resource` 都改；注册表**不改**（逐层浅拷贝），否则并发节点会互相串味 |

第 3、4 两条还暴露了一个**方法论**上的问题：**SQLite 上的迁移测试抓不到服务端会拒绝的
DDL**。现在的守卫是两条——一条 AST 静态扫（禁止 TEXT/JSON/BLOB 列带 `server_default`），
一条**离线渲染 MySQL DDL 再检查**（`alembic upgrade head --sql`，连数据库都不需要，
却能看见发给 MySQL 的每一句 SQL）。

第 7 条暴露的是另一个：**假 LLM 的测试替模型做了它做不到的事**。假 LLM 被写死成
「知道正确的技能 code」，于是这个缺口在本地永远看不见——换成真模型，第一轮就撞上了。
凡是「模型自己决定填什么参数」的地方，假 LLM 都得**按真模型的盲区**来设计用例，
否则测的是自己的假设，不是产品的行为。

#### 22.14.4 补上的：容器级 OOM（skill-service §13.2）

上一轮清单里挂着「§13.2 的 137 退出码判定也还没实现」——**契约里写着的语义，代码里
根本没有那条路径**。这不是「还没测」，是**功能没做**：脚本被 OOM killer 杀掉时，
studio 报的是 `COMPLETED` + `exit_code=-9`，模型拿到一句没头没尾的退出码，
完全看不出「这个脚本是被资源限制干掉的」。

本轮补上（`services/studio/oom.py` + `services/studio/executor.py`）：

| 做什么 | 为什么 |
| --- | --- |
| 退出码是 `-9` 或 `137` → `LIMITED` + `exit_code=-1` + 说明文字 | 两种形状都要认：POSIX 的 `subprocess` 记 `-N`，容器与 shell 记 `128+N` |
| cgroup v2 的 `oom_kill` 计数作**佐证** | 退出码只说「被 SIGKILL 了」，说不了「为什么」；计数涨了才敢说「确实超了内存」 |
| 脚本子进程的 `oom_score_adj` 调到 1000 | 内核的 OOM killer 按内存占用挑进程——吃内存的脚本本来就是首选，但「碰巧」不等于「必然」 |

**实测（真容器，256 MB 限额）**：脚本申请 1 GB → 内核 OOM killer 杀脚本 → 返回
`LIMITED` + `exit_code=-1` + 「脚本被 OOM killer 杀掉（退出码 -9（信号 9））：内存超出
容器限额」；**studio 容器活着**（`ready=true`），cgroup 的 `oom_kill` 计数 +1。
真模型端到端也过了（§22.14.2 的 oom 场景）。

**顺带挖出一个「文档没写、实测才知道」的坑**：`prctl(PR_SET_OOM_SCORE_ADJ, 1000)`
在容器里**返回 0 却什么都不做**——带不带 `argtypes`、uid 1000 还是 uid 0，都一样
（原因未查清）。**写 `/proc/self/oom_score_adj` 才有效**，uid 1000 就能写，fork 出来的
子进程里写也生效。所以实现走 `/proc` 路径，并且**回读验证**（不信任何人的返回值，
包括 `prctl` 的）。另外实测：内核只允许把值往「更易被杀」的方向调，往下调要
`CAP_SYS_RESOURCE`——正好是我们要的语义。

这条方法论跟前面七个 bug 是同一个：**没实测过的实现，谁也不知道它是不是真的在干活**。
本轮还发现，即使**什么都不做**，内核也会按内存占用挑进程，脚本照样被杀、studio 照样
存活——所以 `oom_score_adj` 是把「碰巧」变成「必然」，不是唯一的防线。

#### 22.14.5 还没验到的

- **K8s 侧**：NetworkPolicy 是否真拦出向、headless + `round_robin` 是否真摊流量、
  `terminationGracePeriodSeconds` 与 drain 超时的配合——都依赖集群行为，Docker 上
  无法等效验证（§22.12 第 21 条）。
- **studio 自己成为最大占用者时的 OOM**：实测里被杀的永远是吃内存的脚本，studio 活着。
  但如果 studio 自己的常驻内存反而比脚本大（泄漏之类），内核可能挑中它——那就会变成
  「连不上 studio」。这条没测，也不该靠 `oom_score_adj` 兜（它只加在脚本进程上）。
- **`isolated` Docker 后端没在真 Docker 里验过**：Phase 0 的档位/能力协商与 Phase 1 的
  `services/sandbox/` + `services/dispatcher/` 都有本地测试钉着（协议、dispatcher HTTP、
  Docker 固定模板、控制帧解析），但**真容器里的那一次还没跑**——开发机沙箱起不了子进程。
  `tests/test_sandbox_docker_e2e.py` 已写好，在有 Docker 的机器上跑一次即可补上；
  K8s 后端在 Phase 2。

---

*文档结束*
