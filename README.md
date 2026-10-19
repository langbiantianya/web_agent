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
| **DAG + 条件边（V3）** | Workflow 不再是一条链：无条件出边**并行分叉**、多条入边**汇聚**；带 `when` 的边按表达式**分支**。环、孤立节点、汇聚形状等**全部在保存时拒绝** |
| **条件表达式（V3）** | 手写递归下降解析器（**不用 `eval`**，杜绝 RCE），支持 `== != > < >= <=`、`and/or/not`、括号与白名单函数 `len/contains/lower/is_empty` |
| **人工审批（V3.1）** | 节点可声明 `type: "approval"`：流程跑到它就**挂起等人拍板**，批准则从暂停点继续（前面的节点不重跑、不重花 token），驳回则本次 run 终止 |
| **工具调用闭环（V3.2）** | Agent 可挂内置工具（`now` / `calc`）：模型要工具 → 服务端执行 → 结果回灌 → 模型继续答。工具**只能来自代码内置注册表**，配置里写的是工具名，未注册的名字**保存时就被拒** |
| 心跳保活 | SSE 注释行 `: ping`，不占 `seq`、不落库、不污染回放序列 |
| 统一协议 | 所有模型走 OpenAI 兼容 `/v1/chat/completions`，OpenAI / DeepSeek / Qwen / vLLM 通吃 |
| 可扩展 | 进程内总线抽象为 `BusBackend` Protocol，V2 已落地 Redis Pub/Sub 实现 |

> 多实例的完整设计（含一处被实测抓出来的漏字 bug）见 [`docs/design.md` §13](docs/design.md)。

### 事件流不挂 LangGraph checkpointer

装了 `langgraph-checkpoint-redis` 就能接，但**事件流这条路上不接**。三行理由：

- **粒度差两个数量级**——实测一次三节点流水线产生 **445 条事件**，而 checkpointer 只存 4 份
  快照（每节点一份）。客户端在第 137 个 token 断线就能退回节点边界重放，前端打字机直接
  退化成「整段冒出来」。
- **存的东西不同**——`run_event` 是**追加的、带单调 seq 的事件流**；checkpointer 是
  **状态快照**，每个 super-step 覆盖上一份。快照不能当事件流读。
- **不提供互斥**——`BaseCheckpointSaver` 的契约里没有并发控制，归属租约仍得自己维护。

> **V3.1 更新**：checkpointer 现在**挂上了**，但服务的是另一个消费者——**「图停在哪、恢复时
> 从那儿继续」**（人工审批的暂停点）。上面三条一条没变：事件流仍然只由 `run_event` +
> Redis Stream 负责，checkpointer 不碰它。两个消费者、两份数据，不重叠。
> 见 [`docs/design.md` §16.3](docs/design.md)。

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

## 后台管理（`web/admin.html`）

启动后访问 **`/admin`**。同样零依赖单文件、同源提供，只调用既有 REST 接口，
**不新增任何服务端能力**——所有增删改走的就是 `/api/agents`、`/api/workflows` 那两套接口。

| 标签页 | 能做什么 |
| --- | --- |
| **Agent 配置** | 增删改查 Agent：system_prompt / model / base_url / api_key / temperature / max_tokens / 启用开关 / **工具勾选**（V3.2，选项来自服务端注册表）与 extra_params |
| **Workflow 编排** | 增删改查 Workflow。**节点用表单编排**（id、选 Agent、outputKey），**连线显式添加**（from → to → 可选 `when` 条件），实时预览将要提交的 definition JSON |
| **会话观测** | 按 `user_id` 查会话，看消息历史与进行中的 run（run_id / 状态 / 当前节点 / last_seq） |

几个刻意的设计：

- **api_key 不回填、不回显**。列表接口只给 `has_api_key` 布尔值，编辑时该框留空表示
  「不修改」——直接带上空值等于把已存的 key 抹掉。
- **连线改成显式添加**。V1/V2 时代页面按节点顺序自动成链，理由是「手工连边十有八九会
  连成 DAG 或成环」。V3 支持 DAG 之后这个理由不成立了——分叉、汇聚、条件边**根本没法
  用一条链表达**，于是改成显式连线：一条边一行，可填 `when`。
- **条件表达式有语法提示**。输入 `when` 时页面会提示可用的 `upstream.<outputKey>` 与
  保留变量 `user_input`；写错在保存时就会被服务端拒绝并说明原因。
