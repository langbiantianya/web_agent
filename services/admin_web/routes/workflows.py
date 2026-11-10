"""Workflow list and canvas routes."""

from __future__ import annotations

from typing import Any

from ludic.web import Request

from app.repo.agent_repo import agent_repo
from app.repo.workflow_repo import workflow_repo
from services.admin_web.common import (
    can_edit_resource,
    flash_from_query,
    not_found,
    tenant_id_of,
    unauthorized,
    undo_id_from_query,
    with_auth,
)
from services.admin_web.listing import parse_list_params, search_term
from services.admin_web.registry import can_access_page, visible_pages
from services.admin_web.views import workflow_editor_page, workflows_page
from services.admin_web.workflow_validation import (
    _agent_binding_errors,
    _definition_errors,
    _pydantic_message,
)

__all__ = [
    "_agent_binding_errors",
    "_definition_errors",
    "_pydantic_message",
    "register",
]


def register(app: Any) -> None:
    """Mount the workflow list and canvas routes."""

    @app.get("/admin/workflows")
    async def workflows_get(request: Request):
        """Render the Workflow orchestration list."""
        values = await with_auth(request)
        if values is None:
            return unauthorized()
        db, principal, pref, csrf = values
        try:
            if not await can_access_page(db, principal, "workflows"):
                return not_found()
            flash, _kind = flash_from_query(request)
            undo_id = undo_id_from_query(request)
            list_params = parse_list_params(
                request,
                allowed_sorts=("name", "code"),
            )
            search = search_term(list_params.q)
            total = await workflow_repo.count(db, search=search or None)
            return workflows_page(
                principal=principal,
                csrf=csrf,
                pages=await visible_pages(db, principal),
                rows=await workflow_repo.list(
                    db,
                    limit=list_params.size,
                    offset=list_params.offset,
                    search=search or None,
                    sort=list_params.sort,
                ),
                agents=await agent_repo.list(
                    db,
                    only_enabled=True,
                    limit=500,
                    tenant_id=tenant_id_of(principal),
                ),
                theme=pref.theme,
                color_mode=pref.color_mode,
                density=pref.density,
                can_write=await can_edit_resource(
                    db,
                    principal,
                    page_code="workflows",
                    component_code="workflow_editor",
                ),
                flash=flash,
                undo_id=undo_id,
                list_params=list_params,
                list_total=total,
            )
        finally:
            await db.close()

    @app.get("/admin/workflows/{code}")
    async def workflow_editor_get(code: str, request: Request):
        """Render the Workflow canvas.

        Page access controls whether the canvas can be opened; component access
        controls whether it can be edited.
        """
        values = await with_auth(request)
        if values is None:
            return unauthorized()
        db, principal, pref, csrf = values
        try:
            if not await can_access_page(db, principal, "workflows"):
                return not_found()
            row = await workflow_repo.get_by_code(db, code)
            if row is None:
                return not_found()
            agents = await agent_repo.list(
                db,
                only_enabled=True,
                limit=500,
                tenant_id=tenant_id_of(principal),
            )
            flash, _kind = flash_from_query(request)
            return workflow_editor_page(
                principal=principal,
                csrf=csrf,
                pages=await visible_pages(db, principal),
                workflow=row,
                agents=agents,
                theme=pref.theme,
                color_mode=pref.color_mode,
                density=pref.density,
                can_write=await can_edit_resource(
                    db,
                    principal,
                    page_code="workflows",
                    component_code="workflow_editor",
                ),
                flash=flash,
            )
        finally:
            await db.close()
