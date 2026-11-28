"""工具审计查询的 JSON API（V4.10）。

只读：把 ``tool_audit_repo`` 的查询结果投影成表格形状。与页面路由
``routes/audit.py`` 用同一个仓储方法与同一套筛选语义。

**筛选值要归一**：``ok=maybe`` 这种脏值一律当成「不限」，而不是塞进
查询条件——否则管理员会以为筛了某个状态，实际看到的是全量。
"""

from __future__ import annotations

from typing import Any

from ludic.web import Request

from app.repo.tool_audit_repo import tool_audit_repo
from services.admin_web.api_support import (
    api_route,
    authorize,
    json_response,
)
from services.admin_web.common import tenant_id_of
from services.admin_web.listing import parse_list_params

PAGE_CODE = "audit"
COMPONENT_CODE = "audit_table"

SOURCES = ("builtin", "skill", "mcp")


def _normalize_source(raw: str | None) -> str | None:
    """把来源过滤值归一到白名单内；不在白名单里就当作不限。"""
    text = (raw or "").strip()
    return text if text in SOURCES else None


def _normalize_ok(raw: str | None) -> bool | None:
    """把 ``ok`` 过滤值归一成三态。"""
    text = (raw or "").strip().lower()
    if text == "true":
        return True
    if text == "false":
        return False
    return None


def _row_payload(row: Any) -> dict[str, Any]:
    """把一条审计记录投影成前端形状。"""
    return {
        "id": row.id,
        "runId": row.run_id,
        "nodeId": row.node_id,
        "toolName": row.tool_name,
        "source": row.source,
        "server": row.server,
        "ok": bool(row.ok),
        "failureType": row.failure_type,
        "durationMs": row.duration_ms,
        "createdAt": str(row.created_at) if row.created_at else None,
    }


def register(app: Any) -> None:
    """把工具审计的 JSON API 挂到 app 上。"""

    @app.get("/admin/api/audit")
    @api_route
    async def api_audit_list(request: Request):
        """按当前租户与筛选条件查询工具审计记录。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=False
        )
        try:
            params = parse_list_params(request)
            run_id: int | None = None
            raw_run_id = (request.query_params.get("runId") or "").strip()
            if raw_run_id:
                try:
                    run_id = int(raw_run_id)
                except ValueError:
                    run_id = None
            tool_name = (request.query_params.get("toolName") or "").strip()
            source = _normalize_source(request.query_params.get("source"))
            ok = _normalize_ok(request.query_params.get("ok"))
            rows, total = await tool_audit_repo.list(
                db,
                tenant_id=tenant_id_of(principal),
                run_id=run_id,
                tool_name=tool_name or None,
                source=source,
                ok=ok,
                limit=params.size,
                offset=params.offset,
            )
            return json_response(
                {
                    "items": [_row_payload(row) for row in rows],
                    "total": total,
                    "page": {
                        "q": params.q,
                        "sort": params.sort,
                        "page": params.page,
                        "size": params.size,
                    },
                    "filters": {
                        "runId": raw_run_id,
                        "toolName": tool_name,
                        "source": source or "",
                        "ok": "" if ok is None else ("true" if ok else "false"),
                    },
                    "sources": list(SOURCES),
                }
            )
        finally:
            await db.close()
