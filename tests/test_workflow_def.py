"""Workflow definition 解析与校验（设计文档 §4.2 / §6.1）。"""

from __future__ import annotations

import pytest

from app.core.workflow_def import WorkflowDefinition, render_template

LINEAR = {
    "entry": "planner",
    "nodes": [
        {"id": "planner", "agentCode": "planner", "outputKey": "plan"},
        {"id": "researcher", "agentCode": "researcher", "outputKey": "facts"},
        {"id": "writer", "agentCode": "writer", "outputKey": "answer"},
    ],
    "edges": [
        {"from": "planner", "to": "researcher"},
        {"from": "researcher", "to": "writer"},
    ],
    "inputTemplate": "问题：{{user_input}}\n上游：{{upstream}}",
}


def test_linear_order_follows_edges():
    """执行顺序必须由 edges 拓扑推出，不能偷懒用 nodes 的声明顺序。

    用户在配置里把节点乱序排列时，编排必须照 edges 走，否则「谁先跑」全看运气。
    """
    d = WorkflowDefinition.model_validate(LINEAR)
    assert [n.id for n in d.linear_order()] == ["planner", "researcher", "writer"]


def test_edges_may_be_omitted_for_single_node_pipeline():
    """单节点流水线没有 edges 也很常见，不能因此报「缺边」而配不出来。"""
    d = WorkflowDefinition.model_validate(
        {"nodes": [{"id": "solo", "agentCode": "solo"}]}
    )
    assert [n.id for n in d.linear_order()] == ["solo"]


def test_definition_uses_from_alias():
    """``from`` 是 Python 关键字，边模型必须用 ``from_`` 别名并正确映射。

    忘了 alias 会让合法的边定义直接校验失败。
    """
    d = WorkflowDefinition.model_validate(LINEAR)
    assert d.edges[0].from_ == "planner"


def test_unknown_entry_rejected():
    """entry 指向不存在的节点：配置期就拒，否则运行期找不到起点。"""
    bad = {**LINEAR, "entry": "nope"}
    with pytest.raises(ValueError, match="entry 节点不存在"):
        WorkflowDefinition.model_validate(bad)


def test_edge_to_unknown_node_rejected():
    """悬空的边（to 指向幽灵节点）必须拒：拓扑排序会漏掉这个节点。"""
    bad = {**LINEAR, "edges": [{"from": "planner", "to": "ghost"}]}
    with pytest.raises(ValueError, match="不存在的节点"):
        WorkflowDefinition.model_validate(bad)


def test_cycle_rejected():
    """环状定义会让拓扑排序死循环，必须在配置期就报错而不是运行期卡死。"""
    bad = {
        "entry": "a",
        "nodes": [
            {"id": "a", "agentCode": "a"},
            {"id": "b", "agentCode": "b"},
        ],
        "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "a"}],
    }
    with pytest.raises(ValueError, match="环"):
        WorkflowDefinition.model_validate(bad)


def test_duplicate_node_id_rejected():
    """节点 id 重复会让 ``by_id`` 静默覆盖一个节点，编排时少跑一个 Agent。"""
    bad = {
        "nodes": [{"id": "a", "agentCode": "x"}, {"id": "a", "agentCode": "y"}]
    }
    with pytest.raises(ValueError, match="节点 id 重复"):
        WorkflowDefinition.model_validate(bad)


def test_branching_dag_rejected_at_config_time():
    """V1 明确拒绝 DAG —— 在配置期就拒，而不是跑到一半才发现编排不了。"""
    dag = {
        "entry": "root",
        "nodes": [
            {"id": "root", "agentCode": "root"},
            {"id": "left", "agentCode": "left"},
            {"id": "right", "agentCode": "right"},
            {"id": "join", "agentCode": "join"},
        ],
        "edges": [
            {"from": "root", "to": "left"},
            {"from": "root", "to": "right"},
            {"from": "left", "to": "join"},
            {"from": "right", "to": "join"},
        ],
    }
    with pytest.raises(ValueError, match="DAG"):
        WorkflowDefinition.model_validate(dag)


def test_render_input_substitutes_user_and_upstream():
    """模板变量要同时填上「用户问题」和「上游产出」，这是节点间唯一的数据通道。"""
    d = WorkflowDefinition.model_validate(LINEAR)
    out = d.render_input("怎么接入？", {"plan": "第一步…"})
    assert "怎么接入？" in out
    assert "plan" in out and "第一步…" in out


def test_render_input_handles_empty_upstream():
    """首个节点没有上游时要给占位文本，不能把裸 ``{{upstream}}`` 发给模型。"""
    d = WorkflowDefinition.model_validate(LINEAR)
    assert "（无）" in d.render_input("问题", {})


def test_render_template_leaves_unknown_vars():
    """未知变量保持原样而不是清空或抛错：模板里出现未赋值占位符不该让整条 run 失败。"""
    assert render_template("a={{x}} b={{y}}", {"x": "1"}) == "a=1 b={{y}}"


def test_template_vars_extraction():
    """能扫出模板用到的变量，配置页才能在保存前提示「这个变量没人喂」。"""
    d = WorkflowDefinition.model_validate(LINEAR)
    assert {"user_input", "upstream"} <= d.template_vars()
