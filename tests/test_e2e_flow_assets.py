"""端到端验收流程的**资产**守卫（不跑真模型）。

``scripts/e2e_agent_skill_mcp_workflow.py`` 与 ``scripts/e2e_mcp_server.py`` 是
部署环境里跑的脚本，不进日常单测。但它们的**形状**会悄悄坏掉——比如工作流
定义不再合法、技能正文里的工具限定名与 MCP 服务器 code 对不上、夹具服务器
少了工具。这些问题在真跑一次之前没人会发现，而真跑一次要花钱（MiniMax
按 token 计费）。

所以这里只断言「形状」：不调模型、不起子进程。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from scripts import e2e_agent_skill_mcp_workflow as flow

ROOT = Path(__file__).resolve().parents[1]


def test_workflow_definition_is_valid():
    """工作流定义必须能通过与保存期同一份校验。"""
    from app.core.workflow_def import WorkflowDefinition

    definition = flow.WorkflowDefinition.model_validate(flow.WORKFLOW_DEF)
    assert definition.entry == "collect"
    assert [n.id for n in definition.nodes] == ["collect", "gate", "report"]
    # 审批节点不能带 agentCode，agent 节点必须带。
    gate = next(n for n in definition.nodes if n.id == "gate")
    assert gate.is_approval and not gate.agentCode
    collect = next(n for n in definition.nodes if n.id == "collect")
    assert not collect.is_approval and collect.agentCode == flow.AGENT_CODE


def test_skill_body_points_at_the_mcp_tool():
    """技能正文必须点名那个 MCP 工具——这是「技能注入生效」的证据来源。"""
    assert flow.TOOL_ADD in flow.SKILL_BODY
    assert "E2E_SUM" in flow.SKILL_BODY


def test_skill_body_points_at_the_studio_script():
    """技能正文也要点名 studio 要执行的脚本。

    模型只有拿到这段正文才会去调 ``skill_exec``——所以「模型真的调了
    skill_exec」反过来证明「技能正文真的注入进提示词了」。
    """
    assert flow.SCRIPT_PATH in flow.SKILL_BODY
    assert "skill_exec" in flow.SKILL_BODY
    assert flow.SCRIPT_PATH.startswith("scripts/")


def test_studio_script_resource_is_under_scripts_dir():
    """脚本资源必须在 ``scripts/`` 下——那是 studio 执行器的口径（§22.8.3）。"""
    assert flow.SCRIPT_PATH.startswith("scripts/")
    assert flow.SCRIPT_MARK in flow.SCRIPT_BODY


def test_tool_qualified_names_match_server_code():
    """限定名由服务器 code + 工具名拼出，改了 code 就得同步改引用。"""
    from app.core.mcp.naming import build_qualified_name, parse_qualified_name

    assert flow.TOOL_ADD == build_qualified_name(flow.MCP_CODE, "add")
    server_code, tool_name = parse_qualified_name(flow.TOOL_ADD)
    assert server_code == flow.MCP_CODE
    assert tool_name == "add"


def test_mcp_server_fixture_exposes_expected_tools():
    """夹具 MCP 服务器必须提供流程用到的那几个工具。"""
    source = (ROOT / "scripts" / "e2e_mcp_server.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    tools = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(
            isinstance(dec, ast.Call)
            and getattr(dec.func, "attr", "") == "tool"
            for dec in node.decorator_list
        )
    }
    assert {"add", "echo", "always_fail"} <= tools


def test_fixture_tools_are_deterministic():
    """夹具工具的返回值必须带固定标记，断言才能对得上。"""
    source = (ROOT / "scripts" / "e2e_mcp_server.py").read_text(encoding="utf-8")
    assert "E2E_ECHO:" in source
    assert "E2E_SUM:" in source


def test_script_never_prints_secrets():
    """脚本不能把密钥、Token 或密文打出来。"""
    source = (ROOT / "scripts" / "e2e_agent_skill_mcp_workflow.py").read_text(
        encoding="utf-8"
    )
    for forbidden in ("api_key_cipher}", "decrypt(row.api_key_cipher))"):
        assert forbidden not in source
    # 密钥只报长度，不报内容。
    assert "密钥长度={len(plain)}" in source


EXPECTED_CASES = {
    "E2E-00",
    "E2E-01",
    "E2E-02",
    "E2E-03",
    "E2E-04",
    "E2E-05",
    "E2E-06",
    "E2E-07",
    "E2E-08",
    "E2E-09",
    "E2E-10",
    "E2E-11",
    "E2E-12",
    "E2E-13",
    "E2E-14",
    "E2E-15",
    "E2E-16",
    "E2E-17",
    "E2E-18",
    "E2E-19",
    "E2E-20",
    "E2E-21",
    "E2E-22",
    "E2E-23",
    "E2E-24",
    "E2E-25",
}


def test_every_case_id_is_reported():
    """26 条用例的编号都在脚本里被 ``report.add`` / ``report.skip`` 用上。

    编号只出现在注释或文档里不算数——它必须真的落进报告。
    """
    source = (ROOT / "scripts" / "e2e_agent_skill_mcp_workflow.py").read_text(
        encoding="utf-8"
    )
    reported = set(re.findall(r'report\.(?:add|skip)\(\s*"(E2E-\d+)"', source))
    assert reported == EXPECTED_CASES


