"""Agent 表单的解析与校验。

管理端 Agent 表单涉及跨表校验（选中的 Skill 是否存在、选中的 MCP 工具
是否可用、选中的 LLM 连接是否启用且有默认模型），这些规则**只在这里**
实现一次：路由层只负责把校验失败翻译成「保留输入 + 字段级错误」的
重定向（UX-03）。
"""

from __future__ import annotations

from typing import Any

from app.core.skill_names import SkillConfigError, normalize_skill_codes
from app.core.tools import normalize_tool_names
from app.repo.agent_repo import agent_repo
from app.repo.llm_endpoint_repo import llm_endpoint_repo
from app.repo.mcp_repo import mcp_tool_repo
from app.repo.skill_repo import skill_repo
from services.admin_web.common import as_bool, csv_list


def _form_list(form, name: str) -> list[str]:
    """读取复选框的多值；同时兼容旧表单的逗号分隔文本。"""
    getter = getattr(form, "getlist", None)
    values = getter(name) if callable(getter) else [form.get(name)]
    return csv_list(",".join(str(value or "") for value in values))


async def _agent_form_values(
    db,
    tenant_id: str,
    form,
    *,
    enabled_default: bool = True,
) -> dict:
    """解析并校验 Agent 表单，并解析所选 LLM 连接。"""
    skills = [row.code for row in await skill_repo.list(
        db, only_enabled=True, limit=1000, tenant_id=tenant_id
    )]
    mcp_names = await mcp_tool_repo.qualified_names(
        db, tenant_id=tenant_id, only_enabled=True
    )
    tools = normalize_tool_names(_form_list(form, "tools"), mcp_names)
    try:
        normalized_skills = normalize_skill_codes(
            _form_list(form, "skills"), set(skills)
        )
    except SkillConfigError as exc:
        raise ValueError("invalid_skill_binding") from exc
    llm_endpoint_code = str(form.get("llm_endpoint_code") or "").strip()
    endpoint = (
        await llm_endpoint_repo.get_by_code(db, llm_endpoint_code)
        if llm_endpoint_code
        else None
    )
    if endpoint is None or not endpoint.enabled:
        raise ValueError("invalid_llm_endpoint")
    model = str(form.get("model") or "").strip() or endpoint.model
    if not model:
        raise ValueError("llm_model_required")
    values = {
        "name": str(form.get("name") or ""),
        "description": str(form.get("description") or ""),
        "system_prompt": str(form.get("system_prompt") or ""),
        "model": model,
        "llm_endpoint_code": llm_endpoint_code,
        "tools": tools,
        "skills": [] if normalized_skills is None else normalized_skills,
        "enabled": as_bool(form.get("enabled"), enabled_default),
    }
    # 保留旧客户端兼容字段，但管理端新表单不再渲染它们。
    if "base_url" in form:
        values["base_url"] = str(form.get("base_url") or "")
    if "api_key" in form:
        values["api_key"] = str(form.get("api_key") or "")
    return values


def _business_field_errors(exc: ValueError) -> dict[str, str]:
    """把业务级校验失败映射到具体字段（UX-03）。

    ``_agent_form_values`` 用 :class:`ValueError` 表达业务约束
    （连接不存在 / 连接已停用 / 没有可用模型），错误码本身就是字段名。
    """
    code = str(exc)
    messages = {
        "invalid_llm_endpoint": (
            "llm_endpoint_code",
            "Select an enabled LLM connection.",
        ),
        "llm_model_required": (
            "model",
            "This connection has no default model; enter one.",
        ),
        "invalid_skill_binding": (
            "skills",
            "One or more selected skills no longer exist.",
        ),
    }
    if code in messages:
        field, message = messages[code]
        return {field: message}
    return {"name": "The submitted values are invalid."}
