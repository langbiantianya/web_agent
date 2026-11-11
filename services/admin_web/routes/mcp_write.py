"""MCP 服务器的写操作（更新 / 启停 / 删除 / 刷新工具）。

从 ``mcp.py`` 拆出来：单文件行数上限是 350 行。拆分只切代码位置，
路由的路径、方法与注册顺序都没变。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from ludic.web import Request
from pydantic import ValidationError
from starlette.responses import RedirectResponse

from app.repo.agent_repo import agent_repo
from app.core.mcp.catalog import McpCatalogError, refresh_catalog
from app.core.mcp.transport import McpTransportError
from app.repo.mcp_repo import mcp_server_repo, mcp_tool_repo
from app.schemas.mcp import McpServerUpdate
from services.admin_web.audit import audit_principal_action
from services.admin_web.common import (
    as_bool, business_redirect, not_found, operation_authorized, tenant_id_of,
    unauthorized, undo_id_from_query, with_auth,
)
from services.admin_web.deletion import record_deletion
from services.admin_web.form_errors import _mcp_field_errors
from services.admin_web.form_state import invalid_form_redirect
from services.admin_web.mcp_operations import validate_mcp_connection


def register(app: Any) -> None:
    """挂载写操作路由。"""

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
            submitted = {
                "name": str(form.get("name") or code),
                "timeout_sec": str(form.get("timeout_sec") or "30"),
                "command": command or "",
                "url": url or "",
            }
            try:
                payload = McpServerUpdate(
                    name=submitted["name"],
                    timeout_sec=int(submitted["timeout_sec"]),
                    command=command,
                    url=url,
                )
                await validate_mcp_connection(
                    existing.transport,
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
                        f"/admin/mcp?resource={quote(code, safe='')}",
                        submitted,
                        _mcp_field_errors(exc, existing.transport),
                    ),
                    status_code=303,
                )
            invalidates_tools = (
                payload.command != existing.command
                or payload.url != existing.url
            )
            if invalidates_tools and await mcp_server_repo.agents_using(
                db, existing, tenant_id
            ):
                return business_redirect("/admin/mcp", error="in_use")
            await mcp_server_repo.update(
                db,
                code,
                payload.model_dump(exclude_unset=True),
                tenant_id=tenant_id,
            )
            if invalidates_tools:
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
            existing = await mcp_server_repo.get_by_code(
                db, code, tenant_id
            )
            if existing is None:
                return business_redirect("/admin/mcp", error="missing")
            next_enabled = as_bool(form.get("enabled"))
            if (
                existing.enabled
                and not next_enabled
                and await mcp_server_repo.agents_using(db, existing, tenant_id)
            ):
                return business_redirect("/admin/mcp", error="in_use")
            row = await mcp_server_repo.update(
                db,
                code,
                {"enabled": 1 if next_enabled else 0},
                tenant_id=tenant_id,
            )
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
            existing = await mcp_server_repo.get_by_code(
                db, code, tenant_id
            )
            if existing is None:
                return business_redirect("/admin/mcp", error="missing")
            if await mcp_server_repo.agents_using(db, existing, tenant_id):
                return business_redirect("/admin/mcp", error="in_use")
            # 删除前留快照：误删能在撤销窗口内原样恢复（UX-01）。
            entry = await record_deletion(
                db,
                resource_type="mcp_server",
                row=existing,
                actor_id=principal.principal_id,
                actor_username=principal.username,
            )
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
            return RedirectResponse(
                f"/admin/mcp?notice=deleted&undo={entry.id}",
                status_code=303,
            )
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
            except McpCatalogError as exc:
                # 区分「连不上」与「工具仍被占用」（UX-34）。
                return business_redirect(
                    "/admin/mcp",
                    error=(
                        "mcp_tools_in_use"
                        if exc.reason == "tools_in_use"
                        else "mcp_unreachable"
                    ),
                )
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
