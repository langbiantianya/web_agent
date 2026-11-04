# 设计 TODO

这里只记录**尚未完成**的工作。已完成项保留在版本设计和历史实施计划中，不在这里重复维护。

优先级：`P0` 阻断生产采用，`P1` 重要缺口，`P2` 体验与扩展。

## 工作室与隔离执行

- [ ] **P0：Kubernetes Job 后端**
  实现 `K8sJobBackend`、最小权限 RBAC、Job/Pod 固定模板、日志读取、OOM/驱逐判定和删除回收。当前只有 Docker Phase 1。

- [ ] **P0：真实 K8s 集群验证**
  验证 NetworkPolicy 真正阻断出向、headless Service + `round_robin` 能摊平流量、drain 与 `terminationGracePeriodSeconds` 配合、PodSecurity 与 ResourceQuota 生效。

- [ ] **P1：取消传播到 sandbox**
  gRPC 取消目前不保证结束 dispatcher/sandbox。需要定义取消协议，并验证取消后 5 秒内容器消失。

- [ ] **P1：供应链与更强运行时隔离**
  镜像目录中的 ref 固定到 digest；评估 cosign 校验、自定义 seccomp、gVisor 或 Kata；补准入策略，禁止目录外镜像与特权参数。

- [ ] **P1：sandbox trace 传播**
  将 W3C `traceparent` 带入 payload、runner 日志与 studio/dispatcher 调用，形成跨服务链路。

- [ ] **P1：多级背压与配额**
  当前有 studio isolated 池和 dispatcher in-flight，但没有命名空间配额、技能级配额、预算和预热池。需要补齐容量治理。

- [ ] **P1：sandbox 阶段指标**
  当前有 studio 执行指标，但还缺少独立的 `sandbox_start_seconds`、`sandbox_total{phase}` 与成本指标，难以单独分析容器启动和失败阶段。

- [ ] **P1：isolated 延迟提示**
  `skill_exec` 的工具描述需要明确提示 isolated 脚本可能耗时数秒，避免模型把强隔离档用于毫秒级纯计算。

## MCP

- [ ] **P1：Streamable HTTP 真实端到端验证**
  传输与 SSRF 防护已有单测，但尚未连接真实 Streamable HTTP MCP 服务器完成发现、调用和错误回灌。

- [ ] **P1：空闲 MCP 连接周期回收**
  `close_idle()` 已存在，但没有定时任务调用。需要接入应用生命周期或定时调度，避免 stdio 子进程长期驻留。

- [ ] **P1：多实例在线状态一致性**
  当前 `mcp_manager` 的在线/不可用标记是进程内的。多实例下后台状态可能不一致，需要共享状态或明确改成实例级展示。

- [ ] **P2：MCP resources / prompts**
  当前只接 tools；需要补资源列表、读取、prompt 获取及相应权限模型。

- [ ] **P2：MCP OAuth**
  当前远程服务器只支持静态 header。若接入 OAuth 服务器，需要授权流、令牌刷新和密钥存储设计。

- [ ] **P2：动态工具清单通知**
  当前工具目录是快照，尚未处理 `notifications/tools/list_changed` 或订阅事件。

- [ ] **P2：stdio stdout 污染隔离测试**
  需要验证 MCP 子进程把普通日志误写到 stdout 时，JSON-RPC 流仍不被破坏或能明确判定连接不可用。

## Skill

- [ ] **P1：Skill 管理后台**
  API 已完整，但管理页还没有创建、编辑、导入、导出、绑定、修订回滚界面。

- [ ] **P1：运行档与导入风险可视化**
  后台展示 `runtime-profile`、`runtime-image` 的当前可用性；导入期提示新增第三方 import、网络访问或 shebang 与扩展名不一致的脚本。

- [ ] **P1：`allowed-tools` 提示**
  技能声明了 Agent 白名单之外的工具时，后台应提示；授权仍只以 `agent.tools` 为准。

- [ ] **P1：管理员用户体系**
  当前管理写操作依赖共享 `X-Admin-Key`，审计作者只能取固定值。需要用户身份、角色和按用户记录的审计来源。

- [ ] **P2：草稿态与软删除**
  当前没有“编辑中未发布”状态，删除技能是硬删除。需要先定义发布、回滚和数据保留语义。

- [ ] **P2：二进制资源回滚**
  当前修订快照只保存二进制摘要。若需要真正回滚二进制，需要评估存储成本与对象存储方案。

- [ ] **P2：多租户隔离**
  Skill 当前没有租户维度，定位仍是单部署多 Agent。

## 编排与数据

- [ ] **P1：审批超时语义**
  配置项已预留，但“超时通过、超时驳回或自动升级”尚未定义，不能启用。

- [ ] **P1：会签与多审批人**
  当前只有一个决策人。需要调整 `run_approval` 唯一约束、投票规则和事件模型。

- [ ] **P1：checkpoint 孤儿清理**
  进程崩溃可能留下 Redis/SQLite thread。需要周期巡检与保留策略。

- [ ] **P1：工具审计账本**
  工具参数和结果目前只在事件流与 `run_step.output_json` 中，缺少独立审计表和长期查询接口。

- [ ] **P1：工具结果持久化策略**
  run 结束后回看时看不到完整工具返回。需要定义脱敏、保留期和敏感输出过滤。

- [ ] **P2：工具级权限与并发执行**
  当前工具权限是全局白名单，模型一轮请求多个工具时串行执行。

## 数据与部署

- [ ] **P0：真实 PostgreSQL 服务器验证**
  当前 PostgreSQL 兼容性由离线 DDL 和假连接验证，尚未在真实 PG 上跑迁移、并发与恢复测试。

- [ ] **P1：PgBouncer 事务池部署说明与验证**
  事务池可能忽略连接启动 `options`，需要验证时区配置并补 DBA 配置指导。

## 测试基础设施

- [ ] **P1：gRPC 测试隔离**
  Windows 上全量 pytest 在同一进程混跑普通测试和 studio 子进程测试时会出现 fork 污染。需要分进程运行 studio 用例，或把 `GRPC_ENABLE_FORK_SUPPORT=0` 固化到测试入口。
