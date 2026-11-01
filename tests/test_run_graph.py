"""LangGraph 执行图的构建与执行（V2 编排引擎）。

重点验证「迁移没有改变行为」这件事，而不是 LangGraph 自己能不能跑：

- **节点顺序由 definition.edges 决定**，不是 nodes 的声明顺序——这是用图表达
  编排的真正价值所在，线性顺序恰好也能跑通的实现会在这一步露馅；
- 状态（upstream / context / usage）能沿着边流到下游节点；
- 节点抛出的异常**原样穿透**图引擎——编排器的失败分支完全依赖这一点，
  一旦被图引擎吞掉或包一层，错误就会变成「run 静默结束」；
- 单节点流程没有 edge，必须照样能构图。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from app.core.errors import RunCancelled
from app.core.run_graph import NodeRunner, RunState, build_run_graph, recursion_limit
from app.core.workflow_def import WorkflowDefinition, WorkflowNode


@dataclass
class FakeResult:
    """``AgentResult`` 的最小替身。"""

    text: str
    usage: dict[str, int] = field(default_factory=lambda: {
        "prompt_tokens": 10, "completion_tokens": 5
    })


class FakeHub:
    """记录 emit、并可控制取消的假 hub。"""

    def __init__(self) -> None:
        """初始化事件记录与取消开关。"""
        self.events: list[tuple[str, dict]] = []
        self.cancelled = False

    async def emit(self, run_id: int, event_type: Any, payload: dict) -> Any:
        """记一条事件，返回带 ``seq`` 的对象，形状与 RunEventDTO 一致。"""
        self.events.append((str(getattr(event_type, "value", event_type)), payload))
        return type("E", (), {"seq": len(self.events)})()

    def is_cancelled(self, run_id: int) -> bool:
        """返回是否已请求取消。"""
        return self.cancelled


class FakeOps:
    """记录节点执行顺序与渲染输入的假编排器动作。"""

    def __init__(self, fail_on: str | None = None) -> None:
        """记录执行轨迹；``fail_on`` 指定要让哪个节点抛异常。"""
        self.ran: list[str] = []
        self.rendered: list[str] = []
        self.persisted: list[tuple[str, str]] = []
        self.current: list[str] = []
        self.fail_on = fail_on

    async def set_current_node(self, run_id: int, node_id: str) -> None:
        """记录当前节点。"""
        self.current.append(node_id)

    async def run_with_retry(
        self, run_id: int, node: WorkflowNode, agent: Any,
        context: list[dict], rendered_input: str, user_input: str = "",
    ) -> FakeResult:
        """模拟跑一个节点；命中 ``fail_on`` 时抛异常。"""
        if node.id == self.fail_on:
            raise RuntimeError(f"节点 {node.id} 炸了")
        self.ran.append(node.id)
        self.rendered.append(rendered_input)
        return FakeResult(text=f"输出-{node.id}")

    async def persist_message(
        self, run_id: int, conversation_id: int, agent_code: str, text: str
    ) -> None:
        """记录落库的消息。"""
        self.persisted.append((agent_code, text))


def _definition(nodes: list[tuple[str, str]], edges: list[tuple[str, str]], entry: str = ""):
    """用 (id, agentCode) 列表和 (from, to) 列表拼一个 definition。"""
    return WorkflowDefinition.model_validate({
        "entry": entry,
        "nodes": [{"id": i, "agentCode": c} for i, c in nodes],
        "edges": [{"from": a, "to": b} for a, b in edges],
        "inputTemplate": "问：{{user_input}}\n上游：{{upstream}}",
    })


def _runner(definition: WorkflowDefinition, ops: FakeOps, hub: FakeHub | None = None):
    """按给定 definition 造一个 NodeRunner。"""
    return NodeRunner(
        run_id=1,
        conversation_id=1,
        definition=definition,
        agents={n.agentCode: type("A", (), {"model": "fake-model"})()
                for n in definition.nodes},
        user_input="原始问题",
        hub=hub or FakeHub(),
        ops=ops,
    )


def _initial() -> RunState:
    """初始状态。"""
    return {
        "context": [{"role": "user", "content": "历史"}],
        "upstream": {},
        "usage": {"prompt_tokens": 0, "completion_tokens": 0},
    }


# --------------------------------------------------------------------- 构图


async def test_single_node_graph_runs_without_edges():
    """单节点流程没有 edge，必须照样能构图并执行。"""
    definition = _definition([("only", "a1")], [])
    ops = FakeOps()
    graph = build_run_graph(definition, _runner(definition, ops))

    out = await graph.ainvoke(_initial(), {"recursion_limit": recursion_limit(definition)})

    assert ops.ran == ["only"]
    assert out["upstream"] == {"only": "输出-only"}


async def test_execution_order_follows_edges_not_declaration_order():
    """**关键用例**：nodes 声明顺序与 edges 相反时，仍必须按 edges 执行。

    如果实现是「按 definition.nodes 的顺序硬串」，这里会得到 b → a。
    用图表达编排的意义正在于连线是唯一的真相来源。
    """
    definition = _definition(
        nodes=[("b", "a2"), ("a", "a1")],
        edges=[("a", "b")],
        entry="a",
    )
    ops = FakeOps()
    graph = build_run_graph(definition, _runner(definition, ops))

    await graph.ainvoke(_initial(), {"recursion_limit": recursion_limit(definition)})

    assert ops.ran == ["a", "b"]  # 不是 ["b", "a"]


async def test_three_node_chain_runs_in_topological_order():
    """三节点链按拓扑序执行。"""
    definition = _definition(
        nodes=[("c", "a3"), ("a", "a1"), ("b", "a2")],
        edges=[("a", "b"), ("b", "c")],
        entry="a",
    )
    ops = FakeOps()
    graph = build_run_graph(definition, _runner(definition, ops))

    await graph.ainvoke(_initial(), {"recursion_limit": recursion_limit(definition)})

    assert ops.ran == ["a", "b", "c"]


# ----------------------------------------------------------------- 状态流转


async def test_state_flows_downstream_and_usage_accumulates():
    """上游产出要流到下游的渲染输入里，usage 要跨节点累加。"""
    definition = _definition(
        nodes=[("a", "a1"), ("b", "a2")], edges=[("a", "b")], entry="a"
    )
    ops = FakeOps()
    hub = FakeHub()
    graph = build_run_graph(definition, _runner(definition, ops, hub))

    out = await graph.ainvoke(_initial(), {"recursion_limit": recursion_limit(definition)})

    # 第一个节点还没上游，第二个能看到 a 的产出
    assert "（无）" in ops.rendered[0]
    assert "输出-a" in ops.rendered[1]
    assert out["usage"] == {"prompt_tokens": 20, "completion_tokens": 10}
    assert out["context"][-1] == {"role": "assistant", "content": "输出-b"}
    assert [t for t, _ in hub.events] == ["node_start", "node_end", "node_start", "node_end"]


async def test_assistant_messages_persisted_per_node():
    """每个节点的产出都落一条 assistant 消息，且带上 agentCode。"""
    definition = _definition(
        nodes=[("a", "agent_x"), ("b", "agent_y")], edges=[("a", "b")], entry="a"
    )
    ops = FakeOps()
    graph = build_run_graph(definition, _runner(definition, ops))

    await graph.ainvoke(_initial(), {"recursion_limit": recursion_limit(definition)})

    assert ops.persisted == [("agent_x", "输出-a"), ("agent_y", "输出-b")]


async def test_current_node_recorded_before_each_node():
    """``run.current_node`` 在每个节点开跑前更新。"""
    definition = _definition(
        nodes=[("a", "a1"), ("b", "a2")], edges=[("a", "b")], entry="a"
    )
    ops = FakeOps()
    graph = build_run_graph(definition, _runner(definition, ops))

    await graph.ainvoke(_initial(), {"recursion_limit": recursion_limit(definition)})

    assert ops.current == ["a", "b"]


# ----------------------------------------------------------------- 失败路径


async def test_node_failure_propagates_unchanged():
    """节点异常必须**原样**穿透图引擎，下游节点不得执行。

    这是编排器失败分支的地基：异常若被图引擎吞掉或换一种类型，execute 的
    except 就接不住，run 会「静默结束」——不报错、不置 error 状态。
    """
    definition = _definition(
        nodes=[("a", "a1"), ("b", "a2")], edges=[("a", "b")], entry="a"
    )
    ops = FakeOps(fail_on="a")
    graph = build_run_graph(definition, _runner(definition, ops))

    with pytest.raises(RuntimeError, match="节点 a 炸了"):
        await graph.ainvoke(_initial(), {"recursion_limit": recursion_limit(definition)})

    assert ops.ran == []  # 下游节点没跑


async def test_cancelled_run_stops_before_spending_money():
    """已取消的 run 绝不该走到节点执行——必须在下发之前就中止。"""
    definition = _definition(
        nodes=[("a", "a1"), ("b", "a2")], edges=[("a", "b")], entry="a"
    )
    ops = FakeOps()
    hub = FakeHub()
    hub.cancelled = True
    graph = build_run_graph(definition, _runner(definition, ops, hub))

    with pytest.raises(RunCancelled):
        await graph.ainvoke(_initial(), {"recursion_limit": recursion_limit(definition)})

    assert ops.ran == []
    assert hub.events == []  # 连 node_start 都没发


async def test_recursion_limit_exceeds_node_count():
    """recursion_limit 要够跑完一条链，否则正常流程会被当成图里有环。"""
    definition = _definition(
        nodes=[(f"n{i}", f"a{i}") for i in range(6)],
        edges=[(f"n{i}", f"n{i+1}") for i in range(5)],
        entry="n0",
    )
    assert recursion_limit(definition) > len(definition.nodes)

    ops = FakeOps()
    graph = build_run_graph(definition, _runner(definition, ops))
    await graph.ainvoke(_initial(), {"recursion_limit": recursion_limit(definition)})
    assert ops.ran == [f"n{i}" for i in range(6)]


# ------------------------------------------------------------------ V3 DAG


def _dag_definition(spec: dict):
    """按完整 dict 造 definition——DAG 用例需要 outputKey 与 when，原地拼太啰嗦。"""
    return WorkflowDefinition.model_validate(spec)


def _graph(definition: WorkflowDefinition, ops: FakeOps):
    """构图并执行，返回最终状态。"""
    graph = build_run_graph(definition, _runner(definition, ops))
    return graph


async def test_parallel_fanout_runs_both_branches_and_keeps_both_outputs():
    """并行分叉的两个分支都要跑，且**两条产出都得留下**。

    这是 V3 最关键的一条：并行节点在同一超步各自基于同一份 base state 返回增量，
    没有 reducer 时 LangGraph 只保留后写的那个——一条分支的产出凭空消失，而 run
    仍以 done 正常收尾。这种丢失极难察觉，所以必须钉死。
    """
    definition = _dag_definition({
        "entry": "root",
        "nodes": [
            {"id": "root", "agentCode": "a1", "outputKey": "plan"},
            {"id": "left", "agentCode": "a2", "outputKey": "left_out"},
            {"id": "right", "agentCode": "a3", "outputKey": "right_out"},
        ],
        "edges": [
            {"from": "root", "to": "left"},
            {"from": "root", "to": "right"},
        ],
    })
    ops = FakeOps()
    graph = _graph(definition, ops)

    out = await graph.ainvoke(_initial(), {"recursion_limit": recursion_limit(definition)})

    assert set(ops.ran) == {"root", "left", "right"}
    # 两条分支的产出都必须在
    assert out["upstream"]["left_out"] == "输出-left"
    assert out["upstream"]["right_out"] == "输出-right"
    assert out["upstream"]["plan"] == "输出-root"


async def test_fanin_node_sees_outputs_of_both_parents():
    """汇聚节点要能看到两条分支的全部产出，且只跑一次。"""
    definition = _dag_definition({
        "entry": "root",
        "nodes": [
            {"id": "root", "agentCode": "a1", "outputKey": "plan"},
            {"id": "left", "agentCode": "a2", "outputKey": "left_out"},
            {"id": "right", "agentCode": "a3", "outputKey": "right_out"},
            {"id": "join", "agentCode": "a4"},
        ],
        "edges": [
            {"from": "root", "to": "left"},
            {"from": "root", "to": "right"},
            {"from": "left", "to": "join"},
            {"from": "right", "to": "join"},
        ],
    })
    ops = FakeOps()
    graph = _graph(definition, ops)

    out = await graph.ainvoke(_initial(), {"recursion_limit": recursion_limit(definition)})

    assert ops.ran.count("join") == 1, "汇聚节点必须只跑一次"
    # join 的渲染输入里应同时出现两条分支的产出
    join_input = ops.rendered[-1]
    assert "输出-left" in join_input
    assert "输出-right" in join_input


async def test_usage_accumulates_once_per_node_even_in_parallel():
    """并行分支的 usage 要**相加**，不能各带全量被重复叠加。

    回归价值极高：节点函数早期返回的是「基于当前 state 累加后的全量」，
    配上 reducer 就会双倍计数（2 节点各 10/5 时得到 30/15 而不是 20/10），
    且数字随节点数线性膨胀——线上只会表现为成本统计悄悄偏高。
    """
    definition = _dag_definition({
        "entry": "root",
        "nodes": [
            {"id": "root", "agentCode": "a1", "outputKey": "plan"},
            {"id": "left", "agentCode": "a2", "outputKey": "left_out"},
            {"id": "right", "agentCode": "a3", "outputKey": "right_out"},
        ],
        "edges": [
            {"from": "root", "to": "left"},
            {"from": "root", "to": "right"},
        ],
    })
    ops = FakeOps()
    graph = _graph(definition, ops)

    out = await graph.ainvoke(_initial(), {"recursion_limit": recursion_limit(definition)})

    # 3 个节点，每节点 10 prompt / 5 completion
    assert out["usage"] == {"prompt_tokens": 30, "completion_tokens": 15}


async def test_conditional_edge_takes_only_the_matching_branch():
    """条件成立时只走命中的那条分支，另一条完全不执行。"""
    definition = _dag_definition({
        "entry": "root",
        "nodes": [
            {"id": "root", "agentCode": "a1", "outputKey": "plan"},
            {"id": "deep", "agentCode": "a2", "outputKey": "deep_out"},
            {"id": "shallow", "agentCode": "a3", "outputKey": "shallow_out"},
        ],
        "edges": [
            {"from": "root", "to": "deep", "when": 'contains(upstream.plan, "深入")'},
            {"from": "root", "to": "shallow", "when": "true"},
        ],
    })
    ops = FakeOps()
    graph = _graph(definition, ops)

    out = await graph.ainvoke(_initial(), {"recursion_limit": recursion_limit(definition)})

    # plan = "输出-root"，不含「深入」，所以只走 shallow
    assert set(ops.ran) == {"root", "shallow"}
    assert "deep_out" not in out["upstream"]
    assert out["upstream"]["shallow_out"] == "输出-shallow"


async def test_conditional_edge_all_false_terminates_branch():
    """条件全不成立时要走到 END，不能把 run 悬死。"""
    definition = _dag_definition({
        "entry": "root",
        "nodes": [
            {"id": "root", "agentCode": "a1", "outputKey": "plan"},
            {"id": "deep", "agentCode": "a2", "outputKey": "deep_out"},
            {"id": "shallow", "agentCode": "a3", "outputKey": "shallow_out"},
        ],
        "edges": [
            {"from": "root", "to": "deep", "when": 'contains(upstream.plan, "深入")'},
            {"from": "root", "to": "shallow", "when": 'contains(upstream.plan, "快速")'},
        ],
    })
    ops = FakeOps()
    graph = _graph(definition, ops)

    out = await graph.ainvoke(_initial(), {"recursion_limit": recursion_limit(definition)})

    assert ops.ran == ["root"]
    assert out["upstream"] == {"plan": "输出-root"}


async def test_conditional_branch_can_feed_downstream_unconditionally():
    """条件分支的后继可以是无条件边——「选中哪条都要往下走」。"""
    definition = _dag_definition({
        "entry": "root",
        "nodes": [
            {"id": "root", "agentCode": "a1", "outputKey": "plan"},
            {"id": "deep", "agentCode": "a2", "outputKey": "deep_out"},
            {"id": "shallow", "agentCode": "a3", "outputKey": "shallow_out"},
            {"id": "final", "agentCode": "a4", "outputKey": "final_out"},
        ],
        "edges": [
            {"from": "root", "to": "deep", "when": 'contains(upstream.plan, "深入")'},
            {"from": "root", "to": "shallow", "when": "true"},
            {"from": "deep", "to": "final"},
            {"from": "shallow", "to": "final"},
        ],
    })
    ops = FakeOps()
    graph = _graph(definition, ops)

    await graph.ainvoke(_initial(), {"recursion_limit": recursion_limit(definition)})

    assert "shallow" in ops.ran
    assert "final" in ops.ran, "选中的分支必须继续往下走"
    assert "deep" not in ops.ran
