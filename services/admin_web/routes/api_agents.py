"""Agent 资源的 JSON API —— 读侧（V4.10）。

给 SvelteKit 前端用。与页面路由**共用**同一套仓储与判定规则，差别只有
三点：请求体是 JSON、CSRF 令牌从 ``X-CSRF-Token`` 头取、失败返回 JSON。

写侧在 ``api_agents_write.py``；两边共用 ``agent_api`` 里的形状与校验。
"""

from __future__ import annotations

from typing import Any

from ludic.web import Request

from app.repo.agent_repo import agent_repo
from app.repo.llm_endpoint_repo import llm_endpoint_repo
from app.repo.mcp_repo import mcp_server_repo, mcp_tool_repo
from app.repo.skill_repo import skill_repo
from services.admin_web.routes import api_agents_write
from services.admin_web.agent_api import agent_payload
from services.admin_web.api_support import (
    ApiError,
    api_route,
    authorize,
    json_response,
    tenant_of,
)
from services.admin_web.listing import parse_list_params, search_term

PAGE_CODE = "agents"
COMPONENT_CODE = "agent_editor"


def register(app: Any) -> None:
    """把 Agent 的 JSON API 挂到 app 上。"""
    api_agents_write.register(app)

    @app.get("/admin/api/agents")
    @api_route
    async def api_agents_list(request: Request):
        """列出当前租户的 Agent，并附上表单需要的候选项。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=False
        )
        try:
            tenant_id = tenant_of(principal)
            params = parse_list_params(
                request, allowed_sorts=("name", "code", "created")
            )
            search = search_term(params.q)
            rows = await agent_repo.list(
                db,
                limit=params.size,
                offset=params.offset,
                tenant_id=tenant_id,
                search=search or None,
                sort=params.sort,
            )
            total = await agent_repo.count(
                db, tenant_id=tenant_id, search=search or None
            )
            return json_response(
                {
                    "items": [agent_payload(row) for row in rows],
                    "total": total,
                    "page": {
                        "q": params.q,
                        "sort": params.sort,
                        "page": params.page,
                        "size": params.size,
                    },
                    "options": {
                        "llmEndpoints": [
                            {
                                "code": row.code,
                                "name": row.name,
                                "model": row.model,
                                "enabled": bool(row.enabled),
                            }
                            for row in await llm_endpoint_repo.list(db, limit=500)
                        ],
                        "skills": [
                            {"code": row.code, "name": row.name}
                            for row in await skill_repo.list(
                                db, limit=1000, tenant_id=tenant_id
                            )
                        ],
                        "mcpServers": [
                            {"code": row.code, "name": row.name}
                            for row in await mcp_server_repo.list(
                                db, limit=1000, tenant_id=tenant_id
                            )
                        ],
                        "mcpTools": list(
                            await mcp_tool_repo.list_all(db, tenant_id=tenant_id)
                        ),
                    },
                }
            )
        finally:
            await db.close()

    @app.get("/admin/api/agents/{code}")
    @api_route
    async def api_agents_get(code: str, request: Request):
        """取单个 Agent。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=False
        )
        try:
            row = await agent_repo.get_by_code(
                db, code, tenant_id=tenant_of(principal)
            )
            if row is None:
                raise ApiError("missing", "Agent 不存在", status_code=404)
            return json_response({"item": agent_payload(row)})
        finally:
            await db.close()