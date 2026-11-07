# 设计文档总目录

这里是项目设计文档的唯一入口。设计按产品迭代版本拆分，先读本页确认版本边界，再进入对应版本；未完成事项统一维护在 [TODO.md](TODO.md)，不再散落在各版本正文中。

## 当前状态

| 项 | 当前版本 | 状态 |
| --- | --- | --- |
| web_agent 主服务 | V4.6 | 基础实现完成 |
| 用户 OAuth 与会话历史归属 | V4.6 | OIDC、会话与所有权隔离已通过真 PostgreSQL 验证；真实 IdP/存量迁移待完成 |
| 独立 admin-web 与管理员体系 | V4.7 | 第一阶段已实现并通过 Docker/PostgreSQL 验证；独立 Ludic 容器、两种 Session、Casbin、平台超级管理员、UI 权限、三主题、通知与审计已落地，业务管理页仍在迁移 |
| 生产恢复与沙箱治理 | V4.5 | 已实现并完成真 PostgreSQL / Docker 验证 |
| MCP 工具 | V4 | 已实现 |
| Skill 指令包与资源工具 | V4 | 已实现 |
| studio fast 档 | V4 | 已实现并验证 |
| studio isolated Docker 档 | V4 / Phase 1 | 已实现并在真 Docker 验证 |
| LLM 接口配置与故障切换 | V4.1 | 已实现 |
| PostgreSQL 持久化后端 | V3 / V4.4 | 18.6 已部署；迁移、并发、崩溃恢复、逻辑备份恢复和 PgBouncer 事务池验证通过 |
| PostgreSQL 物理备份与 PITR | V4.5 | 全量、增量、WAL 归档和隔离时间点恢复已验证 |
| sandbox 安全与成本 | V4.5 | 自定义 seccomp、共享 dispatcher 配额、cgroup 资源指标已验证 |

代码、proto 和自动化测试是最终事实来源；设计文档解释边界、取舍和演进原因。若文档与可执行契约冲突，以 `proto/` 与测试为准，并同步修正文档。

## 文档地图

### 主服务

| 文档 | 内容 |
| --- | --- |
| [主服务设计索引](../../app/design/README.md) | V1–V4.1 的阅读顺序与章节映射 |
| [V1：单实例基础版](../../app/design/v1-foundation.md) | 目标、架构、持久化、SSE、线性 Agent 流水线 |
| [V2：多实例与 LangGraph](../../app/design/v2-multi-instance.md) | Redis Stream/Pub/Sub、租约、LangGraph 替换自研控制流 |
| [V3：编排、审批、工具与安全](../../app/design/v3-orchestration.md) | DAG、条件边、人工审批、工具闭环、PostgreSQL、敏感内容过滤 |
| [V4：MCP、Skill 与脚本执行](../../app/design/v4-tools-skills.md) | MCP、Skill、studio 调用侧、真实验收 |
| [V4.1：数据库 LLM 接口与故障切换](../../app/design/v4.1-llm-endpoints.md) | 多接口优先级、加密 Key、失败切换 |
| [V4.2：后台治理与运行期维护](../../app/design/v4.2-admin-maintenance.md) | Skill 后台、MCP 空闲回收、checkpoint 孤儿清理、测试隔离 |
| [V4.3：工具审计与结果持久化](../../app/design/v4.3-tool-audit.md) | 独立工具审计账本、脱敏与限长结果、管理员查询、保留期清理 |
| [V4.4：治理、审批与运行时收口](../../app/design/v4.4-governance-runtime.md) | 数据库管理员与租户、Skill 生命周期、会签超时、工具策略、MCP 动态资源、Trace、取消、配额与指标 |
| [V4.5：备份恢复、沙箱准入与成本闭环](../../app/design/v4.5-recovery-governance.md) | PostgreSQL PITR、自定义 seccomp、跨 studio 准入、真实资源成本、MCP OAuth 机器身份 |
| [V4.6：用户 OAuth 登录与会话历史归属](../../app/design/v4.6-user-oauth-conversation.md) | OIDC、服务端会话、用户身份绑定、conversation owner、越权防护与迁移 |
| [V4.7：独立 admin-web、Casbin RBAC 与 Ludic SSR](../../app/design/v4.7-oauth-admin.md) | 独立容器、OAuth 管理员、账号密码超级管理员、Casbin、页面/组件权限与三主题 |

### studio 服务

| 文档 | 内容 |
| --- | --- |
| [studio 设计索引](../../services/studio/design/README.md) | 服务侧阅读入口 |
| [V4：studio 当前服务设计](../../services/studio/design/v4-current.md) | gRPC、fast/isolated、加固、部署、故障与验证 |
| [studio 使用说明](../../services/studio/README.md) | 启动、配置、源码索引 |

