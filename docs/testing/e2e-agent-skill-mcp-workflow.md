# 端到端验收流程：MiniMax × 智能体 × 技能 × MCP × studio × 工作流

> 落地日期：2026-11-12　｜　执行环境：`192.168.5.19`（docker compose 栈：客户端 8000 / 管理端 8001 / studio 9000）
> 可执行脚本：`scripts/e2e_agent_skill_mcp_workflow.py`　｜　夹具服务器：`scripts/e2e_mcp_server.py`
> 客户端入口：`http://192.168.5.19:8000/`（功能验证台 · 链路自检面板）

这份文档描述的是一条**真的跑得起来**的验收流程：不 mock 模型、不 mock MCP、
不 mock 事件流。它用的是已经配置好的 MiniMax 端点（管理端库里那条
`minimax` / `MiniMax-M2.7` / `https://api.minimax.cn/v1`），从「配置」一路验到
「最终消息落库」。

---

## 1. 为什么要有这条流程

系统里有六段链路，任意一段断了，用户看到的都只是「模型不回话」：

| 段 | 配在哪 | 断了的表现 |
| --- | --- | --- |
| LLM 端点 | `llm_endpoint` | 节点直接报错，或退回到别的模型 |
| 智能体 | `agent` | 找不到节点、模型名空 |
| 技能 | `skill` + `agent.skills` | 模型不按说明做事（**最隐蔽**：不报错） |
| MCP | `mcp_server` + `mcp_tool` + `agent.tools` | 模型不调工具，或调用即失败 |
| studio | `STUDIO_EXEC_ADDR` + 技能里的 `scripts/` 资源 | 技能脚本跑不了；`skill_exec` 工具压根不注册 |
| 工作流 | `workflow.definition` | 节点不串起来、审批挂起不恢复 |

以前这几段没有一个入口能一眼看完，于是排查只能靠猜。这条流程的产出就是
**一个能一眼看完的结论**：每段通过/失败，加上失败原因。

**技能、MCP、studio 是在同一次 run 里一起验的**——技能正文同时点名 MCP 工具
与 studio 脚本，模型按技能办事的过程就把两条工具链都走了一遍。

## 2. 环境前提（四条，缺一条就跑不了）

### 2.1 管理端与运行时是**分库**的

`deploy/docker-compose.stack.yml` 的注释写死了这条：**管理端与客户端必须分库、
分账号**。本机部署的实际形态是：

| 服务 | 库 | 表 |
| --- | --- | --- |
| admin-web（:8001） | PostgreSQL `webagent_admin` | `llm_endpoint` / `agent` / `skill` / `workflow` / `mcp_server` |
| web-client（:8000） | MySQL `webagent` | 同名表（运行时真正读的那一套） |

**后果**：在管理端配好的 MiniMax 端点，在运行时库里**并不存在**。管理端看到的
「已配置」与真正被调用的配置是两套数据。脚本因此先把端点（含 Fernet 密文）
从管理端库**镜像**到运行时库，再验一次密文能不能解开——解不开就等于没配
（两边共用同一个 `SECRET_KEY`，密文可以原样搬运）。

> 这是本次排查出来的**第一件真事**：端点不可见不是配置问题，是拓扑问题。
> 修复方式写在 §7。

### 2.2 stdio MCP 需要放行命令白名单

`MCP_ALLOWED_COMMAND_PREFIXES` 默认为空 = **禁止注册 stdio 服务器**（fail-closed，
注册 stdio 服务器等于允许执行某个程序）。本机部署放开的范围是：

```dotenv
MCP_ALLOWED_COMMAND_PREFIXES=["/usr/local/bin/python"]
```

### 2.3 studio 必须部署着

技能脚本在 **studio 容器**里执行（§22.8）。客户端要配上：

```dotenv
STUDIO_EXEC_ADDR=studio:9000
STUDIO_EXEC_REQUIRED=true
```

`STUDIO_EXEC_REQUIRED=true` 时 studio 连不上会**让主服务启动失败**——这是故意的：
部署不完整就别装作能跑。脚本执行能力**整体不存在**时，`skill_exec` 工具根本不
注册（模型看不见它），所以流程里如果模型压根没调 `skill_exec`，先查 studio。

