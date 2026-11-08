"""对比「改造前 / 改造后」两版 ``WorkflowDefinition`` 的校验行为。

背景：``app/core/workflow_def.py`` 的图结构校验从「遇到第一条违规就抛异常」
改成了「一次收集全部问题」（``collect_graph_issues``）。核心要求是
**``WorkflowDefinition`` 抛出的错误消息逐字不变**——管理员看到的文案不能因为
重构而变样。

本脚本从 **base commit** 反向重建改造前的真实文件（不是 ``HEAD``，避免拿新
文件跟自己比的自比风险），与当前文件在**同一进程**里分别加载，对同一批
payload 跑校验，比对 pydantic ``ValidationError`` 里的消息是否逐字一致。

用法：

```bash
python scripts/diff_workflow_def_behavior.py
python scripts/diff_workflow_def_behavior.py --base a1bdbc8
```

退出码 0 表示零不一致，非 0 表示有差异（并打印差异详情）。
"""

from __future__ import annotations

import argparse
import copy
import subprocess
import sys
import types
from itertools import combinations
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[1]
TARGET_FILE = "app/core/workflow_def.py"

# 以脚本方式运行时 sys.path 里没有仓库根，`import app.core` 会失败。
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# 改造前的 5 个私有校验方法：旧文件里必须在、新文件里必须不在。
REMOVED_METHODS = (
    "_check_reachable",
    "_check_fanin_shape",
    "_check_outgoing_uniform",
    "_validate_conditions",
    "_validate_template_vars",
)


def build_base_payload() -> dict[str, Any]:
    """构造一个合法的基准图：三节点两边的直线流程。"""
    return {
        "entry": "a",
        "nodes": [
            {"id": "a", "agentCode": "agent_a", "outputKey": "a_out"},
            {"id": "b", "agentCode": "agent_b", "outputKey": "b_out"},
            {"id": "c", "agentCode": "agent_c", "outputKey": "c_out"},
        ],
        "edges": [
            {"from": "a", "to": "b"},
            {"from": "b", "to": "c"},
        ],
    }


def _atom_edge_missing(p: dict[str, Any]) -> None:
    """缺陷原子：edge 指向不存在的节点。"""
    p["edges"].append({"from": "c", "to": "ghost"})


def _atom_entry_missing(p: dict[str, Any]) -> None:
    """缺陷原子：entry 指向不存在的节点。"""
    p["entry"] = "ghost"


def _atom_cycle(p: dict[str, Any]) -> None:
    """缺陷原子：图里有环。"""
    p["edges"].append({"from": "c", "to": "a"})


def _atom_orphan(p: dict[str, Any]) -> None:
    """缺陷原子：存在从 entry 走不到的孤立节点。"""
    p["nodes"].append({"id": "lonely", "agentCode": "agent_x", "outputKey": "lonely_out"})


def _atom_fanin_mixed(p: dict[str, Any]) -> None:
    """缺陷原子：汇聚节点入边混了条件边。"""
    p["nodes"].append({"id": "d", "agentCode": "agent_d", "outputKey": "d_out"})
    p["edges"].append({"from": "a", "to": "d", "when": "user_input == 'x'"})
    p["edges"].append({"from": "b", "to": "d"})


def _atom_fanin_all_conditional(p: dict[str, Any]) -> None:
    """缺陷原子：汇聚节点入边全是条件边。"""
    p["nodes"].append({"id": "e", "agentCode": "agent_e", "outputKey": "e_out"})
    p["edges"].append({"from": "a", "to": "e", "when": "user_input == 'x'"})
    p["edges"].append({"from": "b", "to": "e", "when": "user_input == 'y'"})


def _atom_outgoing_mixed(p: dict[str, Any]) -> None:
    """缺陷原子：同一节点出边混条件与无条件。"""
    p["edges"][0]["when"] = "user_input == 'x'"
    p["edges"].append({"from": "a", "to": "c"})


def _atom_when_syntax(p: dict[str, Any]) -> None:
    """缺陷原子：when 表达式语法错误。"""
    p["edges"][1]["when"] = "1 +"


