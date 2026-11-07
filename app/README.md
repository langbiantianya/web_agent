# web_agent 主服务

`app/` 是面向用户和运维的主服务模块，负责配置管理、会话、Workflow 编排、Agent 调用、事件持久化和 SSE 订阅。Agent 与 Workflow 都以数据形式保存在数据库中，修改配置不需要重新发布服务。

## 架构总览

```mermaid
flowchart TB
    Client["浏览器 / API 客户端<br/>REST + EventSource（last_seq 续传）"]
    IDP["外部 OIDC IdP<br/>Authorization · Token · JWKS"]

    subgraph Access["接入层"]
        API["FastAPI REST API<br/>Agent · Workflow · Conversation · Run · Skill · MCP"]
        AUTH["User OIDC + Server Session<br/>PKCE · Cookie · CSRF"]
        ADMIN["Admin / Tenant / Role<br/>环境引导 Key + 数据库管理员"]
        TICKET["SSE Ticket<br/>user + tenant + run 绑定"]
    end

    subgraph Runtime["web_agent 运行时（可水平扩展）"]
        ORCH["Orchestrator<br/>run 生命周期 · 调度 · 重试 · 审批恢复"]
        GRAPH["LangGraph StateGraph<br/>DAG · 条件边 · interrupt"]
        RUNNER["AgentRunner<br/>模型流式调用 + 工具调用闭环"]
        POLICY["Tool Policy<br/>启用 · 并发 · 超时"]
        TOOLS["工具分发<br/>内置工具 · Skill · MCP"]
        AUDIT["Tool Audit Writer<br/>脱敏 · 限长 · 保留期"]
        APPROVAL["Approval Monitor<br/>会签 · 去重投票 · 超时"]
        SKILL["Skill Runtime<br/>提示词注入 · 资源读取"]
        MCP["MCP Client Manager<br/>Tools · Resources · Prompts"]
        HUB["RunHub<br/>seq 分配 · 广播 · 取消"]
        EW["EventWriter<br/>批量写入事件"]
        SSE["SSE Writer<br/>订阅 + 回放合并去重"]
        CKPT["Checkpointer<br/>审批暂停点"]
        TRACE["Trace Context<br/>W3C traceparent"]
    end

    subgraph Persistence["状态与持久化"]
        DB[("业务数据库<br/>MySQL / PostgreSQL / SQLite<br/>身份 · 配置 · Run · Event · Audit")]
        REDIS[("Redis（可选）<br/>Pub/Sub · Stream · Lease")]
        CP[("Checkpoint<br/>SQLite / Redis")]
    end

    LLM["LLM 服务<br/>OpenAI 兼容接口"]
    MCP_SERVER["外部 MCP Server"]

    subgraph ScriptExec["脚本执行边界"]
        STUDIO["studio<br/>gRPC · 无凭据 · 禁出网"]
        DISPATCH["studio-dispatcher<br/>只调度，不执行脚本正文"]
        SANDBOX["一次性 sandbox 容器<br/>isolated 档"]
    end

    Client -->|"REST"| API
    Client -->|"OIDC 登录"| AUTH
    AUTH -->|"discovery / token / JWKS"| IDP
    Client -->|"SSE"| TICKET
    TICKET --> SSE
    Client -->|"X-Admin-Key"| ADMIN
    AUTH --> API
    ADMIN --> API
    API -->|"创建 run / 后台执行"| ORCH
    Client -.->|"traceparent"| TRACE
    TRACE -.-> RUNNER
    TRACE -.-> STUDIO

    ORCH --> GRAPH
    ORCH --> APPROVAL
    APPROVAL --> CKPT
    GRAPH --> RUNNER
    RUNNER -->|"流式请求"| LLM
    RUNNER --> POLICY
    POLICY --> TOOLS
    RUNNER --> AUDIT
    AUDIT --> DB
    TOOLS --> SKILL
    TOOLS --> MCP
    MCP -->|"tools/resources/prompts"| MCP_SERVER
    TOOLS -->|"skill_exec · gRPC"| STUDIO
    STUDIO -->|"内部 HTTP"| DISPATCH
    DISPATCH -->|"创建一次性容器"| SANDBOX

    ORCH --> HUB
    GRAPH --> HUB
    RUNNER --> HUB
    HUB --> EW
    HUB --> SSE
    HUB <-->|"广播 / 控制信号"| REDIS
    SSE -->|"断线回放"| REDIS
    SSE -->|"事件兜底回放"| DB
    EW --> DB

    ORCH --> CKPT
    CKPT --> CP
    API --> DB
    ORCH --> DB
    SKILL --> DB
    AUTH --> DB
    APPROVAL --> DB
    MCP --> DB
```

业务数据默认落在数据库中；开启 Redis 后，多实例之间通过 Pub/Sub 分发实时事件、用 Stream 回放事件，并以租约协调 run 归属。模型生成的技能脚本只进入独立的 studio 执行边界，`web_agent` 主进程不直接执行脚本。

