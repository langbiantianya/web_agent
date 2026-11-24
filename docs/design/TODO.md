# 设计 TODO

本页只保留当前仍未交付的工作。已完成能力、替换关系和验证记录在
[设计总目录](README.md) 与 [V4.4](../../app/design/v4.4-governance-runtime.md) 中维护。

优先级：`P0` 阻断生产采用，`P1` 重要缺口，`P2` 体验与扩展。

截至 V4.10 设计，OAuth/OIDC 主链路、独立 admin-web 业务页、workflow 可视化编排器
和 Agent JSON 输出契约已经落地，旧管理员 API Key 管理栈已退役；后台前端正在
迁移到 SvelteKit：阶段 A（骨架 + 概览页）与端到端联调已完成，阶段 B 的智能体
与 LLM 端点两页已迁，其余资源页仍走旧 Ludic 页面。尚未完成的 **P1** 共四项：
真实 IdP 联调（含登录限频、过期 attempt 清理、provider 故障监控）、存量会话
归属处理，以及 SvelteKit 迁移的阶段 B 剩余资源页与部署接线；其余 13 项均为
P2，涵盖成本账本、容器预热、MCP 能力边界与构建依赖。

## 后台前端迁移（SvelteKit）

见 [V4.10：后台前端迁移到 SvelteKit](../../app/design/v4.10-sveltekit-admin-ui.md)。
迁移分 A–E 五个阶段，**每个阶段都必须能单独回退**（关掉 `admin-ui` 容器，
反向代理直指 `admin-web` 即可）。鉴权与授权**不重写**：会话 Cookie、CSRF、
Casbin 判定、审计、Argon2 口令全部留在 Python 侧。

- [ ] **P1：阶段 B —— 资源页迁移（agents / llm 已迁，其余待迁）**
  `agents` / `skills` / `mcp` / `llm` / `workflows` 五个列表页及其表单迁到
  SvelteKit。每页至少一条 Playwright 用例（读一条 + 写一条），失败信息要能
  指到具体组件，而不是只报「页面没渲染出来」。

  **已完成**：
  - `agents`（`/admin/agents`）：Python 侧 `GET/POST /admin/api/agents`、
    `GET/PUT/DELETE /admin/api/agents/{code}`、`POST .../toggle`；SvelteKit
    页面含列表、搜索/排序/分页、创建表单与编辑面板；浏览器实测读、写、改、
    删全链路通过，JSON Schema 的「显式 null = 写 NULL」语义与页面路由一致。
  - `llm`（`/admin/llm`）：Python 侧 `GET/POST /admin/api/llm`、
    `GET/PUT/DELETE /admin/api/llm/{code}`、`POST .../toggle`；明文 Key
    不回显（只有 `hasApiKey`）；仍被 Agent 引用的连接不能停用、不能删除。

  **待迁**：`skills`、`mcp`、`workflows`（画布本阶段不重写，见阶段 D）。

- [ ] **P1：阶段 C —— 访问控制页迁移**
  `access/pages`、`access/components`、`access/roles`、`access/mappings`、
  `platform-admins`、`preferences`、`audit` 迁到 SvelteKit。
  **硬约束**：前端只消费服务端下发的「可见页面/组件」，越权判定仍然只在
  Python 侧；前端不得出现第二份权限判断逻辑。

- [x] **P1：SvelteKit 与真实 admin-web 的端到端联调**
  已与真实 admin-web 联调跑通：登录页代理、`Set-Cookie` 透传、登录态保持、
  概览页渲染、写操作的 CSRF 头、未登录 302 到登录页、旧 Ludic 页面回退，
  全部在真实浏览器里验证过。

  过程中发现并修掉一个**真实的部署级缺陷**：浏览器发的是公网 Origin，
  Python 看到的是内部地址，代理过来的写请求一律被判成跨源，登录直接失败。
  修法是**分层校验**而不是放行——SvelteKit 先校验浏览器的 Origin，不通过
  就 403；通过后把 Origin 重写成 Python 自己的 origin，Python 的同源检查
  因此照常通过。详见 [V4.10 的「Origin 的分层校验」](../../app/design/v4.10-sveltekit-admin-ui.md)。