### 2.4 浏览器端登录需要运行时库里有登录供应商

客户端控制台走 OIDC 登录。运行时库里的 `oauth_provider` 行是同步过去的
（`local-idp`，issuer `http://192.168.5.19:3002`，client `webagent-admin`）。
最小 IdP 的可用账号：`admin / 666666`、`user / 666666`。

## 3. 测试资产

| 资产 | code | 说明 |
| --- | --- | --- |
| 技能 | `e2e-selfcheck-skill` | 正文写死「先调 `mcp__e2e-selfcheck-mcp__add`，再调 `skill_exec` 跑 `scripts/e2e_sum.py`」 |
| 技能脚本 | `e2e-selfcheck-skill` / `scripts/e2e_sum.py` | studio 执行的脚本，stdout 打固定标记 `E2E_STUDIO_SUM:7` |
| MCP 服务器 | `e2e-selfcheck-mcp` | stdio，子进程跑 `scripts/e2e_mcp_server.py` |
| 工具 | `mcp__e2e-selfcheck-mcp__{add,echo,now_iso,always_fail}` | 限定名由「服务器 code + 工具名」拼出 |
| 智能体 | `e2e-selfcheck-agent` | 绑 minimax 端点 + 技能 + 两个 MCP 工具 + `skill_exec` |
| 智能体 | `e2e-selfcheck-writer` | 绑 minimax 端点，不带工具（汇总节点） |
| 工作流 | `e2e-selfcheck-wf` | `collect`（技能+MCP）→ `gate`（人工审批）→ `report`（汇总） |
| 终端用户 | `e2e-selfcheck` | 直接在运行时库建用户 + 服务端会话（不走外部 IdP） |

**技能正文是行为指令**：模型只有拿到这段正文才会去调那个 MCP 工具、才会让
studio 跑脚本。所以「工具真的被调了」反过来证明「技能真的注入进提示词了」
——这是整条流程里最有说服力的一条断言。

## 4. 用例总表（26 条）

编号前缀 `E2E-`。每条都给出**判定口径**——「看起来对」不算判定。

### 4.1 配置与同步（E2E-00 ~ E2E-07）

| 编号 | 用例 | 判定口径 | 实测 |
| --- | --- | --- | --- |
| E2E-00 | OIDC 登录供应商已同步到运行时库 | 运行时库里有一条指得着的供应商（issuer 可达） | PASS（来源 admin-db） |
| E2E-01 | MiniMax 端点已同步到运行时库 | 运行时库里有该 code，地址与模型与管理端一致 | PASS（`https://api.minimax.cn/v1` / `MiniMax-M2.7`） |
| E2E-02 | 端点密文在运行时可解密 | `decrypt(api_key_cipher)` 非空（只看长度，不打印内容） | PASS（密钥长度 125） |
| E2E-03 | 技能已发布 | `status=published` 且 `enabled=1`，资源文件在 | PASS（v1 + `references/e2e.md`） |
| E2E-04 | 技能正文注入到提示词 | 用**编排器同一个拼装函数** `build_skill_messages` 验：注入列表含该 code，且正文里含工具限定名 | PASS |
| E2E-05 | MCP 服务器已注册且工具目录已发现 | 刷新目录后 `mcp_tool` 快照里有 `add`/`echo` 两个限定名 | PASS（4 个工具） |
| E2E-06 | MCP 工具可真实调用 | 真起 stdio 子进程，调用 `add(2,40)` 返回 `E2E_SUM:42` | PASS |
| E2E-07 | 失败工具映射为错误结果 | 调 `always_fail` 返回「失败 + 说明」而不是抛到节点 | PASS（`Error executing tool always_fail`） |

### 4.2 用户视角的一次完整会话（E2E-08 ~ E2E-20）

