"""MCP 服务器资源的 JSON API —— 写侧（V4.10）。

创建、更新、启停、刷新工具目录与删除。

**两个组件权限**：编辑用 ``mcp_editor``，刷新工具目录用 ``mcp_refresh``。
分开是有意的——刷新会真的连上 MCP 服务器并改写工具快照，权限应当比「改个
名字」更严。
"""

from __future__ import annotations

from typing import Any

from ludic.web import Request
from pydantic import ValidationError

from app.core.mcp.catalog import McpCatalogError, refresh_catalog
from app.core.mcp.transport import McpTransportError
from app.repo.mcp_repo import mcp_server_repo, mcp_tool_repo
from app.schemas.mcp import McpServerCreate, McpServerUpdate
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
from services.admin_web.mcp_api import (
    EDITOR_COMPONENT,
    PAGE_CODE,
    REFRESH_COMPONENT,
    invalid_error,
    server_payload,
)
from services.admin_web.mcp_operations import validate_mcp_connection

_VALIDATION_ERRORS = (
    McpTransportError,
    TypeError,
    ValueError,
    ValidationError,
)


def register(app: Any) -> None:
    """把 MCP 的写操作 JSON API 挂到 app 上。"""

    @app.post("/admin/api/mcp")
    @api_route
    async def api_mcp_create(request: Request):
        """注册一台 MCP 服务器。"""
        db, principal, _csrf = await authorize(
            request,
            page_code=PAGE_CODE,
            component_code=EDITOR_COMPONENT,
            write=True,
        )
        try:
            body = await read_json_body(request)
            transport = str(body.get("transport") or "http")
            try:
                payload = McpServerCreate(
                    code=str(body.get("code") or "").strip(),
                    name=str(body.get("name") or ""),
                    transport=transport,
                    url=str(body.get("url") or "") or None,
                    command=str(body.get("command") or "") or None,
                    timeout_sec=int(body.get("timeoutSec") or 30),
                    enabled=True,
                )
                await validate_mcp_connection(
                    payload.transport, command=payload.command, url=payload.url
                )
            except _VALIDATION_ERRORS as exc:
                raise invalid_error(exc, transport) from exc
            tenant_id = tenant_of(principal)
            if await mcp_server_repo.get_by_code(db, payload.code, tenant_id):
                raise ApiError("duplicate", "该 MCP 服务器 code 已存在", status_code=409)
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
            return json_response({"item": server_payload(row)}, status_code=201)
        finally:
            await db.close()

    @app.put("/admin/api/mcp/{code}")
    @api_route
    async def api_mcp_update(code: str, request: Request):
        """更新 MCP 连接的名称、端点、命令或超时。"""
        db, principal, _csrf = await authorize(
            request,
            page_code=PAGE_CODE,
            component_code=EDITOR_COMPONENT,
            write=True,
        )
        try:
            body = await read_json_body(request)
            tenant_id = tenant_of(principal)
            existing = await mcp_server_repo.get_by_code(db, code, tenant_id)
            if existing is None:
                raise ApiError("missing", "MCP 服务器不存在", status_code=404)
            # 部分更新：没传的端点字段沿用库里已有的值。只改「名字」的
            # 请求不该因为「没带 URL」而被判成配置不合法。
            command = (
                (str(body.get("command") or "") or None)
                if "command" in body
                else existing.command
            )
            url = (
                (str(body.get("url") or "") or None) if "url" in body else existing.url
            )
            try:
                payload = McpServerUpdate(
                    name=str(body.get("name") or code),
                    timeout_sec=int(body.get("timeoutSec") or 30),
                    command=command,
                    url=url,
                )
                await validate_mcp_connection(
                    existing.transport, command=payload.command, url=payload.url
                )
            except _VALIDATION_ERRORS as exc:
                raise invalid_error(exc, existing.transport) from exc
            invalidates_tools = (
                payload.command != existing.command or payload.url != existing.url
            )
            # 端点没变时，不要把未传的字段也写回库。
            if invalidates_tools and await mcp_server_repo.agents_using(
                db, existing, tenant_id
            ):
                raise ApiError(
                    "in_use",
                    "该 MCP 服务器正被智能体引用，不能改端点",
                    status_code=409,
                )
            row = await mcp_server_repo.update(
                db, code, payload.model_dump(exclude_unset=True), tenant_id=tenant_id
            )
            if row is None:
                raise ApiError("missing", "MCP 服务器不存在", status_code=404)
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
            return json_response({"item": server_payload(row)})
        finally:
            await db.close()

    @app.post("/admin/api/mcp/{code}/toggle")
    @api_route
    async def api_mcp_toggle(code: str, request: Request):
        """切换 MCP 服务器的启用状态。"""
        db, principal, _csrf = await authorize(
            request,
            page_code=PAGE_CODE,
            component_code=EDITOR_COMPONENT,
            write=True,
        )
        try:
            body = await read_json_body(request)
            tenant_id = tenant_of(principal)
            existing = await mcp_server_repo.get_by_code(db, code, tenant_id)
            if existing is None:
                raise ApiError("missing", "MCP 服务器不存在", status_code=404)
            enabled = bool(body.get("enabled"))
            if (
                existing.enabled
                and not enabled
                and await mcp_server_repo.agents_using(db, existing, tenant_id)
            ):
                raise ApiError(
                    "in_use",
                    "该 MCP 服务器正被智能体引用，不能停用",
                    status_code=409,
                )
            row = await mcp_server_repo.update(
                db, code, {"enabled": 1 if enabled else 0}, tenant_id=tenant_id
            )
            if row is None:
                raise ApiError("missing", "MCP 服务器不存在", status_code=404)
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
            return json_response({"item": server_payload(row)})
        finally:
            await db.close()

    @app.post("/admin/api/mcp/{code}/refresh")
    @api_route
    async def api_mcp_refresh(code: str, request: Request):
        """刷新 MCP 工具目录快照。

        刷新会真的连上 MCP 服务器，可能长时间不返回；因此它用的是**另一个
        组件权限**（``mcp_refresh``），比「改个名字」更严。
        """
        db, principal, _csrf = await authorize(
            request,
            page_code=PAGE_CODE,
            component_code=REFRESH_COMPONENT,
            write=True,
        )
        try:
            tenant_id = tenant_of(principal)
            server = await mcp_server_repo.get_by_code(db, code, tenant_id)
            if server is None:
                raise ApiError("missing", "MCP 服务器不存在", status_code=404)
            try:
                result = await refresh_catalog(db, server=server)
            except McpCatalogError as exc:
                raise ApiError(
                    "mcp_tools_in_use"
                    if exc.reason == "tools_in_use"
                    else "mcp_unreachable",
                    "工具目录刷新失败",
                    status_code=409,
                ) from exc
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
            return json_response({"ok": True, "imported": list(result.imported)})
        finally:
            await db.close()

    @app.delete("/admin/api/mcp/{code}")
    @api_route
    async def api_mcp_delete(code: str, request: Request):
        """删除 MCP 服务器及其工具快照。"""
        db, principal, _csrf = await authorize(
            request,
            page_code=PAGE_CODE,
            component_code=EDITOR_COMPONENT,
            write=True,
        )
        try:
            tenant_id = tenant_of(principal)
            existing = await mcp_server_repo.get_by_code(db, code, tenant_id)
            if existing is None:
                raise ApiError("missing", "MCP 服务器不存在", status_code=404)
            if await mcp_server_repo.agents_using(db, existing, tenant_id):
                raise ApiError(
                    "in_use",
                    "该 MCP 服务器正被智能体引用，不能删除",
                    status_code=409,
                )
            entry = await record_deletion(
                db,
                resource_type="mcp_server",
                row=existing,
                actor_id=principal.principal_id,
                actor_username=principal.username,
            )
            if not await mcp_server_repo.delete(db, code, tenant_id):
                raise ApiError("missing", "MCP 服务器不存在", status_code=404)
            await audit_principal_action(
                db,
                principal,
                action="mcp.delete",
                target_type="mcp_server",
                target_id=code,
                tenant_id=tenant_id,
            )
            await db.commit()
            return json_response({"ok": True, "undoId": entry.id})
        finally:
            await db.close()