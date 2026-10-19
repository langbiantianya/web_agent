# 多 Agent 协作 Web 服务设计文档（Python + MySQL）

> 版本：V3　|　状态：已实现　|　技术基础：Python 3.11+ / FastAPI / SQLAlchemy 2.0 async / MySQL 8

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

- **V1**：单实例、线性流水线、MySQL 持久化、SSE 续传（已完成，见 §12）
- **V2**：Redis Pub/Sub 替换进程内总线支持多实例；`run_event` 走 Redis Stream 作为回放源（已完成，见 §13）
- **V3**：Workflow 支持 DAG + 条件边（**已完成**，见 §15）；人工审批节点（checkpointer +
  interrupt）、Agent RAG / MCP 工具**顺延到 V3.1**
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

- **人工审批不在 V3**：`checkpointer` + `interrupt` 顺延到 V3.1。V3 仍然不挂 checkpointer，
  事件流仍由 `run_event` + Redis Stream 唯一负责（理由同 §14.3）。
- **汇聚节点不能混条件入边**：这是 LangGraph 的语义限制（等齐所有入边才触发），配置期
  明确拒绝并给出可执行的改法（把条件前移到分叉处）。
- **全条件汇聚被拒绝**：所有入边都是条件边时，任何一条不成立就永远等不到。等价写法是
  把汇聚点改成「无条件边 + 节点内部判断」。

---

*文档结束*
