"""LLM 接口配置管理的路由。"""

from __future__ import annotations

from typing import Any

from ludic.web import Request
from pydantic import ValidationError

from app.core.crypto import encrypt
from app.repo.llm_endpoint_repo import llm_endpoint_repo
from app.schemas.llm_endpoint import LlmEndpointCreate, LlmEndpointUpdate
from services.admin_web.audit import audit_principal_action
from services.admin_web.common import (
    as_bool, business_redirect, can_edit_resource, flash_from_query, not_found,
    operation_authorized, unauthorized, with_auth,
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
            return llm_page(
                principal=principal,
                csrf=csrf,
                pages=await visible_pages(db, principal),
                rows=await llm_endpoint_repo.list(db, limit=500),
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
            try:
                payload = LlmEndpointCreate(
                    code=str(form.get("code") or ""),
                    name=str(form.get("name") or ""),
                    base_url=str(form.get("base_url") or ""),
                    api_key=str(form.get("api_key") or ""),
                    priority=int(str(form.get("priority") or "100")),
                )
            except (TypeError, ValueError, ValidationError):
                return business_redirect("/admin/llm", error="invalid")
            if await llm_endpoint_repo.get_by_code(db, payload.code):
                return business_redirect("/admin/llm", error="duplicate")
            row = await llm_endpoint_repo.create(
                db,
                code=payload.code,
                name=payload.name or payload.code,
                base_url=payload.base_url,
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

    @app.post("/admin/llm/{code}/update")
    async def llm_update(code: str, request: Request):
        """更新 LLM 接口；空 API Key 表示保留旧密钥。"""
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
            if await llm_endpoint_repo.get_by_code(db, code) is None:
                return business_redirect("/admin/llm", error="missing")
            api_key = str(form.get("api_key") or "")
            try:
                values = {
                    "name": str(form.get("name") or code),
                    "base_url": str(form.get("base_url") or ""),
                    "priority": int(str(form.get("priority") or "100")),
                }
                if api_key:
                    values["api_key"] = api_key
                payload = LlmEndpointUpdate(
                    **values,
                )
            except (TypeError, ValueError, ValidationError):
                return business_redirect("/admin/llm", error="invalid")
            fields = payload.model_dump(exclude_unset=True)
            if "api_key" in fields:
                fields["api_key_cipher"] = encrypt(fields.pop("api_key"))
            await llm_endpoint_repo.update(db, code, fields)
            await audit_principal_action(
                db,
                principal,
                action="llm.update",
                target_type="llm_endpoint",
                target_id=code,
                tenant_id=principal.tenant_id,
            )
            await db.commit()
            return business_redirect("/admin/llm", notice="updated")
        finally:
            await db.close()

    @app.post("/admin/llm/{code}/toggle")
    async def llm_toggle(code: str, request: Request):
        """切换 LLM 接口启用状态。"""
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
            row = await llm_endpoint_repo.update(
                db,
                code,
                {"enabled": 1 if as_bool(form.get("enabled")) else 0},
            )
            if row is None:
                return business_redirect("/admin/llm", error="missing")
            await audit_principal_action(
                db,
                principal,
                action="llm.toggle",
                target_type="llm_endpoint",
                target_id=code,
                tenant_id=principal.tenant_id,
                detail={"enabled": bool(row.enabled)},
            )
            await db.commit()
            return business_redirect("/admin/llm", notice="toggled")
        finally:
            await db.close()

    @app.post("/admin/llm/{code}/delete")
    async def llm_delete(code: str, request: Request):
        """删除 LLM 接口配置。"""
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
            if not await llm_endpoint_repo.delete(db, code):
                return business_redirect("/admin/llm", error="missing")
            await audit_principal_action(
                db,
                principal,
                action="llm.delete",
                target_type="llm_endpoint",
                target_id=code,
                tenant_id=principal.tenant_id,
            )
            await db.commit()
            return business_redirect("/admin/llm", notice="deleted")
        finally:
            await db.close()
