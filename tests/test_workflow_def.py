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


def test_topo_order_follows_edges():
    """执行顺序必须由 edges 拓扑推出，不能偷懒用 nodes 的声明顺序。

    用户在配置里把节点乱序排列时，编排必须照 edges 走，否则「谁先跑」全看运气。
    """
    d = WorkflowDefinition.model_validate(LINEAR)
    assert [n.id for n in d.topo_order()] == ["planner", "researcher", "writer"]


def test_edges_may_be_omitted_for_single_node_pipeline():
    """单节点流水线没有 edges 也很常见，不能因此报「缺边」而配不出来。"""
    d = WorkflowDefinition.model_validate(
        {"nodes": [{"id": "solo", "agentCode": "solo"}]}
    )
    assert [n.id for n in d.topo_order()] == ["solo"]


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


DAG = {
    "entry": "root",
    "nodes": [
        {"id": "root", "agentCode": "root", "outputKey": "plan"},
        {"id": "left", "agentCode": "left", "outputKey": "left_out"},
        {"id": "right", "agentCode": "right", "outputKey": "right_out"},
        {"id": "join", "agentCode": "join"},
    ],
    "edges": [
        {"from": "root", "to": "left"},
        {"from": "root", "to": "right"},
        {"from": "left", "to": "join"},
        {"from": "right", "to": "join"},
    ],
}


def test_branching_dag_accepted_from_v3():
    """V3 起 DAG 合法：分叉 + 汇聚都要在配置期放行。"""
    d = WorkflowDefinition.model_validate(DAG)
    assert {n.id for n in d.topo_order()} == {"root", "left", "right", "join"}
    # 分叉后两个分支应当落在同一层（并行），而不是被排成一前一后
    order = [n.id for n in d.topo_order()]
    assert order.index("join") == 3


def test_cycle_still_rejected():
    """环必须继续拒绝——它让执行顺序无法确定，而且会触发 recursion_limit。"""
    cyc = {
        "entry": "a",
        "nodes": [{"id": x, "agentCode": x} for x in ("a", "b")],
        "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "a"}],
    }
    with pytest.raises(ValueError, match="环"):
        WorkflowDefinition.model_validate(cyc)


def test_orphan_node_rejected():
    """从 entry 走不到的节点是死配置：保存不报错，但它永远不会跑。"""
    orphan = {
        "entry": "a",
        "nodes": [{"id": "a", "agentCode": "a"}, {"id": "lost", "agentCode": "x"}],
        "edges": [],
    }
    with pytest.raises(ValueError, match="孤立节点"):
        WorkflowDefinition.model_validate(orphan)


def test_conditional_edge_syntax_checked_at_config_time():
    """条件表达式拼错必须在保存时就被拒，而不是跑到那个分支才炸。"""
    bad = {
        "entry": "a",
        "nodes": [{"id": "a", "agentCode": "a"}, {"id": "b", "agentCode": "b"}],
        "edges": [{"from": "a", "to": "b", "when": 'contains(upstream.a, "x"'}],
    }
    with pytest.raises(ValueError, match="语法错误"):
        WorkflowDefinition.model_validate(bad)


def test_conditional_edge_unknown_output_key_rejected():
    """引用不存在的 outputKey 会被拒：否则运行期只会安静地走错分支。"""
    bad = {
        "entry": "a",
        "nodes": [
            {"id": "a", "agentCode": "a", "outputKey": "plan"},
            {"id": "b", "agentCode": "b"},
            {"id": "c", "agentCode": "c"},
        ],
        "edges": [
            {"from": "a", "to": "b", "when": 'contains(upstream.plna, "x")'},
            {"from": "a", "to": "c", "when": "true"},
        ],
    }
    with pytest.raises(ValueError, match="upstream.plna"):
        WorkflowDefinition.model_validate(bad)


def test_mixed_conditional_and_unconditional_outgoing_rejected():
    """同一节点出边混用条件与无条件时语义不明确，明确拒掉。"""
    mixed = {
        "entry": "a",
        "nodes": [
            {"id": "a", "agentCode": "a", "outputKey": "out"},
            {"id": "b", "agentCode": "b"},
            {"id": "c", "agentCode": "c"},
        ],
        "edges": [
            {"from": "a", "to": "b"},
            {"from": "a", "to": "c", "when": 'contains(upstream.out, "x")'},
        ],
    }
    with pytest.raises(ValueError, match="混用"):
        WorkflowDefinition.model_validate(mixed)


def test_conditional_fanin_rejected():
    """条件边做汇聚输入会被拒：条件不成立时 LangGraph 会永远等不到汇聚节点。

    夹具刻意让 root 的出边**全是无条件**（并行分叉），这样触发的才是汇聚规则
    而不是「出边混用」规则——两个校验的优先级要能被分别测到。
    """
    fanin = {
        "entry": "root",
        "nodes": [
            {"id": "root", "agentCode": "root", "outputKey": "plan"},
            {"id": "left", "agentCode": "left", "outputKey": "left_out"},
            {"id": "right", "agentCode": "right", "outputKey": "right_out"},
            {"id": "join", "agentCode": "join"},
        ],
        "edges": [
            {"from": "root", "to": "left"},
            {"from": "root", "to": "right"},
            {"from": "left", "to": "join", "when": 'is_empty(upstream.left_out)'},
            {"from": "right", "to": "join"},
        ],
    }
    with pytest.raises(ValueError, match="汇聚节点"):
        WorkflowDefinition.model_validate(fanin)


