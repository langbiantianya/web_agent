"""用 LangGraph 表达 workflow 的执行图（设计文档 §6.2，V2）。

V1 的编排是一个手写循环：``for node in definition.linear_order()``。它在 V1 的
「线性流水线」约束下是对的，但一旦要做条件路由、DAG、人工审批，就得重写这个循环
——而这类图编排恰恰是 LangGraph 的强项。

## 边界：LangGraph 只做控制流，不做持久化

**刻意不挂 checkpointer。** 本服务已经有自己的、经过验证的事件日志：

- Redis Stream（V2）——回放源，emit 时同步写入；
- MySQL ``run_event``——长期归档。

若再挂 LangGraph 的 checkpointer，就会出现**两个真相来源**：节点状态存一份、
事件序列存一份，两者可能不一致，而且续传要同时读两个地方。LangGraph 换来的
是控制流表达力，不是持久化需求，所以这里只要控制流。

## 为什么仍然保留节点内的重试与取消检查

图引擎负责「下一个节点是谁」，而「这个节点怎么跑」仍然由编排器决定：

- **重试/退避**留在节点函数里。LangGraph 的重试语义（``RetryPolicy``）与我们要的
  「指数退避 + 写 run_step + 取消时立即中止」并不等价，自己写更可控。
- **取消检查**每个节点开头做一次。``hub.is_cancelled`` 只读进程内布尔字段，不 await，
  在这里检查的代价可以忽略；而放到图外面就没有「进入下一个节点前」的统一拦截点了。

## 为 V3 留的口子

图的连线由 ``definition`` 的 ``edges`` 生成，路由函数也单独抽出来。将来要做条件边
或 DAG 时，改的是 :func:`build_run_graph` 的连线部分和配置期校验，**节点执行逻辑
一行都不用动**。
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph

from app.core.errors import RunCancelled
from app.core.workflow_def import WorkflowDefinition, WorkflowNode
from app.models.enums import EventType

logger = logging.getLogger(__name__)


class RunState(TypedDict):
    """图在节点之间传递的状态。

    只放**跨节点流动的数据**。每 run 固定不变的量（run_id、agents、hub……）不进
    状态：它们由闭包持有，进状态只会让类型签名变脏，还会在每次节点返回时被整体
    复制一遍。

    字段用「整体替换」而非追加语义——每个节点返回自己那一份完整的新值，逻辑最直白，
    也不需要给 list 配 ``Annotated`` reducer。
    """

    context: list[dict]
    upstream: dict[str, str]
    usage: dict[str, int]


class NodeOps(Protocol):
    """编排器提供给图节点的动作集合。

    刻意用 Protocol 而不是直接依赖 :class:`~app.core.orchestrator.Orchestrator`：
    本模块被编排器导入，反向依赖会成环。同时它让节点逻辑可以被单独测试。
    """

    async def set_current_node(self, run_id: int, node_id: str) -> None:
        """把 run 的当前节点写进 ``run.current_node``。"""
        ...

    async def run_with_retry(
        self,
        run_id: int,
        node: WorkflowNode,
        agent: Any,
        context: list[dict],
        rendered_input: str,
    ) -> Any:
        """跑一个节点（含重试与退避），返回带 ``.text`` / ``.usage`` 的结果。"""
        ...

    async def persist_message(
        self, run_id: int, conversation_id: int, agent_code: str, text: str
    ) -> None:
        """把节点产出落成 assistant 消息，供后续节点当上下文。"""
        ...


class NodeRunner:
    """一次 run 的逐节点执行器：每个 LangGraph 节点闭包调用它。

    把「节点要干什么」和「图怎么连」分开：前者在这里，后者由
    :func:`build_run_graph` 负责。换图结构不会碰到执行逻辑，反过来也一样。
    """

    def __init__(
        self,
        *,
        run_id: int,
        conversation_id: int,
        definition: WorkflowDefinition,
        agents: dict[str, Any],
        user_input: str,
        hub: Any,
        ops: NodeOps,
    ) -> None:
        """绑定本次 run 的执行期依赖。

        Args:
            run_id: 本次 run 的 id（事件与租约都用它）。
            conversation_id: 会话 id，落消息时需要。
            definition: 已通过配置期校验的 workflow 定义。
            agents: agentCode → Agent 行。
            user_input: 本次用户输入，用于渲染 inputTemplate。
            hub: 事件出口，同时提供取消探针。
            ops: 编排器提供的三个动作。
        """
        self.run_id = run_id
        self.conversation_id = conversation_id
        self.definition = definition
        self.agents = agents
        self.user_input = user_input
        self.hub = hub
        self.ops = ops

    async def execute_node(self, node: WorkflowNode, state: RunState) -> dict:
        """执行一个节点，返回状态增量。

        顺序与 V1 手写循环逐条对齐，**不要调换**：

        1. 先查取消——被取消的节点绝不该花钱；
        2. ``node_start`` 事件 → 写 ``run.current_node``；
        3. 渲染输入 → 带重试地跑；
        4. 累计 usage、记 ``upstream``；
        5. ``node_end`` 事件；
        6. 产出落成 assistant 消息 → 追加进上下文。

        第 6 步在第 5 步之后：``node_end`` 表示这个节点真的完了，事件先于落库，
        客户端看到节点结束就认为可以读最终消息了。
        """
        if self.hub.is_cancelled(self.run_id):
            raise RunCancelled("用户取消")

        agent = self.agents[node.agentCode]
        await self.hub.emit(
            self.run_id,
            EventType.NODE_START,
            {"node": node.id, "agentCode": node.agentCode, "model": agent.model},
        )
        await self.ops.set_current_node(self.run_id, node.id)

        rendered = self.definition.render_input(self.user_input, state["upstream"])
        result = await self.ops.run_with_retry(
            run_id=self.run_id,
            node=node,
            agent=agent,
            context=state["context"],
            rendered_input=rendered,
        )

        usage = dict(state["usage"])
        usage["prompt_tokens"] = usage.get("prompt_tokens", 0) + result.usage.get(
            "prompt_tokens", 0
        )
        usage["completion_tokens"] = usage.get("completion_tokens", 0) + result.usage.get(
            "completion_tokens", 0
        )

        output_key = node.outputKey or node.id
        upstream = dict(state["upstream"])
        upstream[output_key] = result.text

        await self.hub.emit(
            self.run_id,
            EventType.NODE_END,
            {"node": node.id, "outputKey": output_key, "agentCode": node.agentCode},
        )

        await self.ops.persist_message(
            self.run_id, self.conversation_id, node.agentCode, result.text
        )
        context = state["context"] + [{"role": "assistant", "content": result.text}]

        return {"context": context, "upstream": upstream, "usage": usage}

    def make_node_fn(self, node: WorkflowNode):
        """为一个 workflow 节点生成 LangGraph 节点函数（闭包绑住 ``node``）。"""

        async def _run(state: RunState) -> dict:
            """LangGraph 调用的入口，转交给 :meth:`execute_node`。"""
            return await self.execute_node(node, state)

        _run.__name__ = f"node_{node.id}"
        return _run


def build_run_graph(definition: WorkflowDefinition, runner: NodeRunner):
    """按 definition 连线，编译出本次 run 的执行图。

    连线直接来自 ``edges``（而不是假设「按 nodes 顺序串起来」），这样将来换成
    分叉或条件边时，改的只有这一个函数——``execute_node`` 一行都不用动。

    节点名用 ``node.id``，它同时是事件里的 ``node`` 字段、``upstream`` 的键和
    ``run_step`` 的外键，用它当图节点名能让日志里的图和业务数据对得上。

    Args:
        definition: 已通过配置期校验的 workflow 定义。
        runner: 本次 run 的节点执行器。

    Returns:
        编译好的 LangGraph，可 ``await graph.ainvoke(state, config)``。
    """
    builder = StateGraph(RunState)
    for node in definition.nodes:
        builder.add_node(node.id, runner.make_node_fn(node))

    # entry 优先，否则按拓扑序的第一个。走 ``linear_order()`` 而不是内部的
    # ``_topo_order()``：它会再挡一次分叉，作为纵深防御——万一某条路径绕过配置期
    # 校验把 DAG 塞了进来，这里就会失败，而不是安静地只跑一条分支。
    order = definition.linear_order()
    entry = definition.entry_id
    builder.add_edge(START, entry)

    # 只为「确实有上游边」的节点连边：单节点流程没有任何 edge，
    # 若按 nodes 顺序硬连反而会造出一条 definition 里并不存在的依赖。
    linked = {e.to for e in definition.edges}
    for node in order[1:]:
        if node.id in linked:
            builder.add_edge(_predecessor(node.id, definition), node.id)

    # 链尾节点连到 END。线性流水线里「没有下游边」即为链尾。
    has_downstream = {e.from_ for e in definition.edges}
    for node in order:
        if node.id not in has_downstream:
            builder.add_edge(node.id, END)

    compiled = builder.compile()
    logger.info(
        "run 图已构建：entry=%s，%s 个节点，%s 条边，顺序 %s",
        entry,
        len(order),
        len(definition.edges),
        " -> ".join(n.id for n in order),
    )
    return compiled


def _predecessor(node_id: str, definition: WorkflowDefinition) -> str:
    """返回一个节点的唯一上游节点 id。

    配置期已拒绝分叉，所以入度最多为 1，这里取第一条即可。找不到上游时返回它自己，
    交由 LangGraph 报「节点不存在」这种更直白的错。
    """
    for e in definition.edges:
        if e.to == node_id:
            return e.from_
    return node_id


def recursion_limit(definition: WorkflowDefinition) -> int:
    """给 ``ainvoke`` 的 ``recursion_limit``。

    LangGraph 按「超步」计数，线性链每节点一超步，所以节点数 + 余量足够。留 10 的
    余量是为了将来加条件边时不必回来改这里——触发这个上限意味着图里出现了环，
    早失败比跑出重复执行要好。
    """
    return len(definition.nodes) + 10