- **首次进入自动选中第一项**，右侧不会是一片「请选择」。
- **事件一律 `addEventListener` + 委托**，不用 inline `onclick`，与验证台一致，
  将来加 CSP 也不会整片失效。
- 会话列表接口按 `user_id` 过滤（不传即 `anonymous`），所以页面上给了 `user_id` 输入框——
  演示数据的会话属于 `demo`，不切用户就只会看到空列表。

---

## Workflow 定义格式（V3：DAG + 条件边）

Workflow 的 `definition` 是一张图。`nodes` 声明节点，`edges` 声明连线，`entry` 指定入口
（不写就取第一个节点）。

```json
{
  "entry": "root",
  "nodes": [
    {"id": "root",  "agentCode": "planner",   "outputKey": "plan"},
    {"id": "left",  "agentCode": "researcher","outputKey": "left_out"},
    {"id": "right", "agentCode": "researcher","outputKey": "right_out"},
    {"id": "join",  "agentCode": "writer",    "outputKey": "final"}
  ],
  "edges": [
    {"from": "root", "to": "left"},
    {"from": "root", "to": "right"},
    {"from": "left", "to": "join"},
    {"from": "right", "to": "join"}
  ],
  "inputTemplate": "问：{{user_input}}\n计划：{{plan}}\n左：{{left_out}}\n右：{{right_out}}"
}
```

上面这张图的意思是：**root 之后 left 与 right 并行跑**，两条都结束后 **join 才开始**。
事件流里能直接看出这个结构——两个分支的 `node_start` 连着出现，token 交错，
`join` 的 `node_start` 排在两个分支的 `node_end` 之后。

### 边的两种形态

| 边 | 含义 |
| --- | --- |
| 不带 `when` | 无条件走。同一节点的**多条**无条件出边 = 并行分叉；**多条**无条件入边 = 汇聚 |
| 带 `when` | 表达式成立才走。同一节点的**多条**条件出边 = 分支（互斥选择） |

```json
{"from": "root", "to": "deep",  "when": "len(upstream.plan) > 0"},
{"from": "root", "to": "quick", "when": "false"}
```

### 条件表达式

手写递归下降解析器（`app/core/expr.py`），**不用 `eval`**——条件来自用户配置，
`eval` 等于把 RCE 暴露给任何能编辑工作流的人。

| 类别 | 支持 |
| --- | --- |
| 比较 | `==` `!=` `>` `<` `>=` `<=` |
| 逻辑 | `and` `or` `not`，可加括号 |
| 字面量 | 字符串、数字、`true` / `false` / `null` |
| 变量 | `user_input`、`upstream.<outputKey>` |
| 函数 | `len()` `contains()` `lower()` `is_empty()` |

**只认两种变量写法**：`user_input` 与 `upstream.<outputKey>`。裸写 `plan` 会在保存时被
拒绝并提示 `upstream.plan`——因为求值期的作用域只有这两个根，裸名会**静默求值成
`None`**，条件恒假、走错分支还不报错。

### 模板变量

`inputTemplate` 支持三类占位符：

| 占位符 | 内容 |
| --- | --- |
| `{{user_input}}` | 本次用户输入 |
| `{{upstream}}` | 全部上游产出的 Markdown 摘要（按 outputKey 分块） |
| `{{outputKey}}` | **某个**上游产出，例如 `{{left_out}}` |

DAG 下逐个引用是刚需：并行分支同时产出多份内容，用 `{{upstream}}` 一把全塞进 prompt，
模型分不清哪段对应哪个分支。未知变量在保存时就会被拒绝。

### 保存时会被拒绝的配置

能在运行前静态判定的问题，一律挡在保存时——跑一半才发现编排不了，token 和钱已经花出去了：

| 拒绝 | 原因 |
| --- | --- |
| 存在环 | 执行顺序无法确定 |
| 有从 entry 走不到的节点 | 死配置：保存不报错，但那个节点永远不跑 |
| 汇聚节点的入边里混了条件边 | LangGraph 会等齐所有入边才触发，条件跳过一条就永远等不到 |
| 同一节点的出边条件与无条件混用 | 语义不明确 |
| `when` 语法错误 / 引用了不存在的 outputKey | 运行期会安静求值成 `None`，**走错分支且不报错** |
| `inputTemplate` 引用了不存在的变量 | 会把 `{{xxx}}` 原样发给模型看 |
| 审批节点没写 `prompt` | 审批人看到的是一张白纸 |
| 审批节点又写了 `agentCode` | 两套语义混在一个节点上，说不清谁生效 |