def test_all_conditional_fanin_rejected():
    """全部入边都是条件边的汇聚同样拒绝，并给出可执行的改法。"""
    fanin = {
        "entry": "root",
        "nodes": [
            {"id": "root", "agentCode": "root", "outputKey": "plan"},
            {"id": "left", "agentCode": "left"},
            {"id": "right", "agentCode": "right"},
            {"id": "join", "agentCode": "join"},
        ],
        "edges": [
            {"from": "root", "to": "left", "when": "true"},
            {"from": "root", "to": "right", "when": "true"},
            {"from": "left", "to": "join", "when": "true"},
            {"from": "right", "to": "join", "when": "true"},
        ],
    }
    with pytest.raises(ValueError, match="全是条件边"):
        WorkflowDefinition.model_validate(fanin)


def test_conditional_branch_accepted():
    """标准的「二选一」分支要能配出来。"""
    branch = {
        "entry": "root",
        "nodes": [
            {"id": "root", "agentCode": "root", "outputKey": "plan"},
            {"id": "deep", "agentCode": "deep"},
            {"id": "shallow", "agentCode": "shallow"},
        ],
        "edges": [
            {"from": "root", "to": "deep", "when": 'contains(upstream.plan, "深入")'},
            {"from": "root", "to": "shallow", "when": "true"},
        ],
    }
    d = WorkflowDefinition.model_validate(branch)
    assert len(d.edges) == 2
    assert sum(1 for e in d.edges if e.when) == 2


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


# ------------------------------------------------- inputTemplate 的变量校验（V3）


def test_render_input_substitutes_each_output_key():
    """``{{outputKey}}`` 必须逐个替换——这是 DAG 下把两条分支分开传给汇聚节点的方式。

    曾经只替换 ``{{user_input}}`` 和 ``{{upstream}}`` 两个固定串，docstring 承诺的
    逐键引用其实没实现：模板里写 ``{{left_out}}`` 会原样发给模型看。
    """
    d = WorkflowDefinition.model_validate({
        **LINEAR,
        "inputTemplate": "问：{{user_input}}\n计划：{{plan}}\n事实：{{facts}}\n全：{{upstream}}",
    })
    out = d.render_input("问题", {"plan": "左产", "facts": "右产"})
    assert "计划：左产" in out and "事实：右产" in out
    assert "{{" not in out, f"有占位符没渲染：{out}"


def test_unknown_template_var_rejected_at_config_time():
    """模板变量拼错要在保存时拦下。

    运行期对未知变量是「原样保留」（一次拼错不该让整条 run 失败），但代价是
    ``{{拼错}}`` 会原封不动出现在 prompt 里，看起来像模型在胡说。配置期拦掉
    成本最低：用户改个字段名就行。
    """
    bad = {**LINEAR, "inputTemplate": "{{user_input}} {{plat}}"}
    with pytest.raises(ValueError, match="未知变量"):
        WorkflowDefinition.model_validate(bad)


def test_dotted_template_var_rejected_with_hint():
    """``{{upstream.plan}}`` 这种带点的写法要报错，并告诉正确写法是什么。

    模板语法里点号没有意义，它几乎总是「以为能这么写」的误解。
    """
    bad = {**LINEAR, "inputTemplate": "{{user_input}} {{upstream.plan}}"}
    with pytest.raises(ValueError, match="点号访问"):
        WorkflowDefinition.model_validate(bad)


def test_template_var_may_be_any_node_output_key():
    """outputKey 是合法模板变量——哪怕它由本图后面的节点产出。

    静态校验只认「这张图里存在哪些 outputKey」，不判断拓扑先后：用户完全可能
    先配好下游再配上游的产出键，没必要拦。
    """
    ok = {**LINEAR, "inputTemplate": "{{user_input}} {{answer}}"}
    assert WorkflowDefinition.model_validate(ok).template_vars() == {"user_input", "answer"}


def test_bare_output_key_in_when_rejected():
    """``when`` 里的裸 outputKey 必须拒绝并提示加 ``upstream.`` 前缀。

    求值期的 scope 只有 ``upstream`` 与 ``user_input`` 两个根，裸名会静默求值成
    None → 条件恒假 → 走错分支，而且**没有任何报错**。这是本轮端到端实测踩到的。
    """
    bad = {
        "entry": "root",
        "nodes": [
            {"id": "root", "agentCode": "a1", "outputKey": "plan"},
            {"id": "deep", "agentCode": "a2", "outputKey": "deep_out"},
            {"id": "shallow", "agentCode": "a3", "outputKey": "shallow_out"},
        ],
        "edges": [
            {"from": "root", "to": "deep", "when": 'plan == "深入"'},
            {"from": "root", "to": "shallow", "when": "true"},
        ],
    }
    with pytest.raises(ValueError, match="upstream.plan"):
        WorkflowDefinition.model_validate(bad)


def test_user_input_allowed_as_bare_name_in_when():
    """``user_input`` 是 scope 的根之一，裸写是合法的，不能被上面的规则误伤。"""
    ok = {
        "entry": "root",
        "nodes": [
            {"id": "root", "agentCode": "a1", "outputKey": "plan"},
            {"id": "a", "agentCode": "a2"},
            {"id": "b", "agentCode": "a3"},
        ],
        "edges": [
            {"from": "root", "to": "a", "when": 'contains(user_input, "深入")'},
            {"from": "root", "to": "b", "when": "true"},
        ],
    }
    assert len(WorkflowDefinition.model_validate(ok).edges) == 2
