# 多 Agent 协作 Web 服务

基于 **Python + FastAPI + MySQL** 的多 Agent 协作服务。Agent 与 Workflow 全部以数据形式存在 MySQL 里，**新增/调整 Agent 不需要改代码、不需要发版**；多个 Agent 按 Workflow 协作产出结果，前端通过 SSE 接收流式输出，**断线可续传**。

完整设计见 [`docs/design.md`](docs/design.md)。

---

## 核心特性

| 特性 | 说明 |
| --- | --- |
| 配置即数据 | Agent / Workflow 存 MySQL，运行期热加载，PUT 更新自动 `version+1` 让缓存失效 |
| 生成与连接解耦 | `POST /messages` 立即返回 `runId`，生成在后台 `asyncio.Task` 中跑，**连接断开不影响生成** |
| 断线续传 | 事件带单调递增 `seq` 并全量落库；重连用 `Last-Event-ID` 或 `last_seq` 回放，**不漏字、不重复、不乱序** |
| 批量落库 | `run_event` 每 20 条或每 50ms 批量 INSERT，绝不每 token 一次写库 |
| **多实例（V2）** | 开启 `REDIS_ENABLED` 后事件走 Redis Stream（回放源）+ Pub/Sub（实时），**任意实例都能接任意 run 的流** |
| **归属租约（V2）** | 生成方持带 TTL 的租约并续期；新实例启动只回收**无主**的 run，不会误杀别人正在跑的 |
| **跨实例取消（V2）** | 取消信号走控制频道，在 B 实例点「停止」能真的停掉 A 实例上的生成 |
| **LangGraph 编排（V2）** | 控制流交给 `StateGraph`，连线来自 `definition.edges`；**刻意不挂 checkpointer**，事件流唯一权威（见下） |
| 心跳保活 | SSE 注释行 `: ping`，不占 `seq`、不落库、不污染回放序列 |
| 统一协议 | 所有模型走 OpenAI 兼容 `/v1/chat/completions`，OpenAI / DeepSeek / Qwen / vLLM 通吃 |
| 可扩展 | 进程内总线抽象为 `BusBackend` Protocol，V2 已落地 Redis Pub/Sub 实现 |

> 多实例的完整设计（含一处被实测抓出来的漏字 bug）见 [`docs/design.md` §13](docs/design.md)。

### 为什么不挂 LangGraph checkpointer

装了 `langgraph-checkpoint-redis` 就能接，**但不接**。三行理由：

- **粒度差两个数量级**——实测一次三节点流水线产生 **445 条事件**，而 checkpointer 只存 4 份
  快照（每节点一份）。客户端在第 137 个 token 断线就能退回节点边界重放，前端打字机直接
  退化成「整段冒出来」。
- **存的东西不同**——`run_event` 是**追加的、带单调 seq 的事件流**；checkpointer 是
  **状态快照**，每个 super-step 覆盖上一份。快照不能当事件流读。
- **不提供互斥**——`BaseCheckpointSaver` 的契约里没有并发控制，归属租约仍得自己维护。

它真正擅长的是我们**还没有**的：人工审批（`interrupt()`）、time-travel、崩溃后按节点续跑。
留给 V3。

> 完整论证见 [`docs/design.md` §14.3](docs/design.md)。

---

## 快速开始

### 1. 安装

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

pip install -r requirements.txt
```

### 2. 配置

```bash
copy .env.example .env          # Windows
# cp .env.example .env          # macOS / Linux
```

生产环境**务必**改掉 `SECRET_KEY`（它用于加密 Agent 的 api_key 和签发 SSE ticket）：

```bash
python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"
```

数据库二选一：

```dotenv
# 生产：MySQL（推荐 asyncmy，Cython 实现，大批量查询更快）
DATABASE_URL=mysql+asyncmy://user:pass@127.0.0.1:3306/agentdb?charset=utf8mb4

# 本地开发：SQLite，零依赖直接跑
DATABASE_URL=sqlite+aiosqlite:///./web_agent.db