| 编号 | 用例 | 判定口径 | 实测 |
| --- | --- | --- | --- |
| E2E-08 | 客户端服务健康 | `GET /api/health` = 200 | PASS |
| E2E-09 | 会话已创建 | `POST /api/conversations` = 201 | PASS |
| E2E-10 | 消息已提交并触发 run | `POST .../messages` = 202 且返回 run_id | PASS |
| E2E-11 | 第一段流收到模型吐字 | 第一段订阅里出现 `token` 事件 | PASS（首段 4 个事件） |
| E2E-12 | **断线后可凭 last_seq 续传且无重复** | 主动断开后带 `last_seq` 重连，重连后收到的 seq 全部 **>** 断点 | PASS（4 → 14，重复 0） |
| E2E-13 | 审批节点挂起并接受决策 | 收到 `approval_required` 后 `POST /api/runs/{id}/approval` 返回 200 | PASS（重试 3 次，见 §7.2） |
| E2E-14 | run 跑到终态 | 收到 `done` 且没有 `error` 事件 | PASS（status=done） |
| E2E-15 | 工作流节点按定义顺序执行 | `node_start` / `node_end` 的节点序列 = 定义里的 agent 节点序列 | PASS（collect → report） |
| E2E-16 | 模型在节点内真实调用了 MCP 工具 | 事件流里有 `tool_call` 且工具名是 MCP 限定名，并有同名 `tool_result` | PASS |
| E2E-17 | 工具结果回灌到模型上下文 | `tool_result.summary` 里含工具返回的标记 | PASS（`E2E_SUM:2`） |
| E2E-18 | 事件 seq 严格递增且无重复 | 全部事件的 seq 两两严格递增 | PASS（1…34，34 个事件） |
| E2E-19 | 最终 assistant 消息已落库 | `GET /api/runs/{id}/messages` 里有非空 assistant 正文 | PASS（145 字） |
| E2E-20 | 工具调用审计已落库 | `tool_call_audit` 里有本次 run 的 MCP 工具记录 | PASS（1 条命中） |

### 4.3 studio 脚本执行（E2E-21 ~ E2E-25）

这一段验的是**技能脚本**这一条链：脚本正文随请求发到 studio，studio 起子进程
跑它，输出再回灌给模型（§22.8）。**技能、MCP、studio 在同一次 run 里一起用**
——一次调用同时证明「技能注入 → 模型按技能办事」与两条工具链都通。

| 编号 | 用例 | 判定口径 | 实测 |
| --- | --- | --- | --- |
| E2E-21 | studio 脚本执行服务已连接 | studio 的 Health 返回 ready，版本 ≥ 最低要求 | PASS（2.0.0，运行时 bash/node/python，档位 fast/isolated） |
| E2E-22 | 技能脚本经 studio 真实执行 | 直接调 `exec_skill_script`，退出码 0 且 stdout 含脚本标记 | PASS（`exit_code: 0 --- stdout --- E2E_STUDIO_SUM:7`） |
| E2E-23 | 模型在同一次 run 里真实调用了 skill_exec | 事件流里有 `tool_call` 且工具名是 `skill_exec` | PASS（调用列表含 `mcp__…__add` 与 `skill_exec`） |
| E2E-24 | studio 脚本输出已回灌到模型上下文 | `tool_result.summary` 里含脚本打印的标记 | PASS（`E2E_STUDIO_SUM:7`） |
| E2E-25 | skill_exec 调用审计已落库 | `tool_call_audit` 里有 `skill_exec` 记录 | PASS（1 条命中） |

> **注意**：脚本进程**不是** web_agent 主服务。主服务在启动时 `init_studio()`
> 配置过 studio，验收脚本是独立进程，得自己 `configure_studio()` 一次——否则
> `get_studio_client()` 永远是 `None`，会被误判成「studio 没部署」。

## 4.4 模型与密钥（2026-11-12 换过一次）

端点 `minimax` 现在的配置：

| 字段 | 值 |
| --- | --- |
| 地址 | `https://api.minimax.cn/v1` |
| 默认模型 | `MiniMax-M3.1-Flash` |
| 密钥 | 已加密存库（Fernet），**不入仓库、不入日志** |

换密钥时**两个库都要换**（管理端 PG + 运行时 MySQL），流程是：

1. 直接打一次接口确认新密钥与模型可用（`/v1/models` + 一次带工具的
   `/chat/completions`）；
2. 用应用自己的 `encrypt()` 在两个库里各写一遍——两边共用同一个
   `SECRET_KEY`，密文可以原样搬运；
3. **验一次能解开**（E2E-02 就是这条）。

