"""用 LangGraph 表达 workflow 的执行图（设计文档 §6.2，V2 起）。

V1 的编排是一个手写循环：``for node in definition.linear_order()``。它在 V1 的
「线性流水线」约束下是对的，但一旦要做条件路由、DAG、人工审批，就得重写这个循环
——而这类图编排恰恰是 LangGraph 的强项。

## 边界：LangGraph 只做控制流，不做持久化

**刻意不挂 checkpointer。** 本服务已经有自己的、经过验证的事件日志：

- Redis Stream（V2）——回放源，emit 时同步写入；
- MySQL ``run_event``——长期归档。

若再挂 LangGraph 的 checkpointer，就会出现**两个真相来源**：节点状态存一份、
事件序列存一份，两者可能不一致，而且续传要同时读两个地方。LangGraph 换来的
是控制流表达力，不是持久化需求，所以这里只要控制流。（人工审批需要
checkpointer，那是 V4 的事，届时它只负责「暂停在哪」，不碰对外事件。）

## 为什么仍然保留节点内的重试与取消检查

图引擎负责「下一个节点是谁」，而「这个节点怎么跑」仍然由编排器决定：

- **重试/退避**留在节点函数里。LangGraph 的重试语义（``RetryPolicy``）与我们要的
  「指数退避 + 写 run_step + 取消时立即中止」并不等价，自己写更可控。
- **取消检查**每个节点开头做一次。``hub.is_cancelled`` 只读进程内布尔字段，不 await，
  在这里检查的代价可以忽略；而放到图外面就没有「进入下一个节点前」的统一拦截点了。

## V3：DAG 与条件边

两条扩展线都落在本文件与 :mod:`app.core.workflow_def` 里，
:meth:`NodeRunner.execute_node` 一行都没动——这正是 V2-b 拆分时要的效果：

- **DAG**：无条件出边逐条 ``add_edge``（并行分叉），多入边自动汇聚。
- **条件边**：整组交给 ``add_conditional_edges``，路由函数在
  :func:`_make_router` 里逐条求值。
- **状态合并器**：并行分支的多个节点会在同一超步返回增量，
  :class:`RunState` 上的 reducer 是硬性要求，缺了会静默丢产出。
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph

from app.core.errors import RunCancelled
from app.core.expr import evaluate
from app.core.workflow_def import WorkflowDefinition, WorkflowNode
from app.models.enums import EventType

logger = logging.getLogger(__name__)


def _merge_context(left: list[dict], right: list[dict]) -> list[dict]:
    """``context`` 的 reducer：拼接两段对话。

    DAG 的并行分支会同时返回状态增量，没有 reducer 的话后写的会覆盖先写的——
    表现为「一条分支的产出凭空消失」。串行时这个 reducer 退化成 append，行为与
    V2 完全一致。
    """
    return (left or []) + (right or [])


def _merge_upstream(left: dict[str, str], right: dict[str, str]) -> dict[str, str]:
    """``upstream`` 的 reducer：按 key 合并。

    并行分支产出不同的 outputKey（配置期已保证 id 唯一），所以正常情况下不会撞
    key。真撞了时以 ``right`` 为准——它是最新的写入。
    """
    merged = dict(left or {})
    merged.update(right or {})
    return merged


def _merge_usage(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    """``usage`` 的 reducer：逐 key 相加。

    并行分支各花各的 token，直接相加才是总量；串行时等价于原来在
    :meth:`NodeRunner.execute_node` 里手工累加的写法。
    """
    merged = dict(left or {})
    for k, v in (right or {}).items():
        merged[k] = merged.get(k, 0) + v
    return merged


class RunState(TypedDict):
    """图在节点之间传递的状态。

    只放**跨节点流动的数据**。每 run 固定不变的量（run_id、agents、hub……）不进
    状态：它们由闭包持有，进状态只会让类型签名变脏，还会在每次节点返回时被整体
    复制一遍。

    三个字段都带 **reducer**，这是 DAG 的硬性要求：并行分支的多个节点会在同一个
    超步里各自返回一份状态增量，没有 reducer 时 LangGraph 只能「后者覆盖前者」，
    表现为一条分支的产出凭空消失，而 run 仍以 done 正常收尾——这种丢失极难察觉。
    串行执行时 reducer 退化成原来的「整体替换」，行为不变。
    """

    context: Annotated[list[dict], _merge_context]
    upstream: Annotated[dict[str, str], _merge_upstream]
    usage: Annotated[dict[str, int], _merge_usage]


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

        output_key = node.outputKey or node.id

        await self.hub.emit(
            self.run_id,
            EventType.NODE_END,
            {"node": node.id, "outputKey": output_key, "agentCode": node.agentCode},
        )

        await self.ops.persist_message(
            self.run_id, self.conversation_id, node.agentCode, result.text
        )

        # 返回的是**增量**而不是「基于当前 state 累加后的全量」——这是 reducer 的
        # 契约。全量 + reducer 会重复计数：串行下两个节点就把 usage 加了两轮，
        # 且数字随节点数线性膨胀；并行下更糟，两条分支各自返回全量会互相叠加。
        # 实测 2 节点各花 10/5 时，全量写法得到 30/15（应为 20/10）。
        return {
            "context": [{"role": "assistant", "content": result.text}],
            "upstream": {output_key: result.text},
            "usage": dict(result.usage),
        }

    def make_node_fn(self, node: WorkflowNode):
        """为一个 workflow 节点生成 LangGraph 节点函数（闭包绑住 ``node``）。"""

        async def _run(state: RunState) -> dict:
            """LangGraph 调用的入口，转交给 :meth:`execute_node`。"""
            return await self.execute_node(node, state)

        _run.__name__ = f"node_{node.id}"
        return _run


def build_run_graph(definition: WorkflowDefinition, runner: NodeRunner):
    """按 definition 连线，编译出本次 run 的执行图（V3：支持 DAG + 条件边）。

    两种连线方式，按节点分开处理：

    - **无条件出边**：逐条 ``add_edge``。多个出边就是并行分叉，多个入边就是汇聚
      ——LangGraph 会把入度已满足的节点放进同一个超步并发跑。
    - **条件出边**：整组交给 ``add_conditional_edges``，由路由函数逐条求值决定
      去哪儿。一条都不成立时返回 ``END``，否则这条分支会悬着不结束。

    同一节点的出边不会条件与无条件混用——那是 :meth:`WorkflowDefinition.
    _check_outgoing_uniform` 在配置期就拒掉的，所以这里不用处理混合情况。

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

    order = definition.topo_order()
    entry = definition.entry_id
    builder.add_edge(START, entry)

    outgoing: dict[str, list] = {n.id: [] for n in definition.nodes}
    for e in definition.edges:
        outgoing[e.from_].append(e)

    for node_id, edges in outgoing.items():
        if not edges:
            # 链尾：没有下游边，直接结束。
            builder.add_edge(node_id, END)
        elif edges[0].when:
            targets = [e.to for e in edges]
            builder.add_conditional_edges(
                node_id,
                _make_router(edges, runner.user_input),
                [*targets, END],
            )
        else:
            # 无条件出边。逐条 add_edge 而非 add_edge(list)：后者是「全部满足才
            # 走」，前者才是并行分叉——两者语义完全相反，这里要的是后者。
            for target in edges:
                builder.add_edge(node_id, target.to)

    compiled = builder.compile()
    logger.info(
        "run 图已构建：entry=%s，%s 个节点，%s 条边（其中条件边 %s 条），拓扑序 %s",
        entry,
        len(order),
        len(definition.edges),
        sum(1 for e in definition.edges if e.when),
        " -> ".join(n.id for n in order),
    )
    return compiled


