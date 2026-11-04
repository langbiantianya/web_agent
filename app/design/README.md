# web_agent 主服务设计索引

主设计按版本拆分。当前系统由 V1–V4 累积组成；V4 页不重复粘贴早期章节，因此排障或修改基础协议时要回到对应版本。

| 版本 | 文档 | 主要内容 |
| --- | --- | --- |
| V1 | [单实例基础版](v1-foundation.md) | 目标、架构、持久化、SSE、线性流水线 |
| V2 | [多实例与 LangGraph](v2-multi-instance.md) | Redis、租约、Stream 回放、LangGraph |
| V3 | [编排、审批、工具与安全](v3-orchestration.md) | DAG、审批、工具、PG、敏感过滤 |
| V4 | [MCP、Skill 与脚本执行](v4-tools-skills.md) | MCP、Skill、studio 调用侧 |

跨项目版本历史、已移除方案和任务导航见 [设计总目录](../../docs/design/README.md)。未完成事项只在 [TODO](../../docs/design/TODO.md) 中维护。

## 阅读建议

- 第一次了解系统：先读 [app/README.md](../README.md)，再读 V1 和 V4。
- 排查 run/SSE：读 V1、V2 与 V3。
- 修改 Workflow：读 V1、V2-b、V3。
- 接入 MCP 或 Skill：读 V3.2、V4。
- 修改 studio 契约：同时读 [V4 调用侧](v4-tools-skills.md) 和 [studio 服务侧](../../services/studio/design/v4-current.md)。
