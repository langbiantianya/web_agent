"""MCP 服务器管理的路由。"""

from __future__ import annotations

from typing import Any

from ludic.web import Request
from pydantic import ValidationError

from app.core.mcp.catalog import McpCatalogError, refresh_catalog
from app.core.mcp.transport import (
    McpTransportError, check_command_allowed, check_url_allowed,
)
from app.repo.mcp_repo import mcp_server_repo, mcp_tool_repo
from app.schemas.mcp import McpServerCreate, McpServerUpdate
from services.admin_web.audit import audit_principal_action
from services.admin_web.common import (
    as_bool, business_redirect, can_edit_resource, flash_from_query, not_found,
    operation_authorized, tenant_id_of, unauthorized, with_auth,
)
from services.admin_web.registry import (
    can_access_page, can_show_component, visible_pages,
)
from services.admin_web.views import mcp_page


async def _validate_mcp_connection(
    transport: str,
    *,
    command: str | None,
    url: str | None,
) -> None:
    """在保存前执行 MCP 命令和 URL 的边界校验。"""
    if transport == "stdio":
        if not command:
            raise ValueError("command_required")
        if url:
            raise ValueError("url_not_allowed_for_stdio")
        check_command_allowed(command)
    elif transport == "http":
        if not url:
            raise ValueError("url_required")
        if command:
            raise ValueError("command_not_allowed_for_http")
        await check_url_allowed(url, require_dns=False)
    else:
        raise ValueError("invalid_transport")


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
            return mcp_page(
                principal=principal,
                csrf=csrf,
                pages=await visible_pages(db, principal),
                rows=await mcp_server_repo.list(
                    db,
                    limit=500,
                    tenant_id=tenant_id_of(principal),
                ),
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
            try:
                payload = McpServerCreate(
                    code=str(form.get("code") or ""),
                    name=str(form.get("name") or ""),
                    transport=str(form.get("transport") or "http"),
                    url=str(form.get("url") or "") or None,
                    command=str(form.get("command") or "") or None,
                    timeout_sec=int(str(form.get("timeout_sec") or "30")),
                    enabled=True,
                )
                await _validate_mcp_connection(
                    payload.transport,
                    command=payload.command,
                    url=payload.url,
                )
            except (
                McpTransportError,
                TypeError,
                ValueError,
                ValidationError,
            ):
                return business_redirect("/admin/mcp", error="invalid")
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

    @app.post("/admin/mcp/{code}/update")
    async def mcp_update(code: str, request: Request):
        """更新 MCP 连接名称、端点、命令或超时。"""
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
            tenant_id = tenant_id_of(principal)
            existing = await mcp_server_repo.get_by_code(db, code, tenant_id)
            if existing is None:
                return business_redirect("/admin/mcp", error="missing")
            command = str(form.get("command") or "") or None
            url = str(form.get("url") or "") or None
            try:
                payload = McpServerUpdate(
                    name=str(form.get("name") or code),
                    timeout_sec=int(str(form.get("timeout_sec") or "30")),
                    command=command,
                    url=url,
                )
                await _validate_mcp_connection(
                    existing.transport,
                    command=payload.command,
                    url=payload.url,
                )
            except (
                McpTransportError,
                TypeError,
                ValueError,
                ValidationError,
            ):
                return business_redirect("/admin/mcp", error="invalid")
            await mcp_server_repo.update(
                db,
                code,
                payload.model_dump(exclude_unset=True),
                tenant_id=tenant_id,
            )
            await mcp_tool_repo.replace_for_server(db, existing.id, [])
            await audit_principal_action(
                db,
                principal,
                action="mcp.update",
                target_type="mcp_server",
                target_id=code,
                tenant_id=tenant_id,
            )
            await db.commit()
            return business_redirect("/admin/mcp", notice="updated")
        finally:
            await db.close()

    @app.post("/admin/mcp/{code}/toggle")
    async def mcp_toggle(code: str, request: Request):
        """切换 MCP 服务器启用状态。"""
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
            tenant_id = tenant_id_of(principal)
            row = await mcp_server_repo.update(
                db,
                code,
                {"enabled": 1 if as_bool(form.get("enabled")) else 0},
                tenant_id=tenant_id,
            )
            if row is None:
                return business_redirect("/admin/mcp", error="missing")
            await audit_principal_action(
                db,
                principal,
                action="mcp.toggle",
                target_type="mcp_server",
                target_id=code,
                tenant_id=tenant_id,
                detail={"enabled": bool(row.enabled)},
            )
            await db.commit()
            return business_redirect("/admin/mcp", notice="toggled")
        finally:
            await db.close()

    @app.post("/admin/mcp/{code}/delete")
    async def mcp_delete(code: str, request: Request):
        """删除 MCP 服务器及其工具快照。"""
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
            tenant_id = tenant_id_of(principal)
            if not await mcp_server_repo.delete(db, code, tenant_id):
                return business_redirect("/admin/mcp", error="missing")
            await audit_principal_action(
                db,
                principal,
                action="mcp.delete",
                target_type="mcp_server",
                target_id=code,
                tenant_id=tenant_id,
            )
            await db.commit()
            return business_redirect("/admin/mcp", notice="deleted")
        finally:
            await db.close()

    @app.post("/admin/mcp/{code}/refresh")
    async def mcp_refresh(code: str, request: Request):
        """刷新 MCP 工具目录快照。"""
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
                component_code="mcp_refresh",
                form=form,
            ):
                return not_found()
            tenant_id = tenant_id_of(principal)
            server = await mcp_server_repo.get_by_code(db, code, tenant_id)
            if server is None:
                return business_redirect("/admin/mcp", error="missing")
            try:
                result = await refresh_catalog(db, server=server)
            except McpCatalogError:
                return business_redirect("/admin/mcp", error="failed")
            await audit_principal_action(
                db,
                principal,
                action="mcp.refresh",
                target_type="mcp_server",
                target_id=code,
                tenant_id=tenant_id,
                detail={"imported": len(result.imported)},
            )
            await db.commit()
            return business_redirect("/admin/mcp", notice="refreshed")
        finally:
            await db.close()