### 人工审批节点（V3.1）

节点多了一个 `type` 字段，省略即 `agent`——**老 definition 一行都不用改**。

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

流程跑到 `gate` 就**挂起**：run 状态变成 `waiting`，事件流里出现一条 `approval_required`，
SSE 连接**不关**（run 还没结束）。人给了决策之后：

| 决策 | 结果 |
| --- | --- |
| 批准 | 从暂停点**继续**往下跑。`plan` **不重跑**、不重花 token；审批意见进 `upstream.gate_out` 与上下文 |
| 驳回 | 本次 run 就此终止（`status=rejected`），驳回意见写进 `error_msg`，下游节点不跑 |

```bash
# 查当前待审批项（没有就返回 null）
curl -s $BASE/api/runs/$RUN_ID/approval

# 提交决策
curl -s -X POST $BASE/api/runs/$RUN_ID/approval \
  -H 'Content-Type: application/json' \
  -d '{"approved": true, "comment": "可以执行", "decidedBy": "u1"}'
```

> 驳回不是「跳过这个节点继续跑」：人已经说了「不行」，让它继续跑会产出一个基于错误前提的结果。
> 终止 + 记下原因，让发起人看到「这次没成，原因是 X」。

后台管理页（`/admin`）的节点编辑区有「节点类型」下拉，切到「人工审批」就会出现审批提示输入框；
验证台（`/`）收到 `approval_required` 会亮出审批卡片，可直接批准/驳回。

### Agent 工具调用（V3.2）

Agent 的 `tools` 字段挂的是**内置工具名**：

```json
{"code": "analyst", "tools": ["calc", "now"], "...": "..."}
```

**工具名即契约**：`tools` 里写的是名字，schema（模型看到的描述）与执行器（服务端实际
做的事）都从**同一张注册表**取，所以两者不可能对不上。未注册的名字**保存时就拒**（400），
配置根本进不了库。

> 为什么不让人直接写 OpenAI 的 function schema？因为那等于**让配置决定执行逻辑**——
> 工具参数直接来自模型，而模型会被提示词注入影响。本项目已有同样的先例：条件表达式不用
> `eval`。想加新工具就在 `app/core/tools.py` 的注册表里加一条，不用改配置格式、不用改前端。

内置工具：

| 工具 | 作用 | 参数 |
| --- | --- | --- |
| `now` | 当前时间（UTC + 本地） | 无 |
| `calc` | 算术求值（`+ - * / // % **` 与括号） | `expression` |

`calc` 的求值走 `ast` 解析 + **节点白名单**遍历（`app/core/arith.py`），**不用 `eval`**；
并有长度、AST 节点数、指数、结果量级四道上限——`2 ** 10000000` 这种表达式不能把 CPU 吃光。

闭环的形状是：**模型要工具 → 服务端执行 → 结果以 `role=tool` 回灌 → 模型继续答**。

| 情况 | 行为 |
| --- | --- |
| 工具算错（参数不合法、表达式非法） | **不失败节点**，错误文本回灌给模型，让它有机会自己纠正；`tool_result` 事件里 `ok=false` |
| 模型一直调同一个必失败的工具 | 轮次上限（`TOOL_MAX_ROUNDS`，默认 5）收口：run 以 `error` 结束，**不重试**（重试大概率同样陷住，白烧 token） |
| 工具结果太长 | 截断到 `TOOL_RESULT_MAX_CHARS`（默认 4000）再回灌，避免挤爆下一轮 prompt |

```bash
# 看当前部署有哪些工具可用
curl -s $BASE/api/tools
```

后台管理页（`/admin`）的 Agent 编辑区是**工具勾选框**，选项来自 `/api/tools`；验证台（`/`）
会把工具调用与结果插在节点的输出流里（`⚙ 调用 calc` / `↩ 返回 60`）。

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
| `tool_result` | 工具执行结果（V3.2）：调了哪个工具、成没成、结果摘要 | 是 |
| `node_end` | 节点结束 | 是 |
| `error` | 出错 | 是 |
| `approval_required` | 流程挂起，等人拍板（V3.1） | 是 |
| `approved` / `rejected` | 审批结果（V3.1） | 是 |
| `done` | 整个 run 结束，流关闭 | 是 |
| `: ping` | SSE 注释行心跳 | **否** |