### 配套设计

| 文档 | 内容 |
| --- | --- |
| [主服务概览](../../app/README.md) | 面向开发和运维的当前系统总览 |
| [API 说明](../../app/api/README.md) | HTTP、SSE 与前端接入 |
| [核心运行时说明](../../app/core/README.md) | Workflow、AgentRunner、工具和状态管理 |
| [服务拓扑](../../services/README.md) | web_agent、studio、dispatcher、sandbox 的信任边界 |
| [部署说明](../../deploy/README.md) | Docker Compose 与安全配置 |
| [性能报告](../../scripts/PERFORMANCE.md) | 容量实测与成本模型 |
| [测试策略](../../tests/README.md) | 测试层次与集成验证 |
| [历史实施计划](../superpowers/plans/README.md) | 已完成迭代的原始任务记录，不是当前 TODO |

## 版本演进

版本页把原大文档按轮次切片：V1 保存稳定基础章节，后续页面保存当轮增量与实施记录。
基础章节仍描述当前有效行为，不是冻结的历史快照；章节编号沿用拆分前文档，避免已有讨论和
测试引用失效。

| 版本 | 新增 | 替换或修正 | 状态 |
| --- | --- | --- | --- |
| V1 | FastAPI、MySQL/PostgreSQL/SQLite 持久化、SSE 续传、线性 Agent 流水线、run/event/message 模型 | 建立单实例基线 | 已完成 |
| V2 | Redis Pub/Sub、Redis Stream 回放、归属租约、取消通道、LangGraph 控制流 | 自研线性控制流被 LangGraph 替换；Redis 模式下不再依赖 `hub.flush()` 保证尾部事件 | 已完成 |
| V2-b | 图节点执行与图连线解耦，显式 `edges` | V1 按节点声明顺序串联的做法退出主路径 | 已完成 |
| V3 | DAG、条件边、并行 reducer、人工审批、工具调用闭环、PostgreSQL 适配、容量实测 | 审批节点不再用特殊 Agent code 冒充；条件与 DAG 错误前移到配置期 | 已完成 |
| V3.4 | 字符 n-gram 逻辑回归敏感内容过滤、API 与 LLM 双防线 | 不再依赖关键词表作为唯一拦截手段 | 已完成 |
| V4 | MCP stdio/Streamable HTTP、MCP 工具目录、Skill 指令包、`skill_resource`、`skill_exec` | 工具来源不再局限于代码注册表；MCP 不再使用已废弃的 HTTP+SSE 双端点传输 | 已完成 |
| V4 isolated | studio → dispatcher → 一次性 sandbox、档位能力协商、独立并发池、真 Docker 加固和五场景验证 | isolated 不再只是骨架；禁止静默降级到 fast | Docker 已完成 |
| V4.1 | 独立 `llm_endpoint` 配置表、加密 Key、优先级排序、请求失败自动切换 | 模型接口地址与 Key 不再要求绑定在单个 Agent 上；新配置优先于兼容字段 | 已完成 |
| V4.2 | Skill 管理后台、导入风险与运行档展示、`allowed-tools` 差异提示、MCP 空闲回收、checkpoint 孤儿清理 | 基础设施与 API 已有但缺少运营入口或周期治理；gRPC 测试污染固化在测试入口处理 | 已完成 |
| V4.3 | `tool_call_audit` 独立审计账本、参数与结果脱敏、管理员查询、保留期清理 | 工具结果不再只能依赖事件摘要与节点调用清单回看；审计写入与业务事务解耦 | 已完成 |
| V4.4 | 数据库管理员/角色/租户、Skill 草稿发布归档与二进制回滚、审批会签超时、工具级策略、MCP resources/prompts 与动态通知、W3C Trace、sandbox 取消/指标/技能配额/摘要校验 | 共享管理员 Key 替换为“环境引导 Key + 数据库用户”；Skill 硬删除改为软删除；二进制只存摘要改为可回滚快照；单审批人改为会签；MCP 只接 tools 扩展为 resources/prompts；取消从进程边界贯通到容器 | 已完成 |
| V4.5 | PostgreSQL 全量/增量备份与 PITR、自定义 seccomp、共享 dispatcher 租约配额与预算、cgroup 真实 CPU/内存指标、MCP OAuth 机器身份 | `backup_label` 作为备份识别依据改为显式标记；seccomp 文件路径改为内联 JSON；实例内配额改为共享 dispatcher 准入；静态 MCP header 扩展为可刷新 OAuth | 已完成 |
| V4.6 | 用户 OIDC 登录、本地 `app_user`、服务端 cookie session、PKCE/state/nonce、conversation owner 隔离 | 客户端提交 `user_id` 改为认证主体派生；OAuth token 不进入浏览器存储；消息、run、审批和 SSE 统一按会话 owner 授权 | 基础实现与真 PostgreSQL 验证完成；真实 IdP 与存量迁移待完成 |
| V4.7 | 独立 `admin-web` 容器、OAuth `admin_user`、`super_admin_account`、Casbin RBAC、Ludic SSR、页面/组件权限、三主题、密码轮换、通知与审计 | 后台不再挂载到客户端进程；服务、Session、镜像和发布独立；平台超级管理员不校验角色；密码只可随机重置并支持 30 天轮换 | 第一阶段已实现并通过 Docker/PostgreSQL 验证；业务管理页迁移中 |

