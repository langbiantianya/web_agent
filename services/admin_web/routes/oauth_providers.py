"""OAuth Provider 配置管理的路由。"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import sqlalchemy as sa
from ludic.web import Request
from pydantic import ValidationError
from starlette.responses import RedirectResponse

from app.repo.oauth_provider_repo import oauth_provider_repo
from app.schemas.oauth_provider import OAuthProviderCreate, OAuthProviderUpdate
from services.admin_web.audit import audit_principal_action
from services.admin_web.form_errors import _oauth_field_errors
from services.admin_web.oauth_forms import _provider_form_values, _submitted_values
from services.admin_web.form_state import (
    field_errors, invalid_form_redirect, take_invalid,
)
from services.admin_web.common import (
    as_bool, business_redirect, can_edit_resource, csv_list, flash_from_query,
    not_found, operation_authorized, tenant_id_of, unauthorized, with_auth,
)
from services.admin_web.registry import can_access_page, visible_pages
from services.admin_web.views import oauth_providers_page


def register(app: Any) -> None:
    """把 OAuth Provider 配置管理的路由挂到 app 上。"""

    @app.get("/admin/oauth-providers")
    async def oauth_providers_get(request: Request):
        """渲染客户端和管理端共用的 OAuth Provider 配置页。"""
        values = await with_auth(request)
        if values is None:
            return unauthorized()
        db, principal, pref, csrf = values
        try:
            if not await can_access_page(db, principal, "oauth_providers"):
                return not_found()
            flash, flash_kind = flash_from_query(request)
            # 深链接到编辑面板（UX-09）。
            resource = (request.query_params.get("resource") or "").strip()
            invalid_nonce = request.query_params.get("invalid")
            if resource:
                edit_values, edit_errors = take_invalid(invalid_nonce)
                create_values, create_errors = {}, {}
            else:
                create_values, create_errors = take_invalid(invalid_nonce)
                edit_values, edit_errors = {}, {}
            providers = await oauth_provider_repo.list(
                db, tenant_id_of(principal)
            )
            return oauth_providers_page(
                principal=principal,
                csrf=csrf,
                pages=await visible_pages(db, principal),
                providers=providers,
                theme=pref.theme,
                color_mode=pref.color_mode,
                density=pref.density,
                can_manage=await can_edit_resource(
                    db,
                    principal,
                    page_code="oauth_providers",
                    component_code="provider_editor",
                ),
                flash=flash,
                flash_kind=flash_kind,
                create_errors=create_errors,
                create_values=create_values,
                edit_errors=(
                    {resource: edit_errors} if resource and edit_errors else {}
                ),
                edit_values=(
                    {resource: edit_values} if resource and edit_values else {}
                ),
                editing_code=resource or None,
            )
        finally:
            await db.close()

    @app.post("/admin/oauth-providers")
    async def oauth_providers_post(request: Request):
        """动态新增客户端或管理端 OIDC Provider。"""
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
                page_code="oauth_providers",
                component_code="provider_editor",
                form=form,
            ):
                return not_found()
            try:
                fields = _provider_form_values(form)
                payload = OAuthProviderCreate(
                    code=str(form.get("code") or ""),
                    client_secret=str(form.get("client_secret") or "") or None,
                    **fields,
                )
            except (TypeError, ValueError, ValidationError) as exc:
                # 校验失败：保留已填内容并逐字段标错（UX-03）。
                submitted = _submitted_values(form)
                return RedirectResponse(
                    invalid_form_redirect(
                        "/admin/oauth-providers",
                        submitted,
                        _oauth_field_errors(exc),
                    ),
                    status_code=303,
                )
            tenant_id = tenant_id_of(principal)
            if await oauth_provider_repo.get(db, payload.code, tenant_id):
                return business_redirect(
                    "/admin/oauth-providers", error="duplicate"
                )
            stored = payload.model_dump(
                exclude={"client_secret", "scopes", "code"}
            )
            stored["scopes"] = " ".join(payload.scopes)
            for key in (
                "auto_create",
                "role_claim_required",
                "client_login_enabled",
                "admin_login_enabled",
                "enabled",
            ):
                stored[key] = int(stored[key])
            row = await oauth_provider_repo.create(
                db,
                tenant_id=tenant_id,
                code=payload.code,
                client_secret=payload.client_secret,
                **stored,
            )
            await audit_principal_action(
                db,
                principal,
                action="oauth_provider.create",
                target_type="oauth_provider",
                target_id=row.code,
                tenant_id=tenant_id,
                detail={
                    "client_login": bool(row.client_login_enabled),
                    "admin_login": bool(row.admin_login_enabled),
                },
            )
            await db.commit()
            return business_redirect(
                "/admin/oauth-providers", notice="created"
            )
        finally:
            await db.close()
