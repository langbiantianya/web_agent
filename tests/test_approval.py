"""人工审批（V3.1）：暂停、恢复、驳回。

要钉住的命题：

1. **配置期**就把形状不对的审批节点拒掉（没 prompt、混了 agentCode、类型不认识）；
2. 走到审批节点时图**真的挂起**（返回 ``__interrupt__``），且只有被中断的节点
   会在恢复时重跑——前面的节点不重跑、不重花 token；
3. **批准** → 图继续往下走，审批意见进入 ``upstream``；
4. **驳回** → 抛 ``RunRejected``，本次 run 就此终止。

用真实的 LangGraph + 真实的 SQLite checkpointer，不用 mock：挂起/恢复的语义完全
由框架决定，mock 掉它就等于什么都没测。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from langgraph.types import Command

from app.core.checkpoint import checkpoint_config
from app.core.orchestrator import extract_interrupt
from app.core.run_graph import NodeRunner, RunRejected, build_run_graph
from app.core.workflow_def import WorkflowDefinition, WorkflowNode

# pytest.ini 里 asyncio_mode = auto，async 用例自动识别，这里不用再挂标记


@dataclass
class FakeAgent:
    """``Agent`` 行的最小替身——只有 ``model`` 会被节点读。"""

    model: str = "fake-model"


@dataclass
class FakeResult:
    """``AgentResult`` 的最小替身。"""

    text: str
    usage: dict[str, int] = field(
        default_factory=lambda: {"prompt_tokens": 10, "completion_tokens": 5}
    )


class FakeHub:
    """记录 emit 的假 hub。"""

    def __init__(self) -> None:
        """初始化事件记录。"""
        self.events: list[tuple[str, dict]] = []

    async def emit(self, run_id: int, event_type: Any, payload: dict) -> Any:
        """记一条事件，返回带 ``seq`` 的对象。"""
        self.events.append((str(getattr(event_type, "value", event_type)), payload))
        return type("E", (), {"seq": len(self.events)})()

    def is_cancelled(self, run_id: int) -> bool:
        """本组用例不测取消。"""
        return False


class FakeOps:
    """记录节点执行轨迹的假编排器动作。"""

    def __init__(self) -> None:
        """初始化执行轨迹。"""
        self.ran: list[str] = []
        self.persisted: list[tuple[str, str]] = []
        self.approvals: list[tuple[str, bool, str]] = []

    async def set_current_node(self, run_id: int, node_id: str) -> None:
        """空操作。"""

    async def run_with_retry(
        self, run_id: int, node: WorkflowNode, agent: Any,
        context: list[dict], rendered_input: str, user_input: str = "",
    ) -> FakeResult:
        """记录跑过哪个节点。"""
        self.ran.append(node.id)
        return FakeResult(text=f"输出-{node.id}")

    async def persist_message(
        self, run_id: int, conversation_id: int, agent_code: str, text: str
    ) -> None:
        """记录落库的消息。"""
        self.persisted.append((agent_code, text))

    async def finish_approval_step(
        self, run_id: int, node_id: str, approved: bool, comment: str
    ) -> None:
        """记录审批节点的收尾。"""
        self.approvals.append((node_id, approved, comment))


def _definition() -> WorkflowDefinition:
    """一个「普通节点 → 审批节点 → 普通节点」的流程。"""
    return WorkflowDefinition.model_validate({
        "entry": "first",
        "nodes": [
            {"id": "first", "agentCode": "a1", "outputKey": "plan"},
            {"id": "gate", "type": "approval", "prompt": "这份计划可以执行吗？",
             "outputKey": "gate_out"},
            {"id": "last", "agentCode": "a2", "outputKey": "answer"},
        ],
        "edges": [
            {"from": "first", "to": "gate"},
            {"from": "gate", "to": "last"},
        ],
        "inputTemplate": "问：{{user_input}}",
    })


def _agents() -> dict[str, FakeAgent]:
    """给测试用定义里的两个 agentCode 各配一个假 agent。"""
    return {"a1": FakeAgent(), "a2": FakeAgent()}


async def _saver(run_id: int):
    """建一个临时 SQLite checkpointer（真实 saver，不用 mock）。

    文件名带 run_id：同一进程内多个用例各自独立，跨用例不串数据。
    """
    import tempfile
    from pathlib import Path

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    path = Path(tempfile.gettempdir()) / f"wa_approval_test_{run_id}.db"
    if path.exists():
        path.unlink()
    cm = AsyncSqliteSaver.from_conn_string(str(path))
    saver = await cm.__aenter__()
    await saver.setup()
    return cm, saver


# ------------------------------------------------------------- 配置期校验


def test_approval_node_requires_prompt():
    """审批节点没写 prompt 要被拒——审批人看什么？"""
    with pytest.raises(ValueError, match="prompt"):
        WorkflowDefinition.model_validate({
            "nodes": [{"id": "gate", "type": "approval"}],
        })


def test_approval_node_rejects_agent_code():
    """审批节点不能同时指定 agentCode：两套语义混在一个节点上说不清谁生效。"""
    with pytest.raises(ValueError, match="agentCode"):
        WorkflowDefinition.model_validate({
            "nodes": [
                {"id": "gate", "type": "approval", "prompt": "批？", "agentCode": "a1"},
            ],
        })


def test_unknown_node_type_rejected():
    """不认识的节点类型要报错并列出可用的。"""
    with pytest.raises(ValueError, match="节点类型"):
        WorkflowDefinition.model_validate({
            "nodes": [{"id": "x", "type": "magic", "agentCode": "a1"}],
        })


def test_agent_node_still_requires_agent_code():
    """普通节点仍然必须有 agentCode——默认值不能把老配置校验放松掉。"""
    with pytest.raises(ValueError, match="agentCode"):
        WorkflowDefinition.model_validate({
            "nodes": [{"id": "x"}],
        })


def test_agent_node_type_defaults_to_agent():
    """不写 type 就是普通节点，老 definition 一行都不用改。"""
    node = WorkflowNode.model_validate({"id": "x", "agentCode": "a1"})
    assert node.type == "agent"
    assert node.is_approval is False


# --------------------------------------------------------- 挂起与恢复


async def test_graph_interrupts_at_approval_node():
    """走到审批节点时图挂起，返回 __interrupt__，后续节点不执行。"""
    cm, saver = await _saver(1)
    try:
        ops = FakeOps()
        hub = FakeHub()
        definition = _definition()
        runner = NodeRunner(
            run_id=1, conversation_id=1, definition=definition, agents=_agents(),
            user_input="干吧", hub=hub, ops=ops,
        )
        graph = build_run_graph(definition, runner, checkpointer=saver)
        state = await graph.ainvoke(
            {"context": [], "upstream": {}, "usage": {}},
            checkpoint_config(1, 20),
        )

        paused = extract_interrupt(state)
        assert paused is not None, "审批节点没有挂起"
        assert paused["node"] == "gate"
        assert paused["prompt"] == "这份计划可以执行吗？"
        assert ops.ran == ["first"], f"挂起时不该跑后面的节点：{ops.ran}"
        # approval_required 事件由编排器发，不在节点里——节点会被执行两遍
        assert [e for e in hub.events if e[0] == "approval_required"] == []
    finally:
        await cm.__aexit__(None, None, None)


async def test_resume_continues_from_pause_without_rerunning():
    """恢复后只有被中断的节点重跑，前面的节点不重跑、不重花 token。"""
    cm, saver = await _saver(2)
    try:
        ops = FakeOps()
        hub = FakeHub()
        definition = _definition()
        runner = NodeRunner(
            run_id=2, conversation_id=1, definition=definition, agents=_agents(),
            user_input="干吧", hub=hub, ops=ops,
        )
        graph = build_run_graph(definition, runner, checkpointer=saver)
        config = checkpoint_config(2, 20)
        await graph.ainvoke(
            {"context": [], "upstream": {}, "usage": {}}, config
        )
        assert ops.ran == ["first"]

        state = await graph.ainvoke(
            Command(resume={"approved": True, "comment": "可以"}), config
        )

        # first 只跑过一次——恢复不重走
        assert ops.ran == ["first", "last"], f"执行轨迹不对：{ops.ran}"
        assert state["upstream"]["plan"] == "输出-first"
        assert state["upstream"]["gate_out"] == "可以", "审批意见没进 upstream"
        assert state["upstream"]["answer"] == "输出-last"
        assert ops.approvals == [("gate", True, "可以")]
        # 审批事件恰好一条：节点会被执行两遍，事件若放两遍就是重复发
        assert [e[0] for e in hub.events].count("approved") == 1
        assert "rejected" not in [e[0] for e in hub.events]
    finally:
        await cm.__aexit__(None, None, None)


async def test_rejection_raises_and_stops_the_graph():
    """驳回 → 抛 RunRejected，后面的节点不再执行。"""
    cm, saver = await _saver(3)
    try:
        ops = FakeOps()
        hub = FakeHub()
        definition = _definition()
        runner = NodeRunner(
            run_id=3, conversation_id=1, definition=definition, agents=_agents(),
            user_input="干吧", hub=hub, ops=ops,
        )
        graph = build_run_graph(definition, runner, checkpointer=saver)
        config = checkpoint_config(3, 20)
        await graph.ainvoke({"context": [], "upstream": {}, "usage": {}}, config)

        with pytest.raises(RunRejected) as exc:
            await graph.ainvoke(
                Command(resume={"approved": False, "comment": "不行"}), config
            )
        assert exc.value.comment == "不行"
        assert "last" not in ops.ran, "驳回后不该再跑下游节点"
        assert ops.approvals == [("gate", False, "不行")]
        assert [e[0] for e in hub.events].count("rejected") == 1
        assert "approved" not in [e[0] for e in hub.events]
    finally:
        await cm.__aexit__(None, None, None)


async def test_approval_without_comment_uses_default_text():
    """批准但没写意见时，产出用「已通过」占位，别让 downstream 拿到空串。"""
    cm, saver = await _saver(4)
    try:
        ops = FakeOps()
        definition = _definition()
        runner = NodeRunner(
            run_id=4, conversation_id=1, definition=definition, agents=_agents(),
            user_input="干吧", hub=FakeHub(), ops=ops,
        )
        graph = build_run_graph(definition, runner, checkpointer=saver)
        config = checkpoint_config(4, 20)
        await graph.ainvoke({"context": [], "upstream": {}, "usage": {}}, config)
        state = await graph.ainvoke(Command(resume={"approved": True}), config)
        assert state["upstream"]["gate_out"] == "已通过"
    finally:
        await cm.__aexit__(None, None, None)