- [ ] **P1：部署接线**
  新增 `admin-ui` 容器（`services/admin_ui/Dockerfile`，node:24-alpine，
  `PORT=8002`，`ADMIN_API_BASE_URL=http://admin-web:8001`），反向代理把
  `/admin/*` 指向它；`admin-ui` 再把仍属 Python 的路径（`/login`、`/auth`、
  `/logout`、`/static`）转发回 `admin-web`。部署前需在
  `deploy/docker-compose.admin.yml` 补服务定义，并确认健康检查与日志约定。

- [ ] **P2：阶段 D —— workflow 画布 island 化**
  现有 `workflow-editor.js`（66 KB）+ `workflow-editor-core.js`（11 KB）作为
  island 保留，只把外壳换成 SvelteKit。**本阶段不重写画布**——重写是一次
  需要独立验收的大工程，与「换前端框架」必须分开。

- [ ] **P2：阶段 E —— 下线 Ludic 视图层**
  删除 `services/admin_web/views/` 的页面渲染与 `admin-ui.js`，`admin-web`
  收敛为纯 JSON API + 登录页。删除前打 tag；对应的 Python 页面快照基线
  同步删除。删除后回退方式是按 tag 重建镜像。

- [ ] **P2：页面级 Playwright 覆盖补全**
  当前只有概览页的冒烟用例。阶段 B/C 每迁一页就补一条，长尾的表单项
  （LLM 端点探测、OAuth provider、审计筛选）需要单独的用例。
## 隔离执行与成本

- [ ] **P2：预启动 sandbox 容器池**
  当前启动期已检查并拉取镜像，消除首个请求的镜像拉取成本；尚未预先创建容器。
  若继续优化冷启动，需要为每个预热容器预分配 payload 挂载、设置回收租约，并验证
  长时间空闲容器的安全与内存成本。

- [ ] **P2：多 dispatcher 之间共享配额和预算**
  当前租户/技能租约在一个共享 dispatcher 内跨多个 studio 实例生效。多个独立
  dispatcher 之间尚未共享状态；拆分 dispatcher 前必须把准入状态迁到共享存储，
  否则不能宣称全局一致。`services/dispatcher/quota.py` 明确假设 dispatcher 是唯一
  共享控制面，这个假设在多 dispatcher 部署下不成立。

- [ ] **P2：累计计费账本**
  当前按真实 CPU 秒、内存峰值和启动耗时计算“活动请求成本”，用于准入和运行观测；
  尚未把每次消耗持久化为可对账的累计账单，也未接入云厂商价格目录。全仓目前没有
  billing/ledger 相关表或模块。

- [ ] **P2：token 成本统计**
  `run` 表没有 usage / token 列，模型用量只随 done 事件短暂存在，没有可对账的
  累计口径。要核算「某个工作流 / 租户花了多少 token」，需要先定用量来源（哪些端点
  返回 usage、哪些不返回）与缺口的估算口径，再谈成本换算。

## 用户身份与会话

