"""Agent 资源的 JSON 形状与跨表校验（V4.10）。

页面表单与 JSON API 共用同一套规则：选中的 Skill 是否存在、选中的 MCP 工具
是否可用、选中的 LLM 连接是否启用且有默认模型——这些判断**只在这里**
实现一次，两个入口都调用它。

**为什么不复用 ``agent_forms``**：那个模块的入参是「表单」——一个会按名字
取值的对象，值都是字符串。JSON 请求里 ``tools`` 与 ``skills`` 是真正的
数组，``responseSchema`` 是对象而不是文本。硬套表单解析会让数组被逗号切
开、Schema 被当成字符串，所以这里按 JSON 形状重新实现，但**判断规则一致**。
"""

from __future__ import annotations

from typing import Any

from app.core.agent_response_format import parse_response_format, parse_response_schema
from app.core.skill_names import SkillConfigError, normalize_skill_codes
from app.core.tools import normalize_tool_names
from app.repo.llm_endpoint_repo import llm_endpoint_repo
from app.repo.mcp_repo import mcp_tool_repo
from app.repo.skill_repo import skill_repo


def agent_payload(row: Any) -> dict[str, Any]:
    """把 Agent 行整理成前端可直接消费的形状。"""
    return {
        "code": row.code,
        "name": row.name,
        "description": row.description or "",
        "systemPrompt": row.system_prompt or "",
        "model": row.model or "",
        "llmEndpointCode": row.llm_endpoint_code or "",
        "tools": list(row.tools or []),
        "skills": list(row.skills or []),
        "responseFormat": row.response_format or "text",
        "responseSchema": row.response_schema,
        "enabled": bool(row.enabled),
        "createdAt": row.created_at.isoformat() if row.created_at else None,
        "updatedAt": row.updated_at.isoformat() if row.updated_at else None,
    }


async def resolve_agent_values(
    db, tenant_id: str | None, body: dict[str, Any], *, partial: bool = False
) -> dict[str, Any]:
    """校验跨表引用（Skill / MCP 工具 / LLM 连接）并整理成写入用的值。

    规则与页面表单**完全一致**（见 ``agent_forms._agent_form_values``），
    只是入参从表单换成了 JSON 对象。

    Raises:
        ValueError: 业务约束不满足；错误码就是字段名
            （``invalid_llm_endpoint`` / ``llm_model_required`` /
            ``invalid_skill_binding`` / ``invalid_response_format`` /
            ``invalid_response_schema``）。
    """
    values: dict[str, Any] = {}

    def wanted(name: str) -> bool:
        """该字段这次是否要写。"""
        return not partial or name in body

    if wanted("name"):
        values["name"] = str(body.get("name") or "")
    if wanted("description"):
        values["description"] = str(body.get("description") or "")
    if wanted("systemPrompt"):
        values["system_prompt"] = str(body.get("systemPrompt") or "")

    if wanted("skills"):
        skills = {
            row.code
            for row in await skill_repo.list(
                db, only_enabled=True, limit=1000, tenant_id=tenant_id
            )
        }
        try:
            normalized_skills = normalize_skill_codes(body.get("skills"), skills)
        except SkillConfigError as exc:
            raise ValueError("invalid_skill_binding") from exc
        values["skills"] = [] if normalized_skills is None else normalized_skills

    if wanted("tools"):
        mcp_names = await mcp_tool_repo.qualified_names(
            db, tenant_id=tenant_id, only_enabled=True
        )
        values["tools"] = normalize_tool_names(body.get("tools"), mcp_names)

    if wanted("responseFormat"):
        values["response_format"] = parse_response_format(body.get("responseFormat"))
    if wanted("responseSchema"):
        values["response_schema"] = parse_response_schema(body.get("responseSchema"))

    if wanted("llmEndpointCode") or wanted("model"):
        if partial and "llmEndpointCode" not in body:
            # 只改模型：连接没动，不必重新校验它。
            model = str(body.get("model") or "").strip()
            if not model:
                raise ValueError("llm_model_required")
            values["model"] = model
            return values
        endpoint_code = str(body.get("llmEndpointCode") or "").strip()
        endpoint = (
            await llm_endpoint_repo.get_by_code(db, endpoint_code)
            if endpoint_code
            else None
        )
        if endpoint is None or not endpoint.enabled:
            raise ValueError("invalid_llm_endpoint")
        model = str(body.get("model") or "").strip() or endpoint.model
        if not model:
            raise ValueError("llm_model_required")
        values["llm_endpoint_code"] = endpoint_code
        values["model"] = model
    return values


def field_errors(exc: Any) -> dict[str, str]:
    """把 pydantic 校验错误压成「字段 → 提示」的扁平字典。"""
    errors: dict[str, str] = {}
    for item in exc.errors():
        location = item.get("loc") or ("form",)
        field = str(location[-1])
        errors.setdefault(field, item.get("msg") or "字段不合法")
    return errors