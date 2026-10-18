# 多 Agent 协作 Web 服务设计文档（Python + MySQL）

> 版本：V1　|　状态：已实现　|　技术基础：Python 3.11+ / FastAPI / SQLAlchemy 2.0 async / MySQL 8

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
| Agent 编排 | 自研轻量编排器（V1），预留 LangGraph（V2） | V1 线性流水线自研足够；V2 复杂 DAG 引入 LangGraph |
| 进程内总线 | `asyncio.Queue` + `BusBackend` Protocol | 与事件循环一致，预留 Redis Pub/Sub 扩展位 |
| 分布式扩展 | Redis Pub/Sub（V2） | 替换 `BusBackend` 实现即可水平扩容 |

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
  tools          JSON         DEFAULT NULL,
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
| `error` | 错误 | 是 |
| `done` | 正常结束，流关闭 | 是 |
| `heartbeat` | 心跳 | 否，使用 SSE 注释行 |

> 心跳必须使用 SSE 注释行（以 `:` 开头），不触发 `onmessage`、不占用 `id`，避免污染 seq 序列导致回放错位。

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

### 6.2 V2：引入 LangGraph

当需要 DAG 条件路由、检查点恢复、人机协同时，引入 LangGraph：将 `AgentRunner` 封装为图节点函数，`run_step` 表对应 checkpointer 持久化层。

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
| GET | `/api/health` | 健康检查 |

### 错误码约定

| HTTP | 含义 |
| --- | --- |
| 400 | 请求参数非法（Workflow definition 校验失败、Agent 配置缺失等） |
| 401 | ticket 无效或过期 |
| 404 | run / conversation / agent / workflow 不存在 |
| 409 | 该会话已有 running 的 run，不允许并发发起 |
| 410 | run 已归档且事件已清理，无法回放（返回最终消息） |

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
│   │   ├── deps.py                # get_db, ticket 校验
│   │   └── routes/
│   │       ├── __init__.py
│   │       ├── agent.py
│   │       ├── workflow.py
│   │       ├── conversation.py
│   │       ├── run.py
│   │       ├── auth.py
│   │       └── health.py
│   ├── core/
│   │   ├── __init__.py
│   │   ├── orchestrator.py        # run 生命周期 + workflow 调度
│   │   ├── agent_runner.py        # 单 Agent 执行（AsyncOpenAI）
│   │   ├── run_hub.py             # seq 分配 + BusBackend
│   │   ├── sse_writer.py          # 回放 + 实时合并 + EventWriter 批量落库
│   │   ├── bus.py                 # BusBackend Protocol + InMemory 实现
│   │   ├── crypto.py              # api_key 加解密
│   │   └── workflow_def.py        # definition 解析与校验
│   ├── models/                    # SQLAlchemy ORM 模型
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── agent.py
│   │   ├── workflow.py
│   │   ├── conversation.py
│   │   ├── message.py
│   │   ├── run.py
│   │   └── run_event.py
│   ├── schemas/                   # Pydantic 请求/响应模型
│   ├── repo/                      # 数据访问
│   │   ├── __init__.py
│   │   ├── agent_repo.py
│   │   ├── workflow_repo.py
│   │   ├── conversation_repo.py
│   │   ├── message_repo.py
│   │   ├── run_repo.py
│   │   └── run_event_repo.py
│   ├── cache.py                   # Agent/Workflow 内存 LRU 缓存
│   └── config/
│       ├── __init__.py
│       ├── settings.py            # pydantic-settings
│       └── db.py                  # engine / SessionLocal / get_db
├── web/index.html                 # 功能验证台（单文件，无构建步骤）
├── alembic/
│   ├── env.py
│   └── versions/
├── docs/design.md
├── tests/
├── requirements.txt
├── .env.example
└── README.md
```

---

## 11. 演进路线

- **V1**（本次实现）：单实例、线性流水线、MySQL 持久化、SSE 续传
- **V2**：Redis Pub/Sub 替换进程内总线支持多实例；`run_event` 写入 Redis Stream 降低 MySQL 压力
- **V3**：Workflow 支持 DAG + 条件边 + 人工审批节点；Agent 支持 RAG / MCP 工具
- **V4**：可观测性（OpenTelemetry 链路追踪、token 成本统计）

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
| 工具调用 | 发 `tool_call` 事件 | 只记录不下发执行器 | V1 无工具注册表，执行属 V3 范围 |

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

*文档结束*
