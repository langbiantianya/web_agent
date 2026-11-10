"""LLM 接口配置管理的路由。"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from ludic.web import Request
from pydantic import ValidationError
from starlette.responses import RedirectResponse

from app.core.crypto import decrypt, encrypt
from app.repo.agent_repo import agent_repo
from app.repo.llm_endpoint_repo import llm_endpoint_repo
from app.schemas.llm_endpoint import LlmEndpointCreate, LlmEndpointUpdate
from services.admin_web.audit import audit_principal_action
from services.admin_web.deletion import record_deletion
from services.admin_web.listing import parse_list_params, search_term
from services.admin_web.form_errors import _llm_field_errors
from services.admin_web.form_state import (
    field_errors, invalid_form_redirect, take_invalid,
)
from services.admin_web.common import (
    as_bool, business_redirect, can_edit_resource, flash_from_query,
    json_response_no_store, not_found, operation_authorized, unauthorized,
    undo_id_from_query, with_auth,
)
from services.admin_web.registry import can_access_page, visible_pages
from services.admin_web.views import llm_page

def register(app: Any) -> None:
    """把 LLM 接口配置管理的路由挂到 app 上。"""

    @app.get("/admin/llm")
    async def llm_get(request: Request):
        """渲染 LLM 接口优先级配置页。"""
        values = await with_auth(request)
        if values is None:
            return unauthorized()
        db, principal, pref, csrf = values
        try:
            if not await can_access_page(db, principal, "llm"):
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
            # 列表页的搜索 / 排序 / 分页（UX-06）。
            list_params = parse_list_params(
                request,
                allowed_sorts=("name", "code", "priority"),
            )
            search = search_term(list_params.q)
            total = await llm_endpoint_repo.count(db, search=search or None)
            rows = await llm_endpoint_repo.list(
                db,
                limit=list_params.size,
                offset=list_params.offset,
                search=search or None,
                sort=list_params.sort,
            )
            editing_row = None
            if resource:
                editing_row = next(
                    (row for row in rows if row.code == resource), None
                )
                if editing_row is None:
                    editing_row = await llm_endpoint_repo.get_by_code(
                        db, resource
                    )
            return llm_page(
                principal=principal,
                csrf=csrf,
                pages=await visible_pages(db, principal),
                rows=rows,
                used_by_by_endpoint=await agent_repo.llm_endpoint_binding_map(
                    db
                ),
                theme=pref.theme,
                color_mode=pref.color_mode,
                density=pref.density,
                can_write=await can_edit_resource(
                    db,
                    principal,
                    page_code="llm",
                    component_code="llm_editor",
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

    @app.post("/admin/llm")
    async def llm_post(request: Request):
        """新增一个 OpenAI 兼容接口配置。"""
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
                page_code="llm",
                component_code="llm_editor",
                form=form,
            ):
                return not_found()
            submitted = {
                "code": str(form.get("code") or ""),
                "name": str(form.get("name") or ""),
                "base_url": str(form.get("base_url") or ""),
                "model": str(form.get("model") or ""),
                "priority": str(form.get("priority") or "100"),
            }
            try:
                payload = LlmEndpointCreate(
                    code=submitted["code"],
                    name=submitted["name"],
                    base_url=submitted["base_url"],
                    model=submitted["model"],
                    api_key=str(form.get("api_key") or ""),
                    priority=int(submitted["priority"]),
                )
            except (TypeError, ValueError, ValidationError) as exc:
                # 校验失败：保留已填内容并逐字段标错（UX-03）。
                return RedirectResponse(
                    invalid_form_redirect(
                        "/admin/llm",
                        submitted,
                        _llm_field_errors(exc),
                    ),
                    status_code=303,
                )
            if await llm_endpoint_repo.get_by_code(db, payload.code):
                return business_redirect("/admin/llm", error="duplicate")
            row = await llm_endpoint_repo.create(
                db,
                code=payload.code,
                name=payload.name or payload.code,
                base_url=payload.base_url,
                model=payload.model,
                api_key_cipher=encrypt(payload.api_key),
                priority=payload.priority,
                enabled=1,
            )
            await audit_principal_action(
                db,
                principal,
                action="llm.create",
                target_type="llm_endpoint",
                target_id=row.code,
                tenant_id=principal.tenant_id,
            )
            await db.commit()
            return business_redirect("/admin/llm", notice="created")
        finally:
            await db.close()