def _atom_when_unknown_var(p: dict[str, Any]) -> None:
    """缺陷原子：when 引用未知变量。"""
    p["edges"][1]["when"] = "bogus"


def _atom_when_unknown_upstream(p: dict[str, Any]) -> None:
    """缺陷原子：when 引用不存在的 upstream outputKey。"""
    p["edges"][1]["when"] = "upstream.nope"


def _atom_template_dotted(p: dict[str, Any]) -> None:
    """缺陷原子：inputTemplate 里用了点号访问。"""
    p["inputTemplate"] = "问题：{{user_input}}\n上游：{{upstream.a_out.x}}"


ATOMS: tuple[tuple[str, Callable[[dict[str, Any]], None]], ...] = (
    ("edge_missing", _atom_edge_missing),
    ("entry_missing", _atom_entry_missing),
    ("cycle", _atom_cycle),
    ("orphan", _atom_orphan),
    ("fanin_mixed", _atom_fanin_mixed),
    ("fanin_all_conditional", _atom_fanin_all_conditional),
    ("outgoing_mixed", _atom_outgoing_mixed),
    ("when_syntax", _atom_when_syntax),
    ("when_unknown_var", _atom_when_unknown_var),
    ("when_unknown_upstream", _atom_when_unknown_upstream),
    ("template_dotted", _atom_template_dotted),
)


