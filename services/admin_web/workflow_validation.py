"""Workflow definition diagnostics shared by the admin pages.

The graph rules remain in :mod:`app.core.workflow_def`.  This module only turns
them into localized, UI-addressable errors and checks the cross-module Agent
references before a workflow is saved.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import ValidationError

from app.core.workflow_def import WorkflowDefinition, collect_graph_issues
from app.repo.agent_repo import agent_repo

logger = logging.getLogger(__name__)

_VALUE_ERROR_PREFIX = "Value error, "

_PYDANTIC_MESSAGE_WITH_FIELD = {
    "missing": "缺少必填字段 {field}",
    "extra_forbidden": "不支持的字段 {field}",
    "string_type": "字段 {field} 必须是字符串",
    "int_type": "字段 {field} 必须是整数",
    "int_parsing": "字段 {field} 必须是整数",
    "int_from_float": "字段 {field} 必须是整数",
    "bool_type": "字段 {field} 必须是布尔值",
    "too_long": "字段 {field} 长度超出限制",
    "string_too_long": "字段 {field} 长度超出限制",
    "list_too_long": "字段 {field} 长度超出限制",
    "too_short": "字段 {field} 长度不足",
    "string_too_short": "字段 {field} 长度不足",
    "greater_than_equal": "字段 {field} 小于允许的最小值",
    "less_than_equal": "字段 {field} 大于允许的最大值",
    "list_type": "字段 {field} 必须是数组",
    "model_type": "字段 {field} 必须是对象",
}

_PYDANTIC_MESSAGE_NO_FIELD = {
    "missing": "缺少必填字段",
    "extra_forbidden": "存在不支持的字段",
    "string_type": "字段必须是字符串",
    "int_type": "字段必须是整数",
    "int_parsing": "字段必须是整数",
    "int_from_float": "字段必须是整数",
    "bool_type": "字段必须是布尔值",
    "too_long": "字段长度超出限制",
    "string_too_long": "字段长度超出限制",
    "list_too_long": "字段长度超出限制",
    "too_short": "字段长度不足",
    "string_too_short": "字段长度不足",
    "greater_than_equal": "字段小于允许的最小值",
    "less_than_equal": "字段大于允许的最大值",
    "list_type": "字段必须是数组",
    "model_type": "字段必须是对象",
}

_PYDANTIC_MESSAGE_WITH_FIELD_EN = {
    "missing": "Required field {field} is missing",
    "extra_forbidden": "Field {field} is not supported",
    "string_type": "Field {field} must be a string",
    "int_type": "Field {field} must be an integer",
    "int_parsing": "Field {field} must be an integer",
    "int_from_float": "Field {field} must be an integer",
    "bool_type": "Field {field} must be a boolean",
    "too_long": "Field {field} is too long",
    "string_too_long": "Field {field} is too long",
    "list_too_long": "Field {field} is too long",
    "too_short": "Field {field} is too short",
    "string_too_short": "Field {field} is too short",
    "greater_than_equal": "Field {field} is below the minimum",
    "less_than_equal": "Field {field} is above the maximum",
    "list_type": "Field {field} must be an array",
    "model_type": "Field {field} must be an object",
}

_PYDANTIC_MESSAGE_NO_FIELD_EN = {
    "missing": "A required field is missing",
    "extra_forbidden": "The definition contains an unsupported field",
    "string_type": "The field must be a string",
    "int_type": "The field must be an integer",
    "int_parsing": "The field must be an integer",
    "int_from_float": "The field must be an integer",
    "bool_type": "The field must be a boolean",
    "too_long": "The field is too long",
    "string_too_long": "The field is too long",
    "list_too_long": "The field is too long",
    "too_short": "The field is too short",
    "string_too_short": "The field is too short",
    "greater_than_equal": "The field is below the minimum",
    "less_than_equal": "The field is above the maximum",
    "list_type": "The field must be an array",
    "model_type": "The field must be an object",
}


def _strip_value_error_prefix(message: str) -> str:
    if message.startswith(_VALUE_ERROR_PREFIX):
        return message[len(_VALUE_ERROR_PREFIX):]
    return message


def _pydantic_field_path(loc: tuple, nodes: list) -> str | None:
    if not loc:
        return None
    last = loc[-1]
    if not isinstance(last, str):
        return None
    if len(loc) == 1:
        return None if last in {"nodes", "edges"} else last
    index = loc[1]
    if not isinstance(index, int):
        return last
    container = str(loc[0])
    if container == "nodes":
        node = nodes[index] if index < len(nodes) else None
        node_id = node.get("id") if isinstance(node, dict) else None
        if isinstance(node_id, str) and node_id:
            return f"{node_id}.{last}"
        return f"nodes[{index}].{last}"
    return f"{container}[{index}].{last}"


def _english_workflow_message(message: str) -> str:
    """Translate stable workflow diagnostics for the English admin UI."""
    exact = {
        "definition 必须是对象": "The workflow definition must be an object",
        "nodes 必须是数组": "nodes must be an array",
        "edges 必须是数组": "edges must be an array",
        "definition 存在环，无法确定执行顺序":
            "The workflow contains a cycle, so execution order is undefined",
        "节点 id 重复": "Node IDs must be unique",
    }
    if message in exact:
        return exact[message]

    patterns = (
        (
            r"^nodes\[(\d+)\] 不是对象，已跳过$",
            r"nodes[\1] is not an object and was skipped",
        ),
        (
            r"^nodes\[(\d+)\] 缺少合法 id，已跳过$",
            r"nodes[\1] has no valid ID and was skipped",
        ),
        (
            r"^edges\[(\d+)\] 不是对象，已跳过$",
            r"edges[\1] is not an object and was skipped",
        ),
        (
            r"^edge 引用了不存在的节点：(.+)$",
            r"Edge references an unknown node: \1",
        ),
        (r"^entry 节点不存在：(.+)$", r"Entry node does not exist: \1"),
        (
            r"^以下节点从 entry 走不到（孤立节点）：(.+)$",
            r"Nodes unreachable from the entry: \1",
        ),
        (
            r"^节点 (.+) 是 agent 类型，必须指定 agentCode$",
            r"Node \1 is an agent node and must specify agentCode",
        ),
        (
            r"^节点 (.+) 是 approval 类型，必须写 prompt$",
            r"Node \1 is an approval node and must include a prompt",
        ),
        (
            r"^节点 (.+) 是 approval 类型，不能同时指定 agentCode$",
            r"Node \1 is an approval node and cannot specify agentCode",
        ),
        (
            r"^节点 (.+) 是 agent 类型，不能同时写审批字段$",
            r"Node \1 is an agent node and cannot include approval fields",
        ),
        (
            r"^节点 (.+) 是审批节点，不能同时指定 outputKey 或 inputTemplate$",
            r"Node \1 is an approval node and cannot specify outputKey or inputTemplate",
        ),
        (
            r"^节点 (.+) 是汇聚节点（(\d+) 条入边），但入边里混了条件边（(.+)）：条件不成立时它会永远等不到。请把入边全部改成无条件边，或让条件节点直接连到 (.+) 的下游。$",
            r"Node \1 is a join with \2 incoming edges, but some are conditional (\4). It can wait forever when a condition is false. Use only unconditional incoming edges or connect the conditional node directly downstream of \5.",
        ),
        (
            r"^节点 (.+) 的 (\d+) 条入边全是条件边：本版不支持「多条条件边汇聚到同一节点」，请改成无条件汇聚。$",
            r"All \2 incoming edges to node \1 are conditional. Conditional joins are not supported; use an unconditional join.",
        ),
        (
            r"^节点 (.+) 的出边里条件边与无条件边混用：请统一成「全无条件（并行）」或「全条件（分支）」。$",
            r"Node \1 mixes conditional and unconditional outgoing edges. Use either all unconditional edges (parallel) or all conditional edges (branches).",
        ),
        (
            r"^边 (.+ -> .+) 的 when 表达式有语法错误：(.+)$",
            r"The when expression on edge \1 has a syntax error: \2",
        ),
        (
            r"^边 (.+ -> .+) 的 when 引用了不存在的 upstream\.(.+)；本图可用的 outputKey：(.+)$",
            r"The when expression on edge \1 references missing upstream.\3. Available output keys: \4",
        ),
        (
            r"^边 (.+ -> .+) 的 when 引用了未知变量 (.+?)(；请写成 upstream\.(.+))?；可用：user_input、upstream\.<outputKey>$",
            r"The when expression on edge \1 references unknown variable \3. Use user_input or upstream.<outputKey>.",
        ),
        (
            r"^inputTemplate 里的 \{\{(.+?)\.xxx\}\} 不支持点号访问；单独引用某个上游产出请直接写 \{\{(.+?)\}\}，全部上游写 \{\{upstream\}\}$",
            r"inputTemplate does not support dotted access in {{{\1.xxx}}}. Reference one upstream value with {{{\2}}} or all upstream values with {{upstream}}.",
        ),
        (
            r"^inputTemplate 引用了未知变量：(.+)；可用：user_input、upstream、(.*)$",
            r"inputTemplate references unknown variables: \1. Available: user_input, upstream, \2",
        ),
    )
    for pattern, replacement in patterns:
        if re.search(pattern, message):
            return re.sub(pattern, replacement, message)
    return message


def _pydantic_message(
    error_type: str,
    raw: str,
    field: str | None,
    *,
    locale: str = "zh-CN",
) -> str:
    english = locale == "en"
    if error_type == "value_error":
        message = _strip_value_error_prefix(raw)
        return _english_workflow_message(message) if english else message
    table = (
        _PYDANTIC_MESSAGE_WITH_FIELD_EN
        if english
        else _PYDANTIC_MESSAGE_WITH_FIELD
    )
    if field:
        template = table.get(error_type)
        if template is not None:
            return template.format(field=field)
    else:
        message = (
            _PYDANTIC_MESSAGE_NO_FIELD_EN
            if english
            else _PYDANTIC_MESSAGE_NO_FIELD
        ).get(error_type)
        if message is not None:
            return message
    logger.warning(
        "workflow definition 校验出现未归一的 pydantic 错误类型：type=%r msg=%r",
        error_type,
        raw,
    )
    if english:
        return f"Field {field} is invalid" if field else "The definition is invalid"
    return f"字段 {field} 不合法" if field else "定义不合法"


def _definition_errors(
    definition: dict, *, locale: str = "zh-CN"
) -> list[dict]:
    """Flatten Pydantic and graph issues into UI-addressable diagnostics."""
    errors: list[dict] = []
    nodes = definition.get("nodes") or []
    graph_issues = collect_graph_issues(definition)
    graph_messages = {issue.message for issue in graph_issues}
    try:
        WorkflowDefinition.model_validate(definition)
    except ValidationError as exc:
        for item in exc.errors():
            loc = item.get("loc") or ()
            message = str(item.get("msg") or "invalid")
            if not loc and _strip_value_error_prefix(message) in graph_messages:
                continue
            field = _pydantic_field_path(loc, nodes)
            entry: dict[str, str] = {
                "message": _pydantic_message(
                    str(item.get("type") or ""),
                    message,
                    field,
                    locale=locale,
                )
            }
            if field:
                entry["field"] = field
            if len(loc) >= 2 and loc[0] == "nodes" and isinstance(loc[1], int):
                node = nodes[loc[1]] if loc[1] < len(nodes) else None
                if isinstance(node, dict) and isinstance(node.get("id"), str):
                    entry["node"] = node["id"]
            errors.append(entry)
    for issue in graph_issues:
        entry = {
            "message": (
                _english_workflow_message(issue.message)
                if locale == "en"
                else issue.message
            )
        }
        if issue.node_ids:
            entry["node"] = issue.node_ids[0]
            entry["nodes"] = list(issue.node_ids)
        if issue.edge_refs:
            entry["edge"] = issue.edge_refs[0]
            entry["edges"] = list(issue.edge_refs)
        errors.append(entry)
    return errors


async def _agent_binding_errors(
    db,
    definition: dict,
    tenant_id: str,
    *,
    locale: str = "zh-CN",
) -> list[dict]:
    """Verify that every Agent node references an enabled local Agent."""
    errors = []
    for node in definition.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        if str(node.get("type") or "agent") != "agent":
            continue
        agent_code = str(node.get("agentCode") or "").strip()
        if not agent_code:
            continue
        agent = await agent_repo.get_by_code(db, agent_code, tenant_id=tenant_id)
        if agent is not None and bool(agent.enabled):
            continue
        node_id = str(node.get("id") or "")
        message = (
            f"Agent {agent_code} does not exist or is disabled"
            if locale == "en"
            else f"Agent {agent_code} 不存在或已停用"
        )
        entry: dict[str, Any] = {
            "message": message,
            "code": "agent_unavailable",
            "agent": agent_code,
        }
        if node_id:
            entry["node"] = node_id
            entry["nodes"] = [node_id]
        errors.append(entry)
    return errors


__all__ = ["_agent_binding_errors", "_definition_errors", "_pydantic_message"]