# 演示数据用的模型（scripts/seed.py 读取）
MINIMAX_BASE_URL=https://api.minimaxi.com/v1
MINIMAX_MODEL=MiniMax-M2.7
MINIMAX_API_KEY=sk-xxxxxxxx
```

### 3. 建表

```bash
alembic upgrade head
```

### 4. 灌入演示数据

```bash
python -m scripts.seed
```

会创建 `planner / researcher / writer` 三个 Agent 和 `default_pipeline` 三段式流水线，
默认指向 MiniMax（`https://api.minimaxi.com/v1` 的 `MiniMax-M2.7`）。

**换模型只改 `.env` 里的三个值，不用改任何代码**——所有 Agent 统一走 OpenAI 兼容
`/v1/chat/completions`：

```dotenv
# DeepSeek
MINIMAX_BASE_URL=https://api.deepseek.com/v1
MINIMAX_MODEL=deepseek-chat

# 通义千问
MINIMAX_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
MINIMAX_MODEL=qwen-max

# 本地 vLLM（无需鉴权时 key 可留空）
MINIMAX_BASE_URL=http://127.0.0.1:8000/v1
MINIMAX_MODEL=Qwen2.5-7B-Instruct
```

> ⚠️ **API Key 只放在 `.env` 里**（已在 `.gitignore` 中），不要写进代码或提交。
> `SECRET_KEY` 同样是：它决定 `agent.api_key` 能否被解密。
>
> `MiniMax-M2.7` 默认把思维链以 `<think>…</think>` 内联在 `content` 里返回，
> 因此前端会连思考过程一起显示。若供应商提供独立的 `reasoning_content`
> 字段（DeepSeek / Qwen 等），服务会自动拆成 `reasoning` 事件单独下发。

### 5. 启动

**最省事的方式**——一条命令，迁移、灌数据、启动全包了：

```bash
python run.py                # Windows 也可以直接双击 start.bat
```

`run.py` 会依次完成：切到项目根目录 → 依赖自检 → 缺 `.env` 就从 `.env.example`
生成一份 → `alembic upgrade head` → `scripts.seed` → 起 uvicorn，最后打印出可点击的地址。

常用参数：

```bash
python run.py --no-seed        # 跳过灌演示数据
python run.py --no-reload      # 关掉热重载，行为更接近生产
python run.py --port 9000      # 换端口
python run.py --open           # 启动成功后自动打开浏览器
python run.py --help           # 全部参数
```

- **功能验证台**：<http://127.0.0.1:8000/> ← 单文件页面，开箱即用
- Swagger UI：<http://127.0.0.1:8000/docs>
- 健康检查：<http://127.0.0.1:8000/api/health>

> 💡 **为什么推荐用 `run.py` 而不是直接敲 uvicorn**：`app/config/settings.py` 里
> `env_file=".env"` 是相对路径，`alembic.ini` 的 `script_location=alembic` 也是。
> 从别的目录启动时 `.env` 会**静默读不到**——不报错，只是 `minimax_api_key` 变空，
> 排查起来很费劲。`run.py` 强制 `chdir` 到项目根，从根上消除了这个坑，
> 并且保证「先迁移后 seed」的顺序（顺序反了会留下"表已建但 alembic_version 为空"
> 的半吊子状态，下次迁移才炸）。

不用启动器时，手工四步等价：

```bash
alembic upgrade head
python -m scripts.seed
uvicorn app.main:app --reload
```

### 6. 多实例（V2）

默认是**单实例**。要跑多台，在 `.env` 里打开 Redis：

```dotenv
REDIS_ENABLED=true
REDIS_URL=redis://192.168.1.5:6379/0
REDIS_KEY_PREFIX=webagent    # 多套环境共用一个 Redis 时做隔离
REDIS_STREAM_MAXLEN=20000    # 每个 run 的事件 Stream 保留条数上限
REDIS_ACTIVITY_TTL=120       # 归属租约 TTL（秒），生成方按 TTL/3 续期
REDIS_POLL_INTERVAL=1.0      # 订阅轮询间隔，决定退订收尾的快慢
```

然后把同一个服务起在多个端口 / 多台机器上：

```bash
python run.py --port 8000
python run.py --port 8100      # 另一台终端
```

此时：

- 在 **8000** 发消息，**8100** 上打开 <http://127.0.0.1:8100/> 也能收到完整流式输出；
- 断线后连到**任意**一个实例继续收，带上 `last_seq` 即可续传；
- 在任一实例点「取消」，生成方那个实例上会真的停下来；
- **新起的实例不会误杀正在生成的 run**——归属租约按 TTL/3 续期，
  启动恢复只回收「已经没人管」的 run。

