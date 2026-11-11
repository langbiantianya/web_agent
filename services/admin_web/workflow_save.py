"""工作流保存前的校验准备。

画布上的「保存工作流」是一张表单，提交的是一份图定义（JSON 文本）。
服务端要做两件事：

1. 把这份定义变成**可落库**的 :class:`WorkflowUpdate`；
2. 落不了库时，给出一组**可读**的逐条错误，而不是一句通用的
   「提交的值无效」。

这两件事原先直接写在路由里，路由文件因此超出单文件行数上限；这里
抽出来，路由只负责鉴权、落库与响应。

## 为什么要把错误拆开

画布上的错误条能逐条显示（图上哪个节点不合法、缺哪条边）。管理员
在图上花了几分钟才排好的问题，如果只回一句「无效」，就只能对着
一句没有信息量的话重新猜——所以这里返回的是**结构化**的错误列表。
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from app.core.workflow_def import WorkflowDefinition, WorkflowNode
from app.repo.agent_repo import agent_repo
from app.schemas.workflow import WorkflowCreate, WorkflowUpdate
from services.admin_web.common import as_bool, json_object
from services.admin_web.workflow_validation import (
    _agent_binding_errors,
    _definition_errors,
)

__all__ = [
    "prepare_workflow_create",
    "prepare_workflow_update",
    "submitted_definition",
]


def submitted_definition(form: Any) -> dict:
    """从表单里取出管理员提交的图定义。

    提交的是 JSON 文本；解析不出来时返回空字典，让调用方退回到一句
    通用提示，而不是把异常抛给用户。
    """
    try:
        raw = json_object(form.get("definition"))
    except (ValidationError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


async def prepare_workflow_update(
    db: Any,
    form: Any,
    *,
    tenant_id: str,
    locale: str = "zh-CN",
) -> tuple[WorkflowUpdate | None, list[dict[str, Any]]]:
    """校验一次工作流更新。

    返回 ``(payload, errors)``：

    - ``payload`` 非空：定义合法，可以落库，``errors`` 为空列表；
    - ``payload`` 为空：定义不合法，``errors`` 是逐条可读的错误。

    错误来源有两处：定义本身的图规则（成环、缺入口……）与跨模块的
    Agent 引用（节点指向的 Agent 不存在或已停用）。两者都要报到管理员
    面前，否则画布上只标红节点、却不说「这个 Agent 已经删了」。
    """
    definition = submitted_definition(form)
    try:
        payload = WorkflowUpdate(
            name=str(form.get("name") or ""),
            description=str(form.get("description") or ""),
            definition=json_object(form.get("definition")),
            enabled=as_bool(form.get("enabled"), True),
        )
    except (ValidationError, ValueError):
        errors = _definition_errors(definition, locale=locale)
        if not errors:
            errors = [
                {
                    "message": (
                        "The workflow definition is invalid."
                        if locale == "en"
                        else "工作流定义不合法。"
                    )
                }
            ]
        return None, errors

    binding_errors = await _agent_binding_errors(
        db,
        payload.definition.model_dump(by_alias=True, exclude_none=True),
        tenant_id,
        locale=locale,
    )
    if binding_errors:
        return None, binding_errors
    return payload, []


async def prepare_workflow_create(
    db: Any,
    form: Any,
    *,
    tenant_id: str,
) -> tuple[WorkflowCreate | None, str]:
    """校验一次工作流创建。

    返回 ``(payload, error)``：``payload`` 非空表示可以落库；否则
    ``error`` 是稳定的错误码（``invalid`` / ``duplicate``），由路由
    翻成 Flash 提示。

    新表单只收一个「初始 Agent code」，服务端据此生成单节点骨架
    definition，剩下的图交给编排页画。老的 ``definition`` JSON 表单
    字段仍然接受，避免既有调用方（含既有测试）突然失效。
    """
    agent_code = str(form.get("agent_code") or "").strip()
    raw_definition = str(form.get("definition") or "").strip()
    if agent_code:
        initial_agent = await agent_repo.get_by_code(
            db, agent_code, tenant_id=tenant_id
        )
        if initial_agent is None or not initial_agent.enabled:
            # 初始 Agent 必须真实存在，否则建出来的编排一跑就找不到 Agent。
            return None, "invalid"
        try:
            payload = WorkflowCreate(
                code=str(form.get("code") or ""),
                name=str(form.get("name") or ""),
                description=str(form.get("description") or ""),
                definition=WorkflowDefinition(
                    entry="step1",
                    nodes=[
                        WorkflowNode(
                            id="step1",
                            type="agent",
                            agentCode=agent_code,
                        )
                    ],
                    edges=[],
                ),
                enabled=as_bool(form.get("enabled"), True),
            )
        except (ValidationError, ValueError):
            return None, "invalid"
        return payload, ""
    if not raw_definition:
        return None, "invalid"
    try:
        definition = json_object(raw_definition)
        payload = WorkflowCreate(
            code=str(form.get("code") or ""),
            name=str(form.get("name") or ""),
            description=str(form.get("description") or ""),
            definition=definition,
            enabled=as_bool(form.get("enabled"), True),
        )
    except (ValidationError, ValueError):
        return None, "invalid"
    if await _agent_binding_errors(db, definition, tenant_id):
        return None, "invalid"
    return payload, ""