- [ ] **P1：OAuth 生产加固与真实 IdP 联调**
  [V4.6 方案](../../app/design/v4.6-user-oauth-conversation.md) 中的 OIDC、PKCE、
  HttpOnly 服务端会话、CSRF、供应商管理和 conversation owner 隔离已经实现，
  并已通过专用 PostgreSQL 测试库验证。
  动态供应商配置（管理员配置一个或多个 provider，不写死）已在专用测试部署上对一个
  真实 OIDC IdP 完成端到端联调：admin 新建第二个 provider 后，客户端 `/api/auth/providers`
  与 admin 登录页按 `client_login_enabled` / `admin_login_enabled` 正确分流，新 provider
  的 authorization-code 登录成功；同时修复了 endpoint 字段无法清空和 `scopes: null`
  导致管理接口 500 的两个缺陷，并补充回归测试。
  上线前仍需用实际 `AUTH_DEFAULT_TENANT` 对应的 IdP 完成浏览器联调，并补齐三项加固：
  - **start/callback 限频**：`app/api/routes/auth.py` 的 `/oauth/{provider_code}/start`
    与 `/callback` 目前没有任何限频；全仓 `rate_limit` 只命中 admin-web 的密码重置
    （`services/admin_web/accounts.py`），用户登录链路是裸的。
  - **过期 `oauth_login_attempt` 周期清理**：`app/main.py` 的后台维护只做 checkpoint
    孤儿清理、审批超时过期和 tool_audit 保留期清理，attempt 表只增不删，需要补一条
    过期行清理任务。
  - **provider 故障监控**：失败只 redirect 一个错误码（`provider_error` /
    `login_failed`），没有指标、告警或失败率观测。

- [ ] **P1：存量 conversation 归属迁移**
  当前 `owner_user_id` 仍允许为空，以承接无法证明真实身份的存量数据。
  需要提供显式管理员映射工具，将已确认会话写入目标 user ID；完成无主数据归档后，
  再把 owner 改为非空并删除旧 `conversation.user_id` 列。不能按 username 自动认领。
  现状：`app/models/conversation.py` 的 `owner_user_id` 仍是 `nullable=True`，旧列
  `user_id` 仍在模型中，且没有面向管理员的映射入口。

## Agent 输出格式

- [ ] **P2：按 JSON Schema 校验模型输出**
  V4.9 的 JSON 输出契约只保证「是合法 JSON」：Schema 是给模型看的结构说明，
  服务端不校验实例是否真的满足 Schema。要拒绝「语法对但结构不对」的输出，
  需要引入 JSON Schema 校验依赖，并先定「不合 Schema 时是重试还是直接失败」
  —— 这条策略不定，模型可能被无休止地要求重排结构。

- [ ] **P2：上游 JSON 输出的结构化渲染**
  JSON 模式下游节点拿到的仍是上游输出的**文本**形式，渲染进下游 prompt 时不会
  自动展开成结构化字段。要让下游按字段引用上游结果，需要定义「JSON 输出在模板
  里的寻址语法」，并与 `inputTemplate` 的 `{{upstream.x}}` 机制对齐。

## MCP

- [ ] **P2：MCP authorization-code 用户委托**
  当前已完成 OAuth `client_credentials` 机器身份、refresh token、加密缓存和撤销。
  需要用户同意页与浏览器回调的 MCP 服务仍不可用，后续应接独立 OAuth 前端。

- [ ] **P2：MCP 跨实例在线状态**
  当前在线状态明确是 `online_scope=instance`，不是集群共识。若控制台要求跨实例统一展示，
  需要引入共享健康状态与过期租约。

## 审计与归档

- [ ] **P2：超大工具结果对象存储归档**
  工具审计结果默认最多保存 64K 字符。需要无限归档时，应接对象存储，并补齐签名下载、
  生命周期、访问控制和租户隔离；数据库账本继续只保存可查询摘要。

## 构建与依赖