> ⚠️ **开启后没有降级路径**：Redis 连不上时服务**直接启动失败**，不会静默退回
> 进程内总线。后者会造成「服务能跑、跨实例收不到事件」的分裂大脑，比启动失败
> 难查得多。单实例开发请保持 `REDIS_ENABLED=false`。

多个实例前面挂 Nginx 时仍然要关缓冲，并且 ticket 要每次重连重新签发：

```nginx
upstream web_agent {
    least_conn;
    server 127.0.0.1:8000;
    server 127.0.0.1:8100;
}
location /api/ {
    proxy_pass http://web_agent;
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 3600s;
    proxy_set_header Host $host;
}
```

---

## 功能验证台（`web/index.html`）

启动服务后直接访问根路径即可。它不是演示动画，而是一个能**把每个环节都点一遍**的验证台：

| 区块 | 能验证什么 |
| --- | --- |
| ① 会话 | 列出已启用的 Workflow、创建会话、**恢复进度**（会话级别恢复：拉历史 + 找 active-run + 重新订阅回放） |
| ② 已配置 Agent | 展示 MySQL 里的 Agent 配置，证明「配置即数据」：改库即生效，无需发版 |
| ③ 工作流 | 展示 definition 的节点链路 |
| ④ 对话与流式输出 | 按节点分卡片实时吐字；发送 / 取消 / 断开并重连 |
| ⑤ 事件日志 | 逐条列出 `event` / `id`(=seq) / `data` |

页面上有两个实时指标专门用来验证续传：

- **seq 连续性** —— 客户端每收到一个事件就自检 `seq` 是否单调且不重复，
  显示 `✓ 无重复/乱序`。若服务端有洞或重复，这里会立刻变红。
- **重连次数** —— 配合「自动断线测试」勾选框：吐到第 5 个字就**主动断开**，
  1.5 秒后带 `last_seq` 重连，最终仍能拿到完整内容。

> ⚠️ 验证台每次重连都会重新申请一张 ticket，而不是依赖浏览器自带的重连——
> 因为 EventSource 自动重连会复用旧 URL，60 秒后 ticket 过期就会一直 401。
> 这也是生产接入时容易踩的坑。

页面默认每 500ms 轮询一次 `GET /api/runs/{id}` 来确认服务端**最终落库状态**
（流里的 `done` 事件先于状态回写），所以控制台会比实际请求多一点。

---

## 跑通一次完整对话

```bash
BASE=http://127.0.0.1:8000

# 1. 建会话
CONV=$(curl -s -X POST $BASE/api/conversations \
  -H 'Content-Type: application/json' \
  -d '{"workflow_code":"default_pipeline","user_id":"u1"}' | jq -r .id)

# 2. 发消息 → 立刻拿到 runId，生成在后台跑
RUN=$(curl -s -X POST $BASE/api/conversations/$CONV/messages \
  -H 'Content-Type: application/json' \
  -d '{"content":"帮我规划一次技术分享"}' | jq -r .run_id)

# 3. 换一张 60s 有效的 SSE ticket
TICKET=$(curl -s -X POST $BASE/api/auth/sse-ticket \
  -H 'Content-Type: application/json' -d '{"user_id":"u1"}' | jq -r .ticket)

# 4. 订阅事件流
curl -N "$BASE/api/runs/$RUN/stream?ticket=$TICKET&last_seq=0"
```

输出形如：

```text
id: 1
event: node_start
data: {"node":"planner","agentCode":"planner","model":"gpt-4o-mini"}

id: 2
event: token
data: {"node":"planner","content":"好的"}

: ping

id: 3
event: node_end
data: {"node":"planner","outputKey":"plan","agentCode":"planner"}

id: 9
event: done
data: {"status":"done","usage":{"prompt_tokens":120,"completion_tokens":340}}
```

### 断线续传

前端记住收到的最后一个 `id`，重连时带上即可无缝接上：

```bash
curl -N "$BASE/api/runs/$RUN/stream?ticket=$TICKET&last_seq=3"   # 从 seq 4 继续
curl -N -H "Last-Event-ID: 3" "$BASE/api/runs/$RUN/stream?ticket=$TICKET"  # 等价
```

