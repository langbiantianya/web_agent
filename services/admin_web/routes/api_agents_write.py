"""Agent 资源的 JSON API —— 写侧（V4.10）。

创建、更新、启停与删除。权限与 CSRF 判定与页面路由完全一致（见
``api_support.authorize``）；差别只有请求体是 JSON、失败返回 JSON。

**删除前留快照**：误删能在撤销窗口内原样恢复，与页面路由的行为一致
（见 ``services.admin_web.deletion``）。
"""

from __future__ import annotations

from typing import Any

from ludic.web import Request
from pydantic import ValidationError

from app.core.crypto import encrypt
from app.repo.agent_repo import CLEAR, agent_repo
from app.repo.workflow_repo import workflow_repo
from app.schemas.agent import AgentCreate, AgentUpdate
from services.admin_web.agent_api import agent_payload, field_errors, resolve_agent_values
from services.admin_web.agent_forms import _business_field_errors
from services.admin_web.api_support import (
    ApiError,
    api_route,
    authorize,
    json_response,
    read_json_body,
    tenant_of,
)
from services.admin_web.audit import audit_principal_action
from services.admin_web.deletion import record_deletion

PAGE_CODE = "agents"
COMPONENT_CODE = "agent_editor"


def _invalid(exc: Exception) -> ApiError:
    """把校验失败翻译成带字段级错误的 API 错误。"""
    if isinstance(exc, ValidationError):
        return ApiError(
            "invalid_input",
            "提交的字段不合法",
            status_code=400,
            field_errors=field_errors(exc),
        )
    return ApiError(
        "invalid_input",
        "提交的字段不合法",
        status_code=400,
        field_errors=_business_field_errors(exc),
    )


def register(app: Any) -> None:
    """把 Agent 的写操作 JSON API 挂到 app 上。"""

    @app.post("/admin/api/agents")
    @api_route
    async def api_agents_create(request: Request):
        """创建 Agent。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=True
        )
        try:
            body = await read_json_body(request)
            tenant_id = tenant_of(principal)
            try:
                values = await resolve_agent_values(db, tenant_id, body, partial=True)
                payload = AgentCreate(
                    code=str(body.get("code") or "").strip(), **values
                )
            except (ValidationError, ValueError) as exc:
                raise _invalid(exc) from exc
            if await agent_repo.get_by_code(db, payload.code):
                raise ApiError(
                    "duplicate", "该 Agent code 已存在", status_code=409
                )
            row = await agent_repo.create(
                db,
                code=payload.code,
                tenant_id=tenant_id,
                name=payload.name,
                description=payload.description,
                system_prompt=payload.system_prompt,
                model=payload.model,
                llm_endpoint_code=payload.llm_endpoint_code,
                base_url=payload.base_url,
                api_key_cipher=encrypt(payload.api_key),
                tools=payload.tools,
                skills=payload.skills,
                response_format=payload.response_format,
                response_schema=payload.response_schema,
                enabled=1 if payload.enabled else 0,
            )
            await audit_principal_action(
                db,
                principal,
                action="agent.create",
                target_type="agent",
                target_id=row.code,
                tenant_id=tenant_id,
                detail={"llm_endpoint_code": row.llm_endpoint_code},
            )
            await db.commit()
            return json_response({"item": agent_payload(row)}, status_code=201)
        finally:
            await db.close()

    @app.put("/admin/api/agents/{code}")
    @api_route
    async def api_agents_update(code: str, request: Request):
        """更新 Agent（只改传入的字段）。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=True
        )
        try:
            body = await read_json_body(request)
            tenant_id = tenant_of(principal)
            existing = await agent_repo.get_by_code(db, code, tenant_id=tenant_id)
            if existing is None:
                raise ApiError("missing", "Agent 不存在", status_code=404)
            try:
                values = await resolve_agent_values(db, tenant_id, body, partial=True)
                payload = AgentUpdate(**values)
            except (ValidationError, ValueError) as exc:
                raise _invalid(exc) from exc
            fields = payload.model_dump(exclude_unset=True)
            if "response_schema" in fields and fields["response_schema"] is None:
                # 显式传 null = 「不限定结构」，要真的写 NULL。
                fields["response_schema"] = CLEAR
            if "enabled" in fields:
                fields["enabled"] = 1 if fields["enabled"] else 0
            row = await agent_repo.update(db, code, fields, tenant_id=tenant_id)
            if row is None:
                raise ApiError("missing", "Agent 不存在", status_code=404)
            await audit_principal_action(
                db,
                principal,
                action="agent.update",
                target_type="agent",
                target_id=code,
                tenant_id=tenant_id,
            )
            await db.commit()
            return json_response({"item": agent_payload(row)})
        finally:
            await db.close()

    @app.post("/admin/api/agents/{code}/toggle")
    @api_route
    async def api_agents_toggle(code: str, request: Request):
        """切换 Agent 的启用状态。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=True
        )
        try:
            tenant_id = tenant_of(principal)
            existing = await agent_repo.get_by_code(db, code, tenant_id=tenant_id)
            if existing is None:
                raise ApiError("missing", "Agent 不存在", status_code=404)
            body = await read_json_body(request)
            next_enabled = bool(body.get("enabled"))
            if (
                existing.enabled
                and not next_enabled
                and await workflow_repo.agents_using(db, code)
            ):
                raise ApiError(
                    "agent_in_use",
                    "该 Agent 正被工作流引用，不能停用",
                    status_code=409,
                )
            row = await agent_repo.update(
                db, code, {"enabled": 1 if next_enabled else 0}, tenant_id=tenant_id
            )
            if row is None:
                raise ApiError("missing", "Agent 不存在", status_code=404)
            await audit_principal_action(
                db,
                principal,
                action="agent.toggle",
                target_type="agent",
                target_id=code,
                tenant_id=tenant_id,
                detail={"enabled": bool(row.enabled)},
            )
            await db.commit()
            return json_response({"item": agent_payload(row)})
        finally:
            await db.close()

    @app.delete("/admin/api/agents/{code}")
    @api_route
    async def api_agents_delete(code: str, request: Request):
        """删除 Agent。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=True
        )
        try:
            tenant_id = tenant_of(principal)
            existing = await agent_repo.get_by_code(db, code, tenant_id=tenant_id)
            if existing is None:
                raise ApiError("missing", "Agent 不存在", status_code=404)
            if await workflow_repo.agents_using(db, code):
                raise ApiError(
                    "agent_in_use",
                    "该 Agent 正被工作流引用，不能删除",
                    status_code=409,
                )
            entry = await record_deletion(
                db,
                resource_type="agent",
                row=existing,
                actor_id=principal.principal_id,
                actor_username=principal.username,
            )
            if not await agent_repo.delete(db, code, tenant_id=tenant_id):
                raise ApiError("missing", "Agent 不存在", status_code=404)
            await audit_principal_action(
                db,
                principal,
                action="agent.delete",
                target_type="agent",
                target_id=code,
                tenant_id=tenant_id,
            )
            await db.commit()
            return json_response({"ok": True, "undoId": entry.id})
        finally:
            await db.close()