## 治理与运行期

- `ADMIN_KEY` 只负责环境引导；日常管理员使用数据库账号、`viewer/operator/admin`
  角色和租户隔离，Key 只存 HMAC 摘要并支持一次性轮换。
- Skill 使用草稿、发布、归档和恢复状态；修订资源保存原始字节，二进制也可逐字节回滚。
- 审批支持会签、同一审批人去重、截止时间和 `reject/approve` 超时策略，默认 fail-closed。
- Agent 可配置工具级启用、并发和超时；禁用工具不会进入模型工具清单，伪造调用会被拦截。
- MCP 同时支持 tools、resources、resource templates 和 prompts，并处理工具目录变更通知。
- 远程 MCP 支持 OAuth client credentials、加密 token 缓存、自动刷新与撤销。
- W3C `traceparent` 从 HTTP 入口贯通到工具、gRPC、dispatcher 和 sandbox。
- dispatcher 可取消并强删运行中的一次性容器；生产默认要求镜像固定 digest。
- dispatcher 默认加载自定义 seccomp；共享 dispatcher 对多个 studio 的租户/技能并发与
  活动预算统一准入。
- sandbox 从 cgroup 采集真实 CPU 秒与内存峰值，studio 可按部署费率导出估算成本。
- PostgreSQL 支持全量/增量物理备份、独立 WAL 归档和隔离时间点恢复，演练步骤见
  [`deploy/PITR.md`](../deploy/PITR.md)。

## 快速开始

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python run.py
```

`run.py` 会切换到项目根目录，依次检查依赖、执行数据库迁移、灌入演示数据并启动 uvicorn。常用参数：

```bash
python run.py --no-seed
python run.py --no-reload
python run.py --port 9000
python run.py --open
```

手工启动的等价步骤：

```bash
alembic upgrade head
python -m scripts.seed
uvicorn app.main:app --reload
```

服务启动后可以访问：

- 功能验证台：<http://127.0.0.1:8000/>
- 后台管理（独立 admin-web）：<http://127.0.0.1:8001/admin/>
- OpenAPI：<http://127.0.0.1:8000/docs>
- 健康检查：<http://127.0.0.1:8000/api/health>

## 数据库与密钥

生产环境至少需要设置 `SECRET_KEY`，它用于加密 Agent 的 API Key 和签发 SSE ticket：

```bash
python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"
```

支持三种数据库：

```dotenv
# MySQL
DATABASE_URL=mysql+asyncmy://user:pass@127.0.0.1:3306/agentdb?charset=utf8mb4

# PostgreSQL
DATABASE_URL=postgresql+psycopg://user:pass@127.0.0.1:5432/agentdb

# 本地开发
DATABASE_URL=sqlite+aiosqlite:///./web_agent.db
```

所有模型统一走 OpenAI 兼容接口。运行期地址与 API Key 存在数据库 `llm_endpoint`
表中，可按 `priority` 配置多个入口并在请求失败时依次切换。`.env` 中的
`MINIMAX_BASE_URL` / `MINIMAX_MODEL` / `MINIMAX_API_KEY` 只用于首次灌入演示数据；
之后通过 `/api/llm-endpoints` 管理，不需要重启服务。

## 模块地图

| 目录 | 职责 |
| --- | --- |
| `api/` | FastAPI 路由、SSE ticket 校验和依赖注入 |
| `core/` | LangGraph 编排、AgentRunner、工具分发、事件流、Redis 协作和 Skill Runtime |
| `config/` | 环境变量、数据库连接与引擎参数 |
| `models/` | SQLAlchemy 持久化模型 |
| `repo/` | 数据访问层 |
| `schemas/` | Pydantic 请求与响应模型 |
| `main.py` | FastAPI 工厂、生命周期、静态页面和统一异常处理 |

## 运行链路

1. `POST /api/conversations/{id}/messages` 创建 run 并立即返回 `runId`。
2. 生成任务在后台 `asyncio.Task` 中执行，不依赖原 HTTP 请求是否仍然连接。
3. `Orchestrator` 通过 LangGraph 执行 DAG 中的节点，`AgentRunner` 负责模型流式调用和工具闭环。
4. `RunHub` 为事件分配单调递增的 `seq`，`EventWriter` 批量落库。
5. 客户端通过短期 ticket 订阅 SSE；`SSEWriter` 合并实时事件和持久化回放，处理断线续传。
6. 开启 Redis 后，任意实例都能接收任意 run 的实时事件；租约和取消频道负责多实例协调。

## 相关文档

- [HTTP API 与会话协议](api/README.md)
- [编排、事件与工具运行时](core/README.md)
- [版本化设计目录](design/README.md)
- [全局设计总目录与版本历史](../docs/design/README.md)
- [设计 TODO](../docs/design/TODO.md)
- [项目级文档目录](../README.md)
