"""Agent 配置管理的路由。"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from ludic.web import Request
from pydantic import ValidationError
from starlette.responses import RedirectResponse

from app.core.crypto import encrypt
from services.admin_web.agent_forms import (
    _agent_form_values,
    _business_field_errors,
    _form_list,
)
from app.repo.agent_repo import agent_repo
from app.repo.llm_endpoint_repo import llm_endpoint_repo
from app.repo.mcp_repo import mcp_server_repo, mcp_tool_repo
from app.repo.skill_repo import skill_repo
from app.repo.workflow_repo import workflow_repo
from app.schemas.agent import AgentCreate, AgentUpdate
from services.admin_web.audit import audit_principal_action
from services.admin_web.deletion import record_deletion
from services.admin_web.listing import parse_list_params, search_term
from services.admin_web.form_state import (
    field_errors, invalid_form_redirect, take_invalid,
)
from services.admin_web.common import (
    as_bool, business_redirect, can_edit_resource, csv_list, flash_from_query,
    not_found, operation_authorized, tenant_id_of, unauthorized,
    undo_id_from_query, with_auth,
)
from services.admin_web.registry import can_access_page, visible_pages
from services.admin_web.views import agents_page


def register(app: Any) -> None:
    """把 Agent 配置管理的路由挂到 app 上。"""

    @app.get("/admin/agents")
    async def agents_get(request: Request):
        """渲染当前租户的 Agent 配置列表。"""
        values = await with_auth(request)
        if values is None:
            return unauthorized()
        db, principal, pref, csrf = values
        try:
            if not await can_access_page(db, principal, "agents"):
                return not_found()
            flash, flash_kind = flash_from_query(request)
            undo_id = undo_id_from_query(request)
            # 深链接到编辑面板（UX-09）：``?resource=<code>`` 决定打开
            # 哪个 Agent 的编辑面板。
            resource = (request.query_params.get("resource") or "").strip()
            invalid_nonce = request.query_params.get("invalid")
            if resource:
                edit_values, edit_errors = take_invalid(invalid_nonce)
                create_values, create_errors = {}, {}
            else:
                create_values, create_errors = take_invalid(invalid_nonce)
                edit_values, edit_errors = {}, {}
            # 列表页的搜索 / 排序 / 分页（UX-06）。分页在**服务端**做，
            # 页面只拿到当前页的数据。
            list_params = parse_list_params(
                request,
                allowed_sorts=("name", "code", "created"),
            )
            search = search_term(list_params.q)
            tenant_id = tenant_id_of(principal)
            total = await agent_repo.count(
                db, tenant_id=tenant_id, search=search or None
            )
            rows = await agent_repo.list(
                db,
                limit=list_params.size,
                offset=list_params.offset,
                tenant_id=tenant_id,
                search=search or None,
                sort=list_params.sort,
            )
            editing_row = None
            if resource:
                editing_row = next(
                    (row for row in rows if row.code == resource), None
                )
                if editing_row is None:
                    editing_row = await agent_repo.get_by_code(
                        db, resource, tenant_id=tenant_id
                    )
            return agents_page(
                principal=principal,
                csrf=csrf,
                pages=await visible_pages(db, principal),
                rows=rows,
                llm_endpoints=await llm_endpoint_repo.list(db, limit=500),
                skills=await skill_repo.list(
                    db, limit=1000, tenant_id=tenant_id_of(principal)
                ),
                mcp_servers=await mcp_server_repo.list(
                    db, limit=1000, tenant_id=tenant_id_of(principal)
                ),
                mcp_tools=await mcp_tool_repo.list_all(
                    db, tenant_id=tenant_id_of(principal)
                ),
                used_by_workflows=await workflow_repo.agent_binding_map(db),
                theme=pref.theme,
                color_mode=pref.color_mode,
                density=pref.density,
                can_write=await can_edit_resource(
                    db,
                    principal,
                    page_code="agents",
                    component_code="agent_editor",
                ),
                flash=flash,
                flash_kind=flash_kind,
                undo_id=undo_id,
                create_errors=create_errors,
                create_values=create_values,
                list_params=list_params,
                list_total=total,
                editing_row=editing_row,
                edit_errors=(
                    {resource: edit_errors} if resource and edit_errors else {}
                ),
                edit_values=(
                    {resource: edit_values} if resource and edit_values else {}
                ),
            )
        finally:
            await db.close()

    @app.post("/admin/agents")
    async def agents_post(request: Request):
        """通过共享 Agent 仓储创建租户配置。"""
        values = await with_auth(request)
        if values is None:
            return unauthorized()
        db, principal, _, _ = values
        try:
            form = await request.form()
            if not await operation_authorized(
                request,
                db,
                principal,
                page_code="agents",
                component_code="agent_editor",
                form=form,
            ):
                return not_found()
            submitted = {
                "code": str(form.get("code") or ""),
                "name": str(form.get("name") or ""),
                "description": str(form.get("description") or ""),
                "system_prompt": str(form.get("system_prompt") or ""),
                "model": str(form.get("model") or ""),
                "llm_endpoint_code": str(
                    form.get("llm_endpoint_code") or ""
                ),
                "enabled": as_bool(form.get("enabled"), True),
            }
            try:
                fields = await _agent_form_values(db, tenant_id_of(principal), form)
                payload = AgentCreate(
                    code=submitted["code"],
                    **fields,
                )
            except ValidationError as exc:
                # 校验失败：保留已填内容并逐字段标错（UX-03）。
                return RedirectResponse(
                    invalid_form_redirect(
                        "/admin/agents",
                        submitted,
                        field_errors(exc),
                    ),
                    status_code=303,
                )
            except ValueError as exc:
                # 业务级校验（如连接不存在 / 无默认模型）也定位到字段上。
                return RedirectResponse(
                    invalid_form_redirect(
                        "/admin/agents",
                        submitted,
                        _business_field_errors(exc),
                    ),
                    status_code=303,
                )
            if await agent_repo.get_by_code(db, payload.code):
                return business_redirect("/admin/agents", error="duplicate")
            row = await agent_repo.create(
                db,
                code=payload.code,
                tenant_id=tenant_id_of(principal),
                name=payload.name,
                description=payload.description,
                system_prompt=payload.system_prompt,
                model=payload.model,
                llm_endpoint_code=payload.llm_endpoint_code,
                base_url=payload.base_url,
                api_key_cipher=encrypt(payload.api_key),
                tools=payload.tools,
                skills=payload.skills,
                enabled=1 if payload.enabled else 0,
            )
            await audit_principal_action(
                db,
                principal,
                action="agent.create",
                target_type="agent",
                target_id=row.code,
                tenant_id=tenant_id_of(principal),
                detail={"llm_endpoint_code": row.llm_endpoint_code},
            )
            await db.commit()
            return business_redirect("/admin/agents", notice="created")
        finally:
            await db.close()