def _make_router(edges: list, user_input: str):
    """为一个节点的条件出边生成路由函数。

    路由函数在节点跑完之后被 LangGraph 调用，拿到最新 state，返回下一个节点名
    的列表。返回空列表会让图卡住，所以这里兜底成 ``[END]``——条件全不成立
    就是「这条路不用走了」，而不是出错。

    条件表达式在配置期已做过语法与变量校验（见
    :meth:`WorkflowDefinition._validate_conditions`），运行期再抛
    :class:`~app.core.expr.ExprError` 就意味着校验漏了，让它原样冒泡比静默走
    else 分支好排查得多。
    """

    async def router(state: RunState) -> list[str]:
        """逐条求值出边条件，返回命中的下游节点。"""
        scope = {"upstream": state.get("upstream") or {}, "user_input": user_input}
        chosen = [e.to for e in edges if evaluate(e.when, scope)]
        return chosen if chosen else [END]

    return router


def recursion_limit(definition: WorkflowDefinition) -> int:
    """给 ``ainvoke`` 的 ``recursion_limit``。

    LangGraph 按「超步」计数。DAG 下每个节点最多跑一次，超步数不会超过节点数，
    所以仍是 ``len(nodes) + 余量``。留 10 的余量是为了将来加循环/审批时不必回来
    改——触发这个上限意味着图里出现了环，早失败比跑出重复执行要好。
    """
    return len(definition.nodes) + 10
