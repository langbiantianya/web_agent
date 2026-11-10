"""MCP 服务器管理的路由。"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from ludic.web import Request
from pydantic import ValidationError
from starlette.responses import RedirectResponse

from app.core.mcp.catalog import McpCatalogError, refresh_catalog
from app.core.mcp.transport import McpTransportError
from app.repo.mcp_repo import mcp_server_repo, mcp_tool_repo
from app.schemas.mcp import McpServerCreate, McpServerUpdate
from services.admin_web.audit import audit_principal_action
from services.admin_web.deletion import record_deletion
from services.admin_web.listing import parse_list_params, search_term
from services.admin_web.form_errors import _mcp_field_errors
from services.admin_web.form_state import (
    field_errors, invalid_form_redirect, take_invalid,
)
from services.admin_web.common import (
    as_bool, business_redirect, can_edit_resource, flash_from_query, not_found,
    operation_authorized, tenant_id_of, unauthorized, undo_id_from_query,
    with_auth,
)
from services.admin_web.mcp_operations import (
    page_bindings, validate_mcp_connection,
)
from services.admin_web.registry import (
    can_access_page, can_show_component, visible_pages,
)
from services.admin_web.views import mcp_page


def register(app: Any) -> None:
    """把 MCP 服务器管理的路由挂到 app 上。"""

    @app.get("/admin/mcp")
    async def mcp_get(request: Request):
        """渲染当前租户的 MCP 服务器列表。"""
        values = await with_auth(request)
        if values is None:
            return unauthorized()
        db, principal, pref, csrf = values
        try:
            if not await can_access_page(db, principal, "mcp"):
                return not_found()
            flash, _ = flash_from_query(request)
            undo_id = undo_id_from_query(request)
            # 深链接到编辑面板（UX-09）。
            resource = (request.query_params.get("resource") or "").strip()
            invalid_nonce = request.query_params.get("invalid")
            if resource:
                edit_values, edit_errors = take_invalid(invalid_nonce)
                create_values, create_errors = {}, {}
            else:
                create_values, create_errors = take_invalid(invalid_nonce)
                edit_values, edit_errors = {}, {}
            tenant_id = tenant_id_of(principal)
            # 列表页的搜索 / 排序 / 分页（UX-06）。
            list_params = parse_list_params(
                request,
                allowed_sorts=("name", "code"),
            )
            search = search_term(list_params.q)
            total = await mcp_server_repo.count(
                db, tenant_id=tenant_id, search=search or None
            )
            servers = await mcp_server_repo.list(
                db,
                limit=list_params.size,
                offset=list_params.offset,
                tenant_id=tenant_id,
                search=search or None,
                sort=list_params.sort,
            )
            tools_by_server, used_by_by_server = await page_bindings(
                db, servers, tenant_id
            )
            editing_row = None
            if resource:
                editing_row = next(
                    (row for row in servers if row.code == resource), None
                )
                if editing_row is None:
                    editing_row = await mcp_server_repo.get_by_code(
                        db, resource, tenant_id
                    )
            return mcp_page(
                principal=principal,
                csrf=csrf,
                pages=await visible_pages(db, principal),
                rows=servers,
                tools_by_server=tools_by_server,
                used_by_by_server=used_by_by_server,
                theme=pref.theme,
                color_mode=pref.color_mode,
                density=pref.density,
                can_write=await can_edit_resource(
                    db,
                    principal,
                    page_code="mcp",
                    component_code="mcp_editor",
                ),
                can_refresh=await can_show_component(
                    db, principal, "mcp", "mcp_refresh"
                ),
                flash=flash,
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

    @app.post("/admin/mcp")
    async def mcp_post(request: Request):
        """注册当前租户的 MCP 服务器。"""
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
                page_code="mcp",
                component_code="mcp_editor",
                form=form,
            ):
                return not_found()
            submitted = {
                "code": str(form.get("code") or ""),
                "name": str(form.get("name") or ""),
                "transport": str(form.get("transport") or "http"),
                "url": str(form.get("url") or ""),
                "command": str(form.get("command") or ""),
                "timeout_sec": str(form.get("timeout_sec") or "30"),
            }
            try:
                payload = McpServerCreate(
                    code=submitted["code"],
                    name=submitted["name"],
                    transport=submitted["transport"],
                    url=submitted["url"] or None,
                    command=submitted["command"] or None,
                    timeout_sec=int(submitted["timeout_sec"]),
                    enabled=True,
                )
                await validate_mcp_connection(
                    payload.transport,
                    command=payload.command,
                    url=payload.url,
                )
            except (
                McpTransportError,
                TypeError,
                ValueError,
                ValidationError,
            ) as exc:
                # 校验失败：保留已填内容并逐字段标错（UX-03）。
                return RedirectResponse(
                    invalid_form_redirect(
                        "/admin/mcp",
                        submitted,
                        _mcp_field_errors(exc),
                    ),
                    status_code=303,
                )
            tenant_id = tenant_id_of(principal)
            if await mcp_server_repo.get_by_code(db, payload.code, tenant_id):
                return business_redirect("/admin/mcp", error="duplicate")
            row = await mcp_server_repo.create(
                db,
                code=payload.code,
                tenant_id=tenant_id,
                name=payload.name or payload.code,
                transport=payload.transport,
                command=payload.command,
                url=payload.url,
                timeout_sec=payload.timeout_sec,
                auth_type="none",
                enabled=1,
            )
            await audit_principal_action(
                db,
                principal,
                action="mcp.create",
                target_type="mcp_server",
                target_id=row.code,
                tenant_id=tenant_id,
            )
            await db.commit()
            return business_redirect("/admin/mcp", notice="created")
        finally:
            await db.close()
