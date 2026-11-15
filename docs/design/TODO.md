# 设计 TODO

本页只保留当前仍未交付的工作。已完成能力、替换关系和验证记录在
[设计总目录](README.md) 与 [V4.4](../../app/design/v4.4-governance-runtime.md) 中维护。

优先级：`P0` 阻断生产采用，`P1` 重要缺口，`P2` 体验与扩展。

截至 V4.8 设计，OAuth/OIDC 主链路、独立 admin-web 业务页和 workflow 可视化编排器
已经落地，旧管理员 API Key 管理栈已退役。尚未完成的生产 P1 项集中在真实 IdP 联调、
登录限频/过期 attempt 清理、存量会话归属处理，以及 Windows 开发环境依赖缺口。

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

- [ ] **P1：Windows 开发环境 pywin32 与 Python 版本错配**
  本机 `.venv` 是 Python 3.15.2，而 `Dockerfile` / `Dockerfile.admin` / `Dockerfile.test`
  全部锁 Python 3.14（Ludic 1.x 要求 ≥3.14）。venv 内装的是 pywin32 312，在 3.15 上
  加载失败，导致 `tests/test_mcp_e2e.py::test_refresh_discovers_tools_into_snapshot` 失败：
  `mcp/os/win32/utilities.py` → `import pywintypes` → `_win32sysloader` 报
  `DLL load failed while importing _win32sysloader`。这不是产品缺陷，但会掩盖真实回归。
  处理方式：对齐本地 Python 到 3.14，或安装与 3.15 匹配的 pywin32 版本。

- [ ] **P1：requirements.txt 未显式钉住 pywin32**
  pywin32 目前只通过 `mcp` SDK 在 Windows 上传递引入（`mcp` 2.x 的 stdio 传输在
  Windows 上依赖 `mcp.os.win32.utilities` → `pywintypes`），版本随 Python 解释器漂移。
  需要在依赖里按 Python 版本显式约束，避免不同机器上的 MCP stdio 测试结果不一致。

## 已完成

- [x] **P1：Workflow 编排可视化**
  手写 definition JSON 的后台编辑器已替换为拖拽画布编排器
  （`/admin/workflows/{code}`），见 [V4.8](../../app/design/v4.8-workflow-visual-editor.md)。
  图结构校验规则仍只有一份（`app/core/workflow_def.py`），后台通过
  `POST /admin/workflows/validate` 拿结构化问题，客户端 API 契约不变。