---

## 事件协议

| 事件 | 含义 | 占 seq |
| --- | --- | --- |
| `node_start` | 节点开始 | 是 |
| `token` | 文本增量 | 是 |
| `reasoning` | 思维链增量（DeepSeek / Qwen） | 是 |
| `tool_call` | 工具调用请求 | 是 |
| `node_end` | 节点结束 | 是 |
| `error` | 出错 | 是 |
| `done` | 整个 run 结束，流关闭 | 是 |
| `: ping` | SSE 注释行心跳 | **否** |

> `id:` 字段就是 `seq`，也是续传锚点。心跳用注释行，因此不会污染序列。

---

## 恢复策略

| 场景 | 行为 |
| --- | --- |
| 页面刷新 / 隔天回来 | `GET /conversations/{id}/messages` 拉历史 → `GET /conversations/{id}/active-run` 拿 runId → 带 `last_seq=0` 重连，服务端全量回放 |
| 网络抖动 | 浏览器自动带 `Last-Event-ID` 重连，服务端从该 seq 续传 |
| run 已结束 | 回放剩余事件；事件已归档则补一个合成 `done`，前端转去读最终消息 |
| 事件已清理（>7 天） | 返回 **410**，响应里带 `messagesUrl`，前端据此拉最终内容 |
| 进程重启 | 启动时扫描遗留 `running` run 标为 `error`，提示用户重发 |

---

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET/POST | `/api/agents` | 列出 / 创建 Agent |
| GET/PUT/DELETE | `/api/agents/{code}` | 详情 / 更新（version+1）/ 删除 |
| GET/POST | `/api/workflows` | 列出 / 创建 Workflow |
| GET/PUT/DELETE | `/api/workflows/{code}` | 详情 / 更新 / 删除 |
| GET/POST | `/api/conversations` | 列出 / 创建会话 |
| GET | `/api/conversations/{id}/messages` | 会话历史 |
| GET | `/api/conversations/{id}/active-run` | 进行中的 run，无则返回 `null` |
| POST | `/api/conversations/{id}/messages` | 发消息，返回 `{runId, streamUrl}` |
| GET | `/api/runs/{id}` | run 详情（含各节点执行记录） |
| GET | `/api/runs/{id}/messages` | run 的最终消息 |
| GET | `/api/runs/{id}/stream` | SSE 事件流 |
| POST | `/api/runs/{id}/cancel` | 取消生成 |
| POST | `/api/auth/sse-ticket` | 换取 60s 有效 SSE ticket |
| GET | `/api/health` | 健康检查 |

错误码：`400` 参数非法 · `401` ticket 无效/过期 · `404` 资源不存在 · `409` 该会话已有进行中的 run · `410` 事件已归档无法回放

---

## 前端接入

```ts
let lastSeq = Number(localStorage.getItem(`run:${runId}:seq`) || 0);

function connect() {
  const es = new EventSource(`/api/runs/${runId}/stream?last_seq=${lastSeq}&ticket=${ticket}`);

  es.addEventListener('token', e => {
    appendText(JSON.parse(e.data).content);
    lastSeq = Number(e.lastEventId);
    localStorage.setItem(`run:${runId}:seq`, String(lastSeq));
  });

  es.addEventListener('done', () => {
    es.close();
    localStorage.removeItem(`run:${runId}:seq`);
  });

  es.onerror = () => {
    es.close();
    setTimeout(connect, 1000);   // 带上 last_seq，服务端补齐
  };
}
```

**Nginx 需要关掉缓冲**，否则吐字会被攒住：

```nginx
location /api/ {
    proxy_pass http://127.0.0.1:8000;
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 3600s;
}
```

---

## 测试

```bash
pytest -q
```

**102 个用例**全绿，其中 19 个是需要真实 Redis 的集成测试（`tests/test_redis.py`；
Redis 连不上时这 19 个会自动 skip，其余照跑），9 个是图构建测试（`tests/test_run_graph.py`，
含「执行顺序按 edges 而非 nodes 声明顺序」）。覆盖设计文档里最容易写错的两处：