## 已移除、已替换或明确不做

| 项目 | 当前口径 | 原因 |
| --- | --- | --- |
| V1 自研线性控制流 | 由 LangGraph 图执行替换 | 条件边、并行与审批需要稳定的图语义 |
| 多实例下以 `hub.flush()` 作为续传正确性前提 | Redis 模式以 Stream 为同步回放源 | 不能要求事件恰好在另一实例的本地缓冲里 |
| 用特殊 `agentCode` 表示审批节点 | 使用显式 `type=approval` | 审批不是 Agent，定义必须自解释 |
| MCP 旧 HTTP+SSE 双端点传输 | 只支持 stdio 与 Streamable HTTP | 上游旧传输已废弃，维护两套会话语义没有收益 |
| isolated 只有骨架、运行时缺失后退回 fast | 独立容器执行；能力不足明确失败 | 静默降级会让调用方误以为获得了强隔离 |
| 只依赖容器 `State.OOMKilled` 判断沙箱 OOM | 子进程 OOM 结合 cgroup v1/v2 `oom_kill` 计数 | runner 存活时容器本身可能没有 OOMKilled |
| 在 gRPC 进程中保留默认 fork 支持 | 启动前关闭 fork 支持，并使用 tini | 避免 atfork 状态污染与孤儿/僵尸 |
| 任意镜像 ref、宿主挂载、sandbox 出网 | 镜像目录白名单、只读 payload、无网络 | 固定模板才能控制信任边界 |
| 只用静态 MCP header | 支持 OAuth client credentials、refresh、撤销与密文缓存 | 机器身份可自动续期，短期明文令牌不再进入配置 |
| 运行时默认 seccomp | dispatcher 加载项目自定义 profile | 固定 syscall 拒绝面，缺失时 fail-closed |
| 各 studio 实例各自限流 | 共享 dispatcher 租约跨 studio 生效 | 多实例不能各自按上限计费后共同超额 |
| 只记录 sandbox 耗时 | 采集 cgroup CPU、内存峰值并估算成本 | 成本需要真实资源来源，而不只是墙钟时间 |
| `backup_label` 判断物理备份 | 使用 `.webagent-basebackup` 标记 | 现代 `pg_basebackup` 产物不保证保留该文件 |
| 每个 Agent 只能使用一个模型接口 | 使用数据库 `llm_endpoint` 优先级链 | 主接口失败时自动切换，同时保留旧 Agent 字段兼容升级 |
| Skill 只能通过 API 或脚本维护 | 后台覆盖 CRUD、ZIP、资源、修订和 Agent 绑定 | 配置即数据需要与可操作的管理界面配套 |
| MCP 连接只靠进程退出释放 | 应用生命周期内周期回收空闲连接 | 避免 stdio 子进程和远端会话长期驻留 |
| checkpoint 只依赖 run 正常收尾删除 | 启动与周期巡检清理终态、过期无主 thread | 进程崩溃和删除失败需要兜底 |
| 工具参数和结果只保存在事件摘要与 `run_step.output_json` | 每次执行独立写入 `tool_call_audit` | 事件与节点输出都不是工具级审计账本，缺少稳定关联、脱敏结果和保留策略 |
| 所有管理操作共用一个环境管理员 Key | 环境 Key 负责引导，日常使用数据库管理员、角色和租户 | 需要可撤销、可轮换、可归属到具体操作人的身份 |
| Skill 删除即抹掉内容 | 归档并保留资源与修订，恢复后先进入草稿 | 线上行为不能因历史清理静默改变，误删也必须可恢复 |
| Skill 二进制资源只能记录摘要 | 修订资源独立保存原始字节，可逐字节回滚 | “可回滚”必须覆盖真实内容，而不只是证明内容曾存在 |
| 单审批人一次点击决定流程 | 会签、去重投票、截止时间和 fail-closed 超时策略 | 多人审批需要原子计数，超时必须可预测 |
| gRPC 取消只停在 studio | 取消传播到 dispatcher，并强删一次性容器 | 调用方放弃后不能继续占用宿主资源 |
| MCP 只发现 tools | 支持 resources、resource templates、资源读取、prompts 与目录变更通知 | MCP 服务的能力不应被客户端截断 |
| 镜像签名验证 | 明确不做；继续使用镜像目录白名单、最小基础镜像和生产 digest 固定 | 用户明确不需要签名体系，避免再把它列为 TODO |
| 客户端提交 `user_id` 决定会话归属 | 会话只绑定服务端 session 派生的不可变 `app_user.id`，无匿名降级；越权统一 404 | 可伪造身份会直接破坏历史隔离与审批边界 |
| 浏览器保存 OAuth access/refresh token | 登录只建立 HttpOnly 服务端会话，第三方 token 默认不落库、不下发浏览器 | 降低 XSS 泄漏面，并让会话可以服务端立即撤销 |
| 所有管理员只走 OAuth，或所有管理员都使用本地密码 | OAuth 管理员使用 Casbin；另设独立 `super_admin_account` 提供内置及后续平台超级管理员 | 同时满足常规 OAuth 管理与平台级应急管理 |
| 手工写死管理员 role | 使用 Casbin `p/g`、permission metadata 和 OAuth 角色映射 | 权限可审计、可组合，外部目录是人员角色的权威来源 |
| 后端只提供固定管理页面 | Ludic SSR 注册页面/组件，数据库配置 permission binding 和租户覆盖 | 在不允许任意代码的前提下支持页面和组件级 UI 授权 |
| 后台和客户端共用同一 Web 进程 | `/admin` 独立 admin-web 容器，客户端不注册管理页面；共享数据库和领域语义 | 隔离发布、Session、静态资源、依赖和运行故障 |
| 用户名密码管理员登录 | 明确不做；OAuth 是唯一人员登录协议 | 不维护另一套密码、重置、锁定和高危本地账户体系 |

