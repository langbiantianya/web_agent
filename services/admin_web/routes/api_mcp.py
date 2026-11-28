"""MCP 服务器资源的 JSON API —— 读侧（V4.10）。

给 SvelteKit 前端用。写侧在 ``api_mcp_write.py``；两边共用 ``mcp_api`` 里的
形状与错误翻译。

**凭据字段是「只写」的**：``env`` 与 ``headers`` 里装的就是凭据，读取形状
只回显键名（``envKeys`` / ``headerKeys``），不回显值。
"""

from __future__ import annotations

from typing import Any

from ludic.web import Request

from app.repo.mcp_repo import mcp_server_repo, mcp_tool_repo
from services.admin_web.api_support import (
    ApiError,
    api_route,
    authorize,
    json_response,
    tenant_of,
)
from services.admin_web.listing import parse_list_params, search_term
from services.admin_web.mcp_api import (
    EDITOR_COMPONENT,
    PAGE_CODE,
    server_payload,
)
from services.admin_web.routes import api_mcp_write


def register(app: Any) -> None:
    """把 MCP 的 JSON API 挂到 app 上。"""
    api_mcp_write.register(app)

    @app.get("/admin/api/mcp")
    @api_route
    async def api_mcp_list(request: Request):
        """列出当前租户的 MCP 服务器。"""
        db, principal, _csrf = await authorize(
            request,
            page_code=PAGE_CODE,
            component_code=EDITOR_COMPONENT,
            write=False,
        )
        try:
            tenant_id = tenant_of(principal)
            params = parse_list_params(
                request, allowed_sorts=("name", "code", "created")
            )
            search = search_term(params.q)
            rows = await mcp_server_repo.list(
                db,
                limit=params.size,
                offset=params.offset,
                tenant_id=tenant_id,
                search=search or None,
                sort=params.sort,
            )
            total = await mcp_server_repo.count(
                db, tenant_id=tenant_id, search=search or None
            )
            items = []
            for row in rows:
                tools = await mcp_tool_repo.list_by_server(db, row.id)
                used_by = await mcp_server_repo.agents_using(db, row, tenant_id)
                items.append(server_payload(row, tool_count=len(tools), used_by=used_by))
            return json_response(
                {
                    "items": items,
                    "total": total,
                    "page": {
                        "q": params.q,
                        "sort": params.sort,
                        "page": params.page,
                        "size": params.size,
                    },
                }
            )
        finally:
            await db.close()

    @app.get("/admin/api/mcp/{code}")
    @api_route
    async def api_mcp_get(code: str, request: Request):
        """取单个 MCP 服务器。"""
        db, principal, _csrf = await authorize(
            request,
            page_code=PAGE_CODE,
            component_code=EDITOR_COMPONENT,
            write=False,
        )
        try:
            tenant_id = tenant_of(principal)
            row = await mcp_server_repo.get_by_code(db, code, tenant_id)
            if row is None:
                raise ApiError("missing", "MCP 服务器不存在", status_code=404)
            tools = await mcp_tool_repo.list_by_server(db, row.id)
            used_by = await mcp_server_repo.agents_using(db, row, tenant_id)
            return json_response(
                {"item": server_payload(row, tool_count=len(tools), used_by=used_by)}
            )
        finally:
            await db.close()