- [ ] **P2：Python 3.15 升级（当前统一锁定 3.14）**
  **当前口径：全部镜像与本地开发环境统一锁定 Python 3.14**——主服务、admin-web、
  studio、dispatcher、sandbox 与测试镜像都是 3.14，不再混版本。这不是保守，
  是当前唯一能装上的版本：依赖链上三个「没有 cp315 轮子」的二进制包会把 3.15
  顶回源码编译，而 slim 镜像里没有 C 编译器。

  | 包 | 谁引入 | 症状 |
  | --- | --- | --- |
  | `zstandard` | `langgraph` → `langchain-core` → `langsmith` | **任何版本都没有 cp315 轮子**，只能源码编译（要 C 编译器） |
  | `orjson` | `langgraph-checkpoint-redis` / `langgraph-sdk` | 最新版最高 cp314，3.15 上退回源码编译 |
  | `PyYAML` | `alembic` → `Mako` → `PyYAML` | 最新版最高 cp314；admin-web 也被这条链卡住 |

  3.15 解释器上实测：`pip install pyyaml` 直接 `Failed to build pyyaml`；把
  `--ignore-requires-python` 开到全局更糟——pip 会挑到没有 cp315 轮子的版本再
  回退源码编译（zstandard 即如此），所以不能靠这个开关绕过。

  另外两条独立的障碍：`langsmith` 从 0.3.45 起依赖 `zstandard`，而
  `langchain-core` 1.7.0 起要求 `langsmith>=0.14.9`，绕开只能给编排引擎降依赖
  （langchain-core 降到 1.6.0 + langsmith 钉到 0.9.x），风险大于收益；
  `grpcio` 侧只有 1.85.1 提供 cp315 轮子，而它在无 AVX 的 x86_64 小主机
  （`100.114.77.27`）上有 import 即段错误（退出 139）的记录。

  **解锁条件**（四条都满足才动版本号）：
  1. `zstandard`、`orjson` 与 `PyYAML` 发布 cp315 轮子；
  2. `langgraph-checkpoint-redis` 的 `Requires-Python` 放开到 3.15；
  3. `grpcio` 1.85.1 在无 AVX 主机上的段错误被证实不复现，或上游给出可用的
     旧版 cp315 轮子；
  4. 在真实部署主机上把五个镜像全部重建并跑通 Docker e2e（当前 30 个真实容器
     用例只能在有 Docker 的环境跑）。

- [ ] **P2：把「解释器升级」写进开发环境的固定流程**
  本机之前踩过的坑：`.venv` 是用 scoop 的 `python`（指向 `current` 软链）建的，
  scoop 把 `current` 切到 3.15.2 之后，venv 里的解释器被拽着升到 3.15，而
  site-packages 里还是 3.14 ABI 的 `.pyd`，报 `DLL load failed` / `DLL 初始化例程失败`，
  根因完全看不出。当前已在 `app/README.md` 写明「用带版本号的解释器路径建 venv」，
  但还需要一条可执行的检查（例如 CI 里比对 `pyvenv.cfg` 的 `version` 与
  `sys.version`），避免同类问题再次以「依赖坏了」的形式出现。

## 已完成

- [x] **P1：Python 版本统一锁定 3.14 与 pywin32 显式钉版**
  主服务、admin-web、studio、dispatcher、sandbox 与测试镜像统一锁定 Python 3.14，
  本地开发环境同步到 3.14.7（用带版本号的解释器路径重建，避免 scoop `current`
  软链把 venv 拽到别的版本、留下 ABI 错位的 `.pyd`）。升 3.15 的前置条件写进
  上一节的「Python 3.15 升级」。同时在 `requirements.txt` 显式钉住
  `pywin32==312; sys_platform == "win32"`——它是 MCP SDK 在 Windows 上的硬依赖
  （`mcp.server.stdio` → `mcp.os.win32.utilities` → `pywintypes`），此前只靠
  `mcp` 传递引入，轮子按 CPython ABI 发布，解释器一换就只剩旧 `.pyd`。
  venv 重建方式也写进了 `app/README.md`（必须用带版本号的解释器路径）。

- [x] **P1：Workflow 编排可视化**
  手写 definition JSON 的后台编辑器已替换为拖拽画布编排器
  （`/admin/workflows/{code}`），见 [V4.8](../../app/design/v4.8-workflow-visual-editor.md)。
  图结构校验规则仍只有一份（`app/core/workflow_def.py`），后台通过
  `POST /admin/workflows/validate` 拿结构化问题，客户端 API 契约不变。
