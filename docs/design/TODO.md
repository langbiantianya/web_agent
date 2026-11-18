# 设计 TODO

本页只保留当前仍未交付的工作。已完成能力、替换关系和验证记录在
[设计总目录](README.md) 与 [V4.4](../../app/design/v4.4-governance-runtime.md) 中维护。

优先级：`P0` 阻断生产采用，`P1` 重要缺口，`P2` 体验与扩展。

截至 V4.9 设计，OAuth/OIDC 主链路、独立 admin-web 业务页、workflow 可视化编排器
和 Agent JSON 输出契约已经落地，旧管理员 API Key 管理栈已退役。尚未完成的生产
P1 项集中在真实 IdP 联调、
登录限频/过期 attempt 清理、存量会话归属处理，以及 Python 3.15 升级的上游阻塞。

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