- `run_event` 批量落库不丢事件、seq 严格递增唯一、并发 emit 不产生重复 seq
- 「先订阅 → 冲刷 → 回放 → 按 seq 合并」在断线重连时不漏字、不重复、不乱序
- **跨实例续传**：A 实例生成、B 实例收流，B 侧 `hub.flush()` 是空操作也能收全
- **跨实例取消**：B 实例点取消，A 实例上的生成收到信号并停止
- **归属租约**：启动恢复只回收无主的 run，不误杀其它实例正在跑的

端到端用例会起一个**真实的 OpenAI 兼容 HTTP 服务**当假模型（`tests/fake_llm.py`），
走完整网络栈验证 SSE 吐字、取消、重试、断线续传。另有双实例端到端（两个独立
uvicorn 进程 + 真实 Redis）验证跨实例吐字与续传。

> 测试库按进程号隔离（`%TEMP%\web_agent_test_<pid>.db`），所以**可以并行跑多份
> pytest 而不会互相抢库**。早先固定文件名时，并行跑会出现毫无关联的用例随机失败。
>
> `conftest.py` 会强制 `REDIS_ENABLED=false`——`settings` 的 `env_file=".env"` 是
> 相对路径，pytest 从项目根跑会命中开发者本地的 `.env`，开了 Redis 会让整个套件
> 切到 Redis 模式、用例之间互相撞车。需要 Redis 的用例自己显式建连。

### docstring 覆盖检查

```bash
python scripts/check_docstrings.py            # 全量
python scripts/check_docstrings.py app/core   # 指定目录/文件/模块路径
```

当前状态：**449 个函数，全部带 docstring**。新写函数时请一并补上，并跑一次这个检查。
默认扫描范围含根目录的 `run.py`，新增根目录脚本记得加进 `DEFAULT_TARGETS`。

---

## 项目结构

```text
run.py                     # 本地启动器：迁移 + 灌数据 + 起服务（见上文第 5 步）
start.bat                  # Windows 双击启动（= python run.py --open）
app/
├── main.py                 # FastAPI 工厂 + 生命周期 + 统一异常处理
├── api/
│   ├── deps.py             # get_db / ticket 校验 / 断点解析
│   └── routes/             # agent / workflow / conversation / run / auth / health
├── core/
│   ├── bus.py              # BusBackend Protocol + InMemory / Redis Pub-Sub 两种实现
│   ├── event_log.py        # EventLog Protocol + Db / Redis Stream 两种回放源
│   ├── lease.py            # RunLease Protocol + Noop / Redis 两种归属租约与取消频道
│   ├── redis_client.py     # Redis 连接生命周期 + 实例标识 + 键布局（V2）
│   ├── run_hub.py          # seq 分配 + 事件广播 + 取消信号
│   ├── event_writer.py     # run_event 批量落库（MySQL 长期归档）
│   ├── sse_writer.py       # 回放 + 实时合并去重（续传核心）
│   ├── agent_runner.py     # 单 Agent 执行（AsyncOpenAI 流式）
│   ├── run_graph.py        # LangGraph 图构建 + NodeRunner（V2-b）
│   ├── orchestrator.py     # run 生命周期 + 节点调度 + 重试
│   ├── workflow_def.py     # definition 解析与校验
│   ├── crypto.py           # api_key 加解密
│   └── ticket.py           # SSE ticket 签发与校验
├── models/  repo/  schemas/  config/  cache.py
web/index.html           # 功能验证台（单文件，无依赖）
scripts/seed.py          # 演示数据
scripts/check_docstrings.py  # docstring 覆盖检查
alembic/  tests/  docs/design.md
```

---

## 演进路线

- **V1**（已完成）：单实例、线性流水线、MySQL 持久化、SSE 续传
- **V2**（已完成）：Redis Pub/Sub 替换进程内总线支持多实例；`run_event` 走 Redis Stream 作为回放源；归属租约让启动恢复不再误杀其它实例的 run
- **V2-b**（已完成）：控制流改由 LangGraph `StateGraph` 表达，连线来自 `definition.edges`，为 V3 的 DAG 留好扩展点（不挂 checkpointer，理由见上文）
- **V3**：Workflow 支持 DAG + 条件边 + 人工审批（`interrupt()` + checkpointer，届时再引入）；Agent 支持 RAG / MCP 工具
- **V4**：可观测性（OpenTelemetry 链路追踪、token 成本统计）