两个模型的实测（都是新密钥、同一条流程、21/21 通过）：

| 模型 | 事件数 | 最终答复长度 | 工具调用 |
| --- | --- | --- | --- |
| `MiniMax-M3.1-Flash` | 67 | 48 字 | ✅ 调了 `mcp__e2e-selfcheck-mcp__add` |
| `MiniMax-M2.7` | 31 | 232 字 | ✅ 同上 |

两个模型都支持 OpenAI 兼容的 function-calling，工具结果回灌（`role=tool`）
也正常。

## 5. 怎么跑

```bash
# 1) 部署（改了 app/ scripts/ web/ 都要重建镜像）
cd /docker/web-agent-stack
docker compose -f deploy/docker-compose.stack.yml --env-file .env up -d --build web-client

# 2) 跑验收流程（在客户端容器里：脚本要 import 应用代码，也要连运行时库）
docker exec -w /app -e PYTHONPATH=/app webagent-client \
    python scripts/e2e_agent_skill_mcp_workflow.py --keep

# 3) 机器可读的结论（CI 采集用）
docker exec -w /app -e PYTHONPATH=/app webagent-client \
    python scripts/e2e_agent_skill_mcp_workflow.py --json
```

常用参数：

| 参数 | 作用 |
| --- | --- |
| `--base-url` | 客户端服务地址，默认 `http://127.0.0.1:8000` |
| `--llm-code` | 使用的端点 code，默认 `minimax` |
| `--oidp-code` | 同步的登录供应商 code，默认 `local-idp` |
| `--skip-oidc` | 不同步登录供应商（只验运行时） |
| `--keep` | 跑完保留布场数据（默认清理） |
| `--json` | 额外打印一行 JSON 报告 |

退出码：`0` 全部通过；`1` 有用例失败；`2` 环境/配置问题跑不起来。

**脚本不打印任何密钥、Token 或密文**（只报密钥长度）。

## 6. 浏览器端复现（客户端功能验证台）

除了脚本，客户端控制台也做了一键复现：**⑦ 链路自检**面板
（`http://192.168.5.19:8000/`）。

面板分三段：

- **配置自检**（静态，来自 `GET /api/selfcheck`）：每个工作流的每一段配置
  （图结构 / 模型端点 / 技能 / MCP）逐段给出通过、失败或提醒；
- **Studio 脚本执行**（静态）：studio 在不在、连不连得上、版本与档位、
  哪些技能带 `scripts/` 下的脚本（没脚本的 studio 是空转）；
- **动态探测**（动态）：真的建会话、发消息、订阅 SSE、跑完一次工作流，
  然后按事件打分：模型有没有吐字、有没有调 MCP 工具、**有没有调
  `skill_exec` 让 studio 跑脚本**、节点有没有按序执行、审批挂起后有没有
  继续、最终消息有没有落库。

复现步骤（全部鼠标操作）：

1. 打开 `http://192.168.5.19:8000/`，下拉选「本地 IdP」，点「登录」；
2. 在 IdP 页填 `admin / 666666`，点「登录并授权」；
3. 回到验证台，点「**运行自检**」；
4. 流程跑到审批节点会挂起 → 在对话流里点「**批准**」；
5. 自检面板逐段出结论；点「**导出报告**」下载 JSON。

实测截图：

- `docs/testing/audit/client-selfcheck-m3flash-pass.png`（M3.1-Flash 全过）
- `docs/testing/audit/client-selfcheck-m3flash-8of8.png`（动态探测 **8/8 通过**）
- `docs/testing/audit/client-selfcheck-studio-pass.png`（加上 studio 后
  **9/9 通过**，含「脚本执行（studio）」一段）

### 6.1 自检面板的两处收尾（已修）

第一版面板里，「建立会话」「已触发 run」这两个阶段是以**进度态**（RUN）加进去的，
跑完之后没人把它们落定，于是它们永远停在 RUN——汇总那句话就成了
「动态探测 6/8 通过」，明明全过，看着像有东西没成。

现在收尾时会把这两段落成 pass/fail（`setStage()`）：

- 「建立会话」：会话建成了就是 pass，写明 conv id；
- 「已触发 run」：run 终态是 `done` 才算 pass，否则 fail 并写明终态。