## 阅读路径

- 查当前功能和启动方式：先读 [app/README.md](../../app/README.md)，再按需进入版本页。
- 查某项能力为什么这样设计：从下表的“设计文档”进入。
- 准备继续开发：先读 [TODO.md](TODO.md)，不要从历史 implementation plan 继续勾选。
- 修改 gRPC 契约：只改 `proto/` 契约源，再同步 V4 调用侧与 studio 服务侧的设计片段。

## 按任务定位

| 任务 | 设计文档 |
| --- | --- |
| 数据库、SSE、基础 run 生命周期 | [V1](../../app/design/v1-foundation.md) |
| Redis 多实例、租约、LangGraph | [V2](../../app/design/v2-multi-instance.md) |
| DAG、审批、工具、敏感过滤 | [V3](../../app/design/v3-orchestration.md) |
| MCP、Skill、脚本执行调用侧 | [V4](../../app/design/v4-tools-skills.md) |
| Skill 管理后台、运行期资源回收 | [V4.2](../../app/design/v4.2-admin-maintenance.md) |
| 工具审计、结果回看、脱敏与保留期 | [V4.3](../../app/design/v4.3-tool-audit.md) |
| 管理员、租户、会签、MCP 资源、运行期治理 | [V4.4](../../app/design/v4.4-governance-runtime.md) |
| 备份恢复、seccomp、共享配额、成本与 MCP OAuth | [V4.5](../../app/design/v4.5-recovery-governance.md) |
| 用户登录、Cookie 会话和历史按用户隔离 | [V4.6](../../app/design/v4.6-user-oauth-conversation.md) |
| 独立 admin-web、OAuth 管理员、账号密码超级管理员、Casbin 与 Ludic SSR | [V4.7](../../app/design/v4.7-oauth-admin.md) |
| studio 协议、隔离、资源限制 | [studio V4](../../services/studio/design/v4-current.md) |
| 下一步开发 | [TODO](TODO.md) |

## 维护规则

1. 新能力先判断属于现有版本修正，还是需要新版本。新版本新增独立文件，不改写旧版本已经发生的事实。
2. 被替换或取消的方案写进本页“已移除、已替换或明确不做”，同时说明替代方案。
3. 未完成事项只写进 [TODO.md](TODO.md)。版本设计正文可以解释限制，但不要维护第二份待办清单。
4. 完成后从 TODO 删除，并把实施结论写回对应版本页或当前说明。
5. 历史计划目录只保留过程记录；若与当前设计冲突，以本页、版本页、proto 和测试为准。
