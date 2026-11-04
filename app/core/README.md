# 核心编排与运行时

`app/core/` 包含主服务最关键的执行逻辑：LangGraph 工作流、模型调用、工具分发、事件广播、断线续传、审批暂停点、Redis 多实例协作和 Skill Runtime。

## 执行链路

```text
Orchestrator
  -> LangGraph StateGraph
  -> AgentRunner
  -> LLM / 工具分发
  -> RunHub（seq + 广播）
  -> EventWriter（批量落库）
  -> SSEWriter（实时 + 回放合并）
```

`POST /messages` 只负责创建 run；生成任务由后台协程执行，因此客户端断开不会终止生成。节点失败按指数退避重试，仍失败时写入 `error` 事件并结束 run。

## Workflow DAG

Workflow 的 `definition` 包含 `entry`、`nodes`、`edges` 和可选 `inputTemplate`。同一节点的多条无条件出边表示并行分叉，多条无条件入边表示等待全部上游完成后再汇聚。

```json
{
  "entry": "root",
  "nodes": [
    {"id": "root", "agentCode": "planner", "outputKey": "plan"},
    {"id": "left", "agentCode": "researcher", "outputKey": "left_out"},
    {"id": "right", "agentCode": "researcher", "outputKey": "right_out"},
    {"id": "join", "agentCode": "writer", "outputKey": "final"}
  ],
  "edges": [
    {"from": "root", "to": "left"},
    {"from": "root", "to": "right"},
    {"from": "left", "to": "join"},
    {"from": "right", "to": "join"}
  ],
  "inputTemplate": "问：{{user_input}}\n计划：{{plan}}\n左：{{left_out}}\n右：{{right_out}}"
}
```

条件边使用 `when` 表达式。表达式解析器不调用 `eval`，只支持比较、`and/or/not`、括号以及 `len/contains/lower/is_empty` 白名单函数。环、孤立节点、非法汇聚和越界模板变量在保存时就会被拒绝。

## 审批与工具

- `type: "approval"` 的节点通过 LangGraph `interrupt()` 暂停。
- 暂停点由 `checkpoint.py` 保存；批准后从暂停节点继续，前面的节点不会重跑。
- `llm_endpoints.py` 每个节点从数据库按优先级加载启用的 OpenAI 兼容接口；
  `AgentRunner` 在尚未输出内容时自动切换到下一接口。
- 模型请求的内置工具、Skill 资源和 MCP 工具都先经过 `agent.tools` 白名单。
- `skill_resource` 从数据库读取技能资源；`skill_exec` 通过 gRPC 调用独立 studio，主进程不执行技能脚本。
- MCP 支持 stdio 与 Streamable HTTP，工具名格式为 `mcp__<server>__<tool>`。
- 工具失败会作为 `tool_result` 回灌给模型，通常不会直接失败整个 run。

## 事件与多实例

每条事件由 `RunHub` 分配单调递增的 `seq`。`EventWriter` 按 20 条或 50ms 批量写入 `run_event`，避免每个 token 单独写数据库。

开启 Redis 后：

- Pub/Sub 负责跨实例实时广播。
- Redis Stream 负责事件回放。
- 归属租约记录 run 由哪个实例执行并持续续期。
- 控制频道传播取消信号。
- 新实例启动只回收无主 run，不误杀其他实例仍在执行的任务。

没有 Redis 时使用进程内总线和数据库回放，适合单实例开发。

## 敏感内容前置过滤

`sensitive.py` 在调用 LLM 前执行字符 n-gram 与逻辑回归分类。命中后返回 `422`，不创建 run，也不调用模型。工具输出回灌前也会经过同一过滤边界。

## 源码索引

| 文件 | 职责 |
| --- | --- |
| `orchestrator.py` | run 生命周期、节点调度、审批恢复 |
| `run_graph.py` | LangGraph 图构建、节点执行和审批节点 |
| `agent_runner.py` | OpenAI 兼容流式调用与工具闭环 |
| `llm_endpoints.py` | 数据库 LLM 接口优先级与兼容回退 |
| `tools.py` | 内置工具注册表和动态工具分发 |
| `run_hub.py` | seq 分配、广播和取消 |
| `event_writer.py` | 事件批量落库 |
| `event_log.py` | 数据库与 Redis Stream 回放抽象 |
| `sse_writer.py` | 实时事件和回放合并去重 |
| `bus.py` | 进程内与 Redis Pub/Sub 总线 |
| `lease.py` | Redis 归属租约与控制频道 |
| `checkpoint.py` | SQLite / Redis 审批暂停点 |
| `skill_*.py` | Skill 解析、导入、提示词注入和资源读取 |
| `mcp/` | MCP 传输、连接管理和命名映射 |
| `studio.py`、`skill_exec.py` | studio gRPC 客户端及执行前校验 |

## 相关文档

- [API 与 SSE 协议](../api/README.md)
- [版本化主设计目录](../design/README.md)
- [studio 执行服务](../../services/studio/README.md)