修完再跑就是 **8/8 通过**，两段都带着结论，不再有悬着的 RUN。

左栏同时补齐了以前**看不见**的三段：模型端点、技能、MCP 服务器（工具数与
限定名）。以前客户端只有 Agent 与工作流两个列表。

## 7. 实测发现的三件真事

### 7.1 管理端配好的端点，运行时根本读不到

见 §2.1。这是**拓扑**问题不是配置问题：管理端与运行时分库（compose 里明确
要求分库分账号），而端点表在两套库里各有一份。管理端页面显示「已配置
MiniMax」时，运行时那一侧是空的。

本轮的处理方式（**不改拓扑**）：验收脚本在开跑前把端点镜像到运行时库并
验解密，把「两套数据」这件事变成流程里显式的一步。长期看要么让管理端写
运行时库、要么加一条同步通道——那是架构决策，不在本次范围内。

### 7.2 审批事件与 run 状态落库之间有一个竞态（已修）

编排器 `_pause` 的顺序是：**先建审批记录 → emit `approval_required` → 再把
run 置为 `waiting`**。中间只有几毫秒。于是「看到审批卡片就立刻点批准」会撞上
`POST /api/runs/{id}/approval` 的状态判断，返回 **409**（"run 当前状态是
running，不在等待审批"）。

实测复现：脚本第一次就撞上了（HTTP 409），客户端前端同样会撞——人点得够快
就会看到一条红色报错。

**修法（服务端）**：有**待审批记录**就说明流程确实停在审批节点上——它比 run
状态更强。于是接口改成：

1. 先看有没有待审批记录；没有 → 仍然 409（不能把「压根没审批」也放过去）；
2. 有记录但状态还没落上 → **等编排器把状态落上**（最多等 5 秒），而不是
   让人重试。

为什么「等状态」而不是「直接放行」：run 的 `status` 与 `last_seq` 是**同一条
UPDATE** 写的，所以「状态变成 waiting」就意味着 seq 锚点也已经写准——恢复执行
读到的 `start_seq` 不会退回旧值（退回去会与 `run_event` 的 `uk_run_seq` 撞车，
事件直接丢）。

等的过程中每次轮询前都要 `rollback`：MySQL 默认的 REPEATABLE READ 会让同一
个事务反复读到**同一个快照**，不换掉事务就永远看不到状态变化。

修完的效果：审批**第一次就成功**（实测「尝试 1 次」，修之前是 3～5 次）。
客户端那层 409 重试保留着当兜底，但它已经不再是常态路径。

回归用例：`tests/test_approval_race.py`（4 条：竞态窗口内提交成功 / seq 锚点
没退回旧值 / 没有待审批记录仍然 409 / 正常 waiting 路径不变）。

### 7.3 studio 是独立容器，脚本进程要自己配一次（已修）

验收脚本是**独立进程**，不是 web_agent 主服务。主服务在启动时 `init_studio()`
配过 studio 客户端，脚本进程没有——`get_studio_client()` 永远是 `None`，
于是 E2E-21/22 一开始判成「studio 没部署」。

修法：脚本自己 `configure_studio(settings.studio_exec_addr)` 一次，并在退出前
`close_studio_client()`。这也从侧面说明一件事——**部署里 studio 是不是真的
在跑，光看客户端的界面是看不出来的**，得有一条独立断言去问它。

回归用例：`tests/test_selfcheck_studio.py`（3 条：没配 studio 是提醒不是失败 /
配了但连不上是失败 / 就绪时脚本资源要列出来）。

## 8. 回归防护

| 位置 | 覆盖内容 |
| --- | --- |
| `tests/test_selfcheck_api.py` | 自检接口的判定逻辑：全通 / 端点停用 / 技能未发布 / 工具不在目录 / 智能体不存在 / 未登录 |
| `scripts/e2e_agent_skill_mcp_workflow.py` | 流程本身（真跑，需要部署环境；不进日常单测） |
| `scripts/e2e_mcp_server.py` | stdio MCP 夹具（真起子进程） |

日常单测：`pytest tests/ --ignore=tests/test_mcp_e2e.py`。
