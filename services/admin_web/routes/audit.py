"""工具审计查询页的路由。"""

from __future__ import annotations

from typing import Any

from ludic.web import Request

from app.repo.tool_audit_repo import tool_audit_repo
from services.admin_web.common import (
    flash_from_query, not_found, tenant_id_of, unauthorized, with_auth,
)
from services.admin_web.registry import can_access_page, visible_pages
from services.admin_web.views import audit_page


def register(app: Any) -> None:
    """把工具审计查询页的路由挂到 app 上。"""

    @app.get("/admin/audit")
    async def audit_get(request: Request):
        """按当前租户和固定筛选条件查询工具审计。"""
        values = await with_auth(request)
        if values is None:
            return unauthorized()
        db, principal, pref, csrf = values
        try:
            if not await can_access_page(db, principal, "audit"):
                return not_found()
            raw_run_id = request.query_params.get("run_id", "").strip()
            raw_ok = request.query_params.get("ok", "").strip().lower()
            source = request.query_params.get("source", "").strip() or None
            if source not in (None, "builtin", "skill", "mcp"):
                source = None
            ok = True if raw_ok == "true" else False if raw_ok == "false" else None
            try:
                run_id = int(raw_run_id) if raw_run_id else None
            except ValueError:
                run_id = None
            tool_name = request.query_params.get("tool_name", "").strip()
            rows, total = await tool_audit_repo.list(
                db,
                tenant_id=tenant_id_of(principal),
                run_id=run_id,
                tool_name=tool_name or None,
                source=source,
                ok=ok,
                limit=100,
            )
            flash, flash_kind = flash_from_query(request)
            return audit_page(
                principal=principal,
                csrf=csrf,
                pages=await visible_pages(db, principal),
                rows=rows,
                total=total,
                theme=pref.theme,
                color_mode=pref.color_mode,
                density=pref.density,
                flash=flash,
                flash_kind=flash_kind,
                # 回显当前筛选条件：URL 里的条件必须与页面上的控件一致
                # （UX-36）。
                run_id=raw_run_id,
                tool_name=tool_name,
                source=source or "",
                # 用**归一后**的值：``ok=maybe`` 这种脏值必须回显成「任意」，
                # 而不是把脏值塞回下拉框。
                ok="" if ok is None else ("true" if ok else "false"),
            )
        finally:
            await db.close()