> `id:` 字段就是 `seq`，也是续传锚点。心跳用注释行，因此不会污染序列。
>
> **只有 `done` 是终止事件。** 流程挂在审批节点上时**不发** `done`，SSE 连接继续开着，
> 批准后同一条流接着吐字。

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
| GET | `/api/tools` | 可用的内置工具目录（V3.2） |
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
| GET | `/api/runs/{id}/approval` | 当前待审批项，无则 `null`（V3.1） |
| POST | `/api/runs/{id}/approval` | 提交审批决策，批准则继续、驳回则终止（V3.1） |
| POST | `/api/auth/sse-ticket` | 换取 60s 有效 SSE ticket |
| GET | `/api/health` | 健康检查 |

错误码：`400` 参数非法 · `401` ticket 无效/过期 · `404` 资源不存在 · `409` 该会话已有进行中的 run，或审批冲突 · `410` 事件已归档无法回放

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

**171 个用例**全绿，其中 19 个是需要真实 Redis 的集成测试（`tests/test_redis.py`；
Redis 连不上时这 19 个会自动 skip，其余照跑），6 个是 Redis 降级测试
（`tests/test_redis_degradation.py`，用假件模拟抖动，不需要真实 Redis），
15 个是图构建与执行测试（`tests/test_run_graph.py`，含「执行顺序按 edges 而非 nodes
声明顺序」、并行分支的 reducer 合并、条件边的分支选择）。覆盖设计文档里最容易写错的
几处：

- `run_event` 批量落库不丢事件、seq 严格递增唯一、并发 emit 不产生重复 seq
- 「先订阅 → 冲刷 → 回放 → 按 seq 合并」在断线重连时不漏字、不重复、不乱序
- **跨实例续传**：A 实例生成、B 实例收流，B 侧 `hub.flush()` 是空操作也能收全
- **跨实例取消**：B 实例点取消，A 实例上的生成收到信号并停止
- **归属租约**：启动恢复只回收无主的 run，不误杀其它实例正在跑的
- **Redis 抖动降级**：租约拿不到、Stream 写不进去、广播发不出去时，**run 不能夭折**
- **并发下的数据库**：SSE 流不占着 DB 事务、32 个并发「先读后写」的事务不撞
  `database is locked`（`tests/test_db_concurrency.py`）

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

当前状态：**690 个函数，全部带 docstring**。新写函数时请一并补上，并跑一次这个检查。
默认扫描范围含根目录的 `run.py`，新增根目录脚本记得加进 `DEFAULT_TARGETS`。

---

## 容量与性能

完整的实测报告见 [`docs/performance.md`](docs/performance.md)，压测脚本是
`scripts/bench.py`。

**4c4g 单实例能带多少并发，取决于三件事，本服务只占其中最小的那一项：**

| 口径 | 4c4g | 依据 |
| --- | --- | --- |
| CPU | 2000–3750 字/s ≈ **40–75 人**（每用户 50 字/s） | 实测每字（≈1 个 model token）耗 0.8–1.5 ms CPU，4 核留 25% 给系统 |
| 内存 | 约 **9700 条**并发流 | 实测基线 155 MB + 每条流约 0.3 MB（保守） |
| 数据库 | SQLite 约 **64 条**；MySQL 不限 | 实测 64 并发 0 失败，128 并发 28–30 个 500 |
| 模型配额 | 取决于供应商 | 一条流至少要一个模型请求——**这通常是真正的天花板** |

中文分词器 1 token ≈ 1～1.5 字，按 50 **model** token/s 的用户折算，
4c4g 实际是 **30～45 人**。

**规划建议**：4c4g + MySQL，按 **30 个并发生成用户**起步、**50 个**做预案；
再往上加实例（本服务无状态，V2 的 Redis 架构支持多实例，扩容是线性的）。
**用 SQLite 的话并发上限被钉在 64**——生产请用 MySQL。

**服务端不是 CPU 密集型**：64 条并发流也只用掉不到 1 核，它绝大部分时间在等模型。

> 压测客户端、假模型、服务端同机抢 4 核，实测的服务端 CPU 占用是**下界**；
> MySQL 路径**未实测**（本机没有凭据）。完整的偏差清单见 `docs/performance.md` §8。

```powershell
# 压测（会建独立的临时库，不碰开发库）
python scripts/bench.py --label saturated --chunk-delay 0
python scripts/bench.py --label realistic --chunk-delay 0.02
```

