"""工作流列表、编排页与 definition 校验辅助。"""

from __future__ import annotations

import logging
from typing import Any

from ludic.web import Request
from pydantic import ValidationError

from app.core.workflow_def import WorkflowDefinition, collect_graph_issues
from app.repo.agent_repo import agent_repo
from app.repo.workflow_repo import workflow_repo
from services.admin_web.listing import parse_list_params, search_term
from services.admin_web.common import (
    can_edit_resource, flash_from_query, not_found, tenant_id_of,
    unauthorized, undo_id_from_query, with_auth,
)
from services.admin_web.registry import can_access_page, visible_pages
from services.admin_web.views import workflow_editor_page, workflows_page

logger = logging.getLogger(__name__)


_VALUE_ERROR_PREFIX = "Value error, "


_PYDANTIC_MESSAGE_WITH_FIELD: dict[str, str] = {
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


_PYDANTIC_MESSAGE_NO_FIELD: dict[str, str] = {
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


def _strip_value_error_prefix(message: str) -> str:
    """剥掉 pydantic 给 ``ValueError`` 加的英文前缀。

    剥掉后剩下的通常是我们自己写的中文消息（节点字段组合校验、
    模型级图校验），直接透出即可。
    """
    if message.startswith(_VALUE_ERROR_PREFIX):
        return message[len(_VALUE_ERROR_PREFIX) :]
    return message


def _pydantic_field_path(loc: tuple, nodes: list) -> str | None:
    """把 pydantic 的 ``loc`` 渲染成可读的字段路径。

    - ``('nodes', 0, 'id')`` 且能反查到节点 id 时给 ``step1.id``；
      反查不到（节点自己就缺 id）时退回 ``nodes[0].id``；
    - ``('nodes', 0)`` 定位的是**节点整体**，没有字段名；
    - ``('nodes',)`` 与空 ``loc`` 是容器/整体层面的报错，也没有字段名。

    Returns:
        可读字段路径；渲染不出来时返回 None。
    """
    if not loc:
        return None
    last = loc[-1]
    if not isinstance(last, str):
        return None
    if len(loc) == 1:
        # ('nodes',) / ('edges',) 是容器层面的报错，('entry',) 才是字段名
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


def _pydantic_message(error_type: str, raw: str, field: str | None) -> str:
    """把 pydantic 的英文内部文案翻成中文。

    未覆盖的错误类型**不再把英文原文透出去**——后台是中文界面，
    英文内部文案不该出现在管理员眼前。原始 ``type`` 与 ``msg`` 改记到
    日志，信息不丢，只是从响应挪到服务端。
    """
    if error_type == "value_error":
        # 我们自己抛的 ValueError，消息本来就是中文，只去掉英文前缀
        return _strip_value_error_prefix(raw)
    if field:
        template = _PYDANTIC_MESSAGE_WITH_FIELD.get(error_type)
        if template is not None:
            return template.format(field=field)
    else:
        message = _PYDANTIC_MESSAGE_NO_FIELD.get(error_type)
        if message is not None:
            return message
    logger.warning(
        "workflow definition 校验出现未归一的 pydantic 错误类型：type=%r msg=%r",
        error_type,
        raw,
    )
    return f"字段 {field} 不合法" if field else "定义不合法"


def _definition_errors(definition: dict) -> list[dict]:
    """把一份 definition 的全部问题摊平成画布能定位的条目。

    两路串联，缺一不可：

    1. **pydantic** 负责**节点级**问题（id 缺失/重复、未知节点类型、
       agent 节点缺 agentCode、approval 节点缺 prompt…）。它给出的
       ``loc`` 形如 ``("nodes", 0, "id")``，据此能反查是哪个节点。
    2. **collect_graph_issues** 负责**图级**问题（环、孤立节点、条件边
       混用、when 表达式），并直接带出 ``node_ids`` / ``edge_refs``。

    只跑其中一路会漏：pydantic 的 model 级报错会把整张图压成一条
    没有定位的信息；collect_graph_issues 则完全不看节点自身的字段组合。

    模型级条目与图级条目重复时只留图级那条（带定位），详见循环内的说明。
    """
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
            if not loc:
                # 模型级报错：WorkflowDefinition 的模型级校验器把
                # collect_graph_issues 的首条 issue 原样抛上来，与下面那条
                # 图级条目重复。按**消息**去重而不是无条件丢弃——将来
                # WorkflowDefinition 新增别的模型级校验器时不会误丢消息。
                if _strip_value_error_prefix(message) in graph_messages:
                    continue
            field = _pydantic_field_path(loc, nodes)
            entry: dict[str, str] = {
                "message": _pydantic_message(
                    str(item.get("type") or ""), message, field
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
        entry = {"message": issue.message}
        if issue.node_ids:
            # 单值 ``node`` / ``edge`` 先于整组存在，保留给按单值取用的调用方；
            # 整组 ``nodes`` / ``edges`` 让画布能把整个环、整组孤立节点一次标红，
            # 而不是只红一个节点让管理员自己顺着箭头找。
            entry["node"] = issue.node_ids[0]
            entry["nodes"] = list(issue.node_ids)
        if issue.edge_refs:
            entry["edge"] = issue.edge_refs[0]
            entry["edges"] = list(issue.edge_refs)
        errors.append(entry)
    return errors


def register(app: Any) -> None:
    """把工作流列表与编排页的路由挂到 app 上。"""

    @app.get("/admin/workflows")
    async def workflows_get(request: Request):
        """渲染 Workflow 编排列表。"""
        values = await with_auth(request)
        if values is None:
            return unauthorized()
        db, principal, pref, csrf = values
        try:
            if not await can_access_page(db, principal, "workflows"):
                return not_found()
            flash, kind = flash_from_query(request)
            undo_id = undo_id_from_query(request)
            # 列表页的搜索 / 排序 / 分页（UX-06）。
            list_params = parse_list_params(
                request,
                allowed_sorts=("name", "code"),
            )
            search = search_term(list_params.q)
            total = await workflow_repo.count(db, search=search or None)
            return workflows_page(
                principal=principal,
                csrf=csrf,
                pages=await visible_pages(db, principal),
                rows=await workflow_repo.list(
                    db,
                    limit=list_params.size,
                    offset=list_params.offset,
                    search=search or None,
                    sort=list_params.sort,
                ),
                agents=await agent_repo.list(
                    db, only_enabled=True, limit=500, tenant_id=tenant_id_of(principal)
                ),
                theme=pref.theme,
                color_mode=pref.color_mode,
                density=pref.density,
                can_write=await can_edit_resource(
                    db,
                    principal,
                    page_code="workflows",
                    component_code="workflow_editor",
                ),
                flash=flash,
                undo_id=undo_id,
                list_params=list_params,
                list_total=total,
            )
        finally:
            await db.close()

    @app.get("/admin/workflows/{code}")
    async def workflow_editor_get(code: str, request: Request):
        """渲染 Workflow 画布编排页。

        只读页：能不能看由页面权限决定，能不能改由编排组件权限决定。
        编排不存在与无权访问都返回 404，不向外区分这两种情况。
        """
        values = await with_auth(request)
        if values is None:
            return unauthorized()
        db, principal, pref, csrf = values
        try:
            if not await can_access_page(db, principal, "workflows"):
                return not_found()
            row = await workflow_repo.get_by_code(db, code)
            if row is None:
                return not_found()
            agents = await agent_repo.list(
                db, only_enabled=True, limit=500, tenant_id=tenant_id_of(principal)
            )
            # 保存成功后会被重定向回本页并带上 notice=，这里把它翻成页面提示。
            # flash_from_query 只认固定白名单里的查询值，绝不回显任意文本。
            flash, _kind = flash_from_query(request)
            return workflow_editor_page(
                principal=principal,
                csrf=csrf,
                pages=await visible_pages(db, principal),
                workflow=row,
                agents=agents,
                theme=pref.theme,
                color_mode=pref.color_mode,
                density=pref.density,
                can_write=await can_edit_resource(
                    db,
                    principal,
                    page_code="workflows",
                    component_code="workflow_editor",
                ),
                flash=flash,
            )
        finally:
            await db.close()
