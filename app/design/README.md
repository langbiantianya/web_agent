# web_agent 主服务设计索引

主设计按版本拆分。当前已实现系统由 V1–V4.6 累积组成；V4.6 的用户 OIDC
登录、服务端会话与历史归属已落地并通过真 PostgreSQL 验证。V4.7 已完成
独立 admin-web 服务、OAuth 管理员、账号密码超级管理员、Casbin RBAC、Ludic SSR 管理端设计，待实现；真实 IdP 联调、存量数据和旧管理员
迁移仍以 TODO 为准。
版本页不重复粘贴早期章节，因此排障或修改基础协议时要回到对应版本。

| 版本 | 文档 | 主要内容 |
| --- | --- | --- |
| V1 | [单实例基础版](v1-foundation.md) | 目标、架构、持久化、SSE、线性流水线 |
| V2 | [多实例与 LangGraph](v2-multi-instance.md) | Redis、租约、Stream 回放、LangGraph |
| V3 | [编排、审批、工具与安全](v3-orchestration.md) | DAG、审批、工具、PG、敏感过滤 |
| V4 | [MCP、Skill 与脚本执行](v4-tools-skills.md) | MCP、Skill、studio 调用侧 |
| V4.1 | [数据库 LLM 接口与故障切换](v4.1-llm-endpoints.md) | 多接口优先级、加密 Key、请求失败自动切换 |
| V4.2 | [后台治理与运行期维护](v4.2-admin-maintenance.md) | Skill 后台、MCP 空闲回收、checkpoint 孤儿清理、测试隔离 |
| V4.3 | [工具审计与结果持久化](v4.3-tool-audit.md) | 独立审计账本、脱敏参数与结果、管理员查询、保留期清理 |
| V4.4 | [治理、审批与运行时收口](v4.4-governance-runtime.md) | 管理员与租户、Skill 生命周期、会签超时、工具策略、MCP 资源、Trace、取消、配额与指标 |
| V4.5 | [备份恢复、沙箱准入与成本闭环](v4.5-recovery-governance.md) | PostgreSQL PITR、自定义 seccomp、共享配额、真实资源成本、MCP OAuth |
| V4.6 | [用户 OAuth 登录与会话历史归属](v4.6-user-oauth-conversation.md) | OIDC 登录、本地用户、服务端会话、会话 owner 隔离与迁移方案 |
| V4.7 | [独立 admin-web、Casbin RBAC 与 Ludic SSR](v4.7-oauth-admin.md) | 独立容器、OAuth `admin_user`、账号密码超级管理员、Casbin、页面/组件权限、三主题 |

跨项目版本历史、已移除方案和任务导航见 [设计总目录](../../docs/design/README.md)。未完成事项只在 [TODO](../../docs/design/TODO.md) 中维护。

## 阅读建议

- 第一次了解系统：先读 [app/README.md](../README.md)，再读 V1 和 V4。
- 排查 run/SSE：读 V1、V2 与 V3。
- 修改 Workflow：读 V1、V2-b、V3。
- 接入 MCP 或 Skill：读 V3.2、V4。
- 配置模型地址、Key 或故障切换：读 V4.1。
- 管理 Skill、排查运行期资源残留：读 V4.2。
- 排查工具参数、结果、耗时或敏感输出：读 V4.3。
- 管理用户、租户、Skill 发布、审批会签或 sandbox 生命周期：读 V4.4。
- 配置数据库恢复、自定义 seccomp、跨 studio 配额、成本费率或 MCP OAuth：读 V4.5。
- 设计或实现终端用户登录、Cookie 会话和用户历史隔离：读 V4.6。
- 设计或实现独立 admin-web 容器、平台超级管理员、Casbin、Ludic SSR、UI 权限和主题：读 V4.7。
- 修改 studio 契约：同时读 [V4 调用侧](v4-tools-skills.md) 和 [studio 服务侧](../../services/studio/design/v4-current.md)。