def build_explicit_cases() -> list[tuple[str, dict[str, Any]]]:
    """构造 35 个显式用例：合法图、字段级非法、每条图规则各一次、边界形状。"""
    base = build_base_payload
    cases: list[tuple[str, dict[str, Any]]] = [
        ("valid_linear", base()),
        ("valid_single_node", {"entry": "a", "nodes": [{"id": "a", "agentCode": "x"}], "edges": []}),
        (
            "valid_diamond",
            {
                "entry": "a",
                "nodes": [
                    {"id": "a", "agentCode": "x", "outputKey": "a_out"},
                    {"id": "b", "agentCode": "y", "outputKey": "b_out"},
                    {"id": "c", "agentCode": "z", "outputKey": "c_out"},
                    {"id": "d", "agentCode": "w", "outputKey": "d_out"},
                ],
                "edges": [
                    {"from": "a", "to": "b"},
                    {"from": "a", "to": "c"},
                    {"from": "b", "to": "d"},
                    {"from": "c", "to": "d"},
                ],
            },
        ),
        ("empty_nodes", {"entry": "a", "nodes": [], "edges": []}),
        (
            "agent_without_agent_code",
            {"entry": "a", "nodes": [{"id": "a"}], "edges": []},
        ),
        (
            "duplicate_node_id",
            {
                "entry": "a",
                "nodes": [{"id": "a", "agentCode": "x"}, {"id": "a", "agentCode": "y"}],
                "edges": [],
            },
        ),
        (
            "unknown_node_type",
            {"entry": "a", "nodes": [{"id": "a", "type": "robot", "agentCode": "x"}], "edges": []},
        ),
        (
            "approval_without_prompt",
            {"entry": "a", "nodes": [{"id": "a", "type": "approval"}], "edges": []},
        ),
        (
            "approval_with_agent_code",
            {
                "entry": "a",
                "nodes": [
                    {"id": "a", "type": "approval", "prompt": "同意吗", "agentCode": "x"}
                ],
                "edges": [],
            },
        ),
        (
            "approval_node_in_graph",
            {
                "entry": "a",
                "nodes": [
                    {"id": "a", "agentCode": "x", "outputKey": "a_out"},
                    {"id": "b", "type": "approval", "prompt": "同意吗"},
                ],
                "edges": [{"from": "a", "to": "b"}],
            },
        ),
        ("entry_missing", {**base(), "entry": "ghost"}),
        (
            "edge_endpoint_missing",
            {**base(), "edges": [*base()["edges"], {"from": "c", "to": "ghost"}]},
        ),
        (
            "cycle",
            {**base(), "edges": [*base()["edges"], {"from": "c", "to": "a"}]},
        ),
        (
            "orphan_node",
            {
                **base(),
                "nodes": [*base()["nodes"], {"id": "lonely", "agentCode": "x"}],
            },
        ),
        (
            "fanin_mixed",
            {
                **base(),
                "nodes": [*base()["nodes"], {"id": "d", "agentCode": "x"}],
                "edges": [
                    *base()["edges"],
                    {"from": "a", "to": "d", "when": "user_input == 'x'"},
                    {"from": "b", "to": "d"},
                ],
            },
        ),
        (
            "fanin_all_conditional",
            {
                **base(),
                "nodes": [*base()["nodes"], {"id": "e", "agentCode": "x"}],
                "edges": [
                    *base()["edges"],
                    {"from": "a", "to": "e", "when": "user_input == 'x'"},
                    {"from": "b", "to": "e", "when": "user_input == 'y'"},
                ],
            },
        ),
        (
            "outgoing_mixed",
            {
                **base(),
                "edges": [
                    {"from": "a", "to": "b", "when": "user_input == 'x'"},
                    {"from": "a", "to": "c"},
                ],
            },
        ),
        (
            "when_syntax_error",
            {**base(), "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "c", "when": "1 +"}]},
        ),
        (
            "when_unknown_var",
            {**base(), "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "c", "when": "bogus"}]},
        ),
        (
            "when_unknown_upstream",
            {
                **base(),
                "edges": [
                    {"from": "a", "to": "b"},
                    {"from": "b", "to": "c", "when": "upstream.nope"},
                ],
            },
        ),
        (
            "template_dotted",
            {**base(), "inputTemplate": "上游：{{upstream.a_out.x}}"},
        ),
        (
            "template_unknown_var",
            {**base(), "inputTemplate": "问题：{{user_input}}\n结果：{{nope}}"},
        ),
        (
            "template_valid_output_key",
            {**base(), "inputTemplate": "问题：{{user_input}}\n结果：{{a_out}}"},
        ),
        ("entry_empty_uses_first_node", {**base(), "entry": ""}),
        ("entry_points_to_second_node", {**base(), "entry": "b"}),
        (
            "two_disconnected_components",
            {
                "entry": "a",
                "nodes": [
                    {"id": "a", "agentCode": "x"},
                    {"id": "b", "agentCode": "y"},
                    {"id": "c", "agentCode": "z"},
                ],
                "edges": [{"from": "a", "to": "b"}],
            },
        ),
        ("self_loop", {**base(), "edges": [{"from": "a", "to": "a"}]}),
        (
            "edge_with_empty_when",
            {**base(), "edges": [{"from": "a", "to": "b", "when": ""}]},
        ),
        (
            "node_without_output_key",
            {
                "entry": "a",
                "nodes": [{"id": "a", "agentCode": "x"}, {"id": "b", "agentCode": "y"}],
                "edges": [{"from": "a", "to": "b"}],
            },
        ),
        (
            "branch_to_distinct_nodes",
            {
                "entry": "a",
                "nodes": [
                    {"id": "a", "agentCode": "x"},
                    {"id": "b", "agentCode": "y"},
                    {"id": "c", "agentCode": "z"},
                ],
                "edges": [
                    {"from": "a", "to": "b", "when": "user_input == 'x'"},
                    {"from": "a", "to": "c", "when": "user_input == 'y'"},
                ],
            },
        ),
        (
            "cycle_and_orphan",
            {
                **base(),
                "nodes": [*base()["nodes"], {"id": "lonely", "agentCode": "x"}],
                "edges": [*base()["edges"], {"from": "c", "to": "a"}],
            },
        ),
        (
            "edge_from_missing_node",
            {**base(), "edges": [*base()["edges"], {"from": "ghost", "to": "c"}]},
        ),
        ("default_input_template", base()),
        (
            "template_upstream_only",
            {**base(), "inputTemplate": "问题：{{user_input}}\n上游：{{upstream}}"},
        ),
        (
            "multiple_defects",
            {
                **base(),
                "entry": "ghost",
                "edges": [
                    {"from": "a", "to": "b", "when": "user_input == 'x'"},
                    {"from": "a", "to": "c"},
                ],
            },
        ),
    ]
    return cases


def build_combination_cases() -> list[tuple[str, dict[str, Any]]]:
    """构造缺陷原子的 1~3 元组合用例。

    11 个原子、每个最多取 3 个，组合数 = C(11,1) + C(11,2) + C(11,3) = 231。
    多个缺陷同时存在时，「谁先抛」正是最容易悄悄漂移的地方。
    """
    cases: list[tuple[str, dict[str, Any]]] = []
    for size in (1, 2, 3):
        for picked in combinations(ATOMS, size):
            payload = build_base_payload()
            for name, mutate in picked:
                mutate(payload)
            label = "+".join(name for name, _ in picked)
            cases.append((f"combo:{label}", payload))
    return cases


def load_old_module(base_commit: str) -> types.ModuleType:
    """从 base commit 反向重建改造前的模块并加载。

    Raises:
        RuntimeError: 重建结果与当前文件相同（自比）或结构不符合预期。
    """
    old_src = subprocess.check_output(
        ["git", "show", f"{base_commit}:{TARGET_FILE}"], cwd=REPO_ROOT
    ).decode("utf-8")
    new_src = (REPO_ROOT / TARGET_FILE).read_text(encoding="utf-8")

    if old_src == new_src:
        raise RuntimeError(
            f"base commit {base_commit} 的 {TARGET_FILE} 与当前文件完全一致，"
            "无法构成对照——请换一个更早的 base commit。"
        )
    for name in REMOVED_METHODS:
        if f"def {name}" not in old_src:
            raise RuntimeError(f"重建的旧文件里找不到 def {name}，重建结果不对")
        if f"def {name}" in new_src:
            raise RuntimeError(f"当前文件里仍存在 def {name}，改造未完成")
    if "def collect_graph_issues" in old_src:
        raise RuntimeError("重建的旧文件里已经出现 collect_graph_issues，base commit 选错了")

    module = types.ModuleType("old_workflow_def")
    sys.modules["old_workflow_def"] = module
    exec(compile(old_src, "old_workflow_def.py", "exec"), module.__dict__)
    return module


def collect_messages(module: types.ModuleType, payload: dict[str, Any]) -> list[str]:
    """跑一次校验，返回 pydantic 报出的全部消息文本（成功时为空列表）。"""
    try:
        module.WorkflowDefinition.model_validate(copy.deepcopy(payload))
    except ValidationError as exc:
        return [err["msg"] for err in exc.errors()]
    except Exception as exc:  # noqa: BLE001 - 非校验异常也要如实记录
        return [f"<{type(exc).__name__}: {exc}>"]
    return []


def main() -> int:
    """跑差分比对并打印结果，返回进程退出码。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        default="a1bdbc8",
        help="改造前的 base commit（默认 a1bdbc8）",
    )
    args = parser.parse_args()

    old_module = load_old_module(args.base)
    from app.core import workflow_def as new_module

    cases = build_explicit_cases() + build_combination_cases()
    diffs: list[tuple[str, list[str], list[str]]] = []
    for name, payload in cases:
        old_msgs = collect_messages(old_module, payload)
        new_msgs = collect_messages(new_module, payload)
        if old_msgs != new_msgs:
            diffs.append((name, old_msgs, new_msgs))

    print(f"base commit      : {args.base}")
    print(f"显式用例         : {len(build_explicit_cases())}")
    print(f"组合用例         : {len(build_combination_cases())}")
    print(f"合计用例         : {len(cases)}")
    print(f"消息不一致用例数 : {len(diffs)}")
    for name, old_msgs, new_msgs in diffs[:20]:
        print(f"  - {name}\n      old: {old_msgs}\n      new: {new_msgs}")
    if diffs:
        print("❌ 存在不一致")
        return 1
    print("✅ 全部逐字一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