### 压测带出来的两个真问题（已修）

1. **SQLite 并发写直接 500**（`database is locked`）。修法是 WAL + busy_timeout
   （`app/config/db.py`）。**试过 `BEGIN IMMEDIATE` 并放弃**——它把「报错」变成
   了「死锁」。SQLite 只适合开发。
2. **SSE 流一直占着 DB 连接**。FastAPI 的 yield 依赖在响应发完之后才关闭，
   而 SSE 响应一直开着——每条开着的流都占住连接池里的一个连接。MySQL 默认池
   `pool_size=10 + max_overflow=20`，也就是**整个服务最多 30 条并发流**。
   修法：SSE 路由取完数据后**在返回流之前就 `await db.rollback()`**。

---

## 项目结构

```text
run.py                     # 本地启动器：迁移 + 灌数据 + 起服务（见上文第 5 步）
start.bat                  # Windows 双击启动（= python run.py --open）
app/
├── main.py                 # FastAPI 工厂 + 生命周期 + 统一异常处理
├── api/
│   ├── deps.py             # get_db / ticket 校验 / 断点解析
│   └── routes/             # agent / workflow / conversation / run / auth / health / tool
├── core/
│   ├── bus.py              # BusBackend Protocol + InMemory / Redis Pub-Sub 两种实现
│   ├── event_log.py        # EventLog Protocol + Db / Redis Stream 两种回放源
│   ├── expr.py             # 条件表达式解析与求值（手写递归下降，不用 eval，V3）
│   ├── tools.py            # 内置工具注册表：工具名即契约（now / calc，V3.2）
│   ├── arith.py            # 算术求值（ast 白名单遍历，不用 eval，V3.2）
│   ├── lease.py            # RunLease Protocol + Noop / Redis 两种归属租约与取消频道
│   ├── redis_client.py     # Redis 连接生命周期 + 实例标识 + 键布局（V2）
│   ├── run_hub.py          # seq 分配 + 事件广播 + 取消信号
│   ├── event_writer.py     # run_event 批量落库（MySQL 长期归档）
│   ├── sse_writer.py       # 回放 + 实时合并去重（续传核心）
│   ├── agent_runner.py     # 单 Agent 执行（AsyncOpenAI 流式 + 工具闭环 V3.2）
│   ├── run_graph.py        # LangGraph 图构建 + NodeRunner + 审批节点（V2-b / V3.1）
│   ├── orchestrator.py     # run 生命周期 + 节点调度 + 重试 + 审批挂起/恢复（V3.1）
│   ├── checkpoint.py       # LangGraph checkpointer：审批暂停点（Redis / SQLite 双实现，V3.1）
│   ├── workflow_def.py     # definition 解析与校验
│   ├── crypto.py           # api_key 加解密
│   └── ticket.py           # SSE ticket 签发与校验
├── models/  repo/  schemas/  config/  cache.py
│   └── run_approval.py     # 审批记录模型（V3.1）
web/index.html           # 功能验证台（单文件，无依赖）
web/admin.html           # 后台管理（单文件，无依赖，访问 /admin）
scripts/seed.py          # 演示数据
scripts/bench.py         # 并发压测：起真实 uvicorn + 假模型，量 CPU/内存/延迟
scripts/check_docstrings.py  # docstring 覆盖检查
alembic/  tests/  docs/design.md  docs/performance.md
```

---

## 演进路线

- **V1**（已完成）：单实例、线性流水线、MySQL 持久化、SSE 续传
- **V2**（已完成）：Redis Pub/Sub 替换进程内总线支持多实例；`run_event` 走 Redis Stream 作为回放源；归属租约让启动恢复不再误杀其它实例的 run
- **V2-b**（已完成）：控制流改由 LangGraph `StateGraph` 表达，连线来自 `definition.edges`，为 V3 的 DAG 留好扩展点（事件流不挂 checkpointer，理由见上文）
- **V3**（已完成）：Workflow 支持 DAG + 条件边
- **V3.1**（已完成）：人工审批节点（`interrupt()` + checkpointer 暂停点，批准继续、驳回终止）
- **V3.2**（已完成）：Agent 工具调用闭环（内置工具注册表 + 执行 → 回灌 → 继续；工具名即契约，配置期校验）
- **V4**：MCP 工具（把 V3.2 的工具来源从「代码内置」换成「MCP 服务器」，闭环本身不改）；可观测性（OpenTelemetry 链路追踪、token 成本统计）
