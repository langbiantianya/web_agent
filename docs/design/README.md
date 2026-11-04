# 设计文档总目录

这里是项目设计文档的唯一入口。设计按产品迭代版本拆分，先读本页确认版本边界，再进入对应版本；未完成事项统一维护在 [TODO.md](TODO.md)，不再散落在各版本正文中。

## 当前状态

| 项 | 当前版本 | 状态 |
| --- | --- | --- |
| web_agent 主服务 | V4 | 已实现 |
| MCP 工具 | V4 | 已实现 |
| Skill 指令包与资源工具 | V4 | 已实现 |
| studio fast 档 | V4 | 已实现并验证 |
| studio isolated Docker 档 | V4 / Phase 1 | 已实现并在真 Docker 验证 |

代码、proto 和自动化测试是最终事实来源；设计文档解释边界、取舍和演进原因。若文档与可执行契约冲突，以 `proto/` 与测试为准，并同步修正文档。

## 文档地图

### 主服务

| 文档 | 内容 |
| --- | --- |
| [主服务设计索引](../../app/design/README.md) | V1–V4 的阅读顺序与章节映射 |
| [V1：单实例基础版](../../app/design/v1-foundation.md) | 目标、架构、持久化、SSE、线性 Agent 流水线 |
| [V2：多实例与 LangGraph](../../app/design/v2-multi-instance.md) | Redis Stream/Pub/Sub、租约、LangGraph 替换自研控制流 |
| [V3：编排、审批、工具与安全](../../app/design/v3-orchestration.md) | DAG、条件边、人工审批、工具闭环、PostgreSQL、敏感内容过滤 |
| [V4：MCP、Skill 与脚本执行](../../app/design/v4-tools-skills.md) | MCP、Skill、studio 调用侧、真实验收 |

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
| MCP `resources` / `prompts` | 当前只接 tools | 尚未实现，已列入 TODO，不属于“已完成” |
| MCP OAuth | 当前只用静态 header | 尚未实现，已列入 TODO |

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
| studio 协议、隔离、资源限制 | [studio V4](../../services/studio/design/v4-current.md) |
| 下一步开发 | [TODO](TODO.md) |

## 维护规则

1. 新能力先判断属于现有版本修正，还是需要新版本。新版本新增独立文件，不改写旧版本已经发生的事实。
2. 被替换或取消的方案写进本页“已移除、已替换或明确不做”，同时说明替代方案。
3. 未完成事项只写进 [TODO.md](TODO.md)。版本设计正文可以解释限制，但不要维护第二份待办清单。
4. 完成后从 TODO 删除，并把实施结论写回对应版本页或当前说明。
5. 历史计划目录只保留过程记录；若与当前设计冲突，以本页、版本页、proto 和测试为准。
