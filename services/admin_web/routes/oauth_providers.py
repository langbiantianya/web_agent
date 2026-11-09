"""OAuth Provider 配置管理的路由。"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from ludic.web import Request
from pydantic import ValidationError

from app.repo.oauth_provider_repo import oauth_provider_repo
from app.schemas.oauth_provider import OAuthProviderCreate, OAuthProviderUpdate
from services.admin_web.audit import audit_principal_action
from services.admin_web.common import (
    as_bool, business_redirect, can_edit_resource, csv_list, flash_from_query,
    not_found, operation_authorized, tenant_id_of, unauthorized, with_auth,
)
from services.admin_web.registry import can_access_page, visible_pages
from services.admin_web.views import oauth_providers_page


def _provider_form_values(form) -> dict:
    """解析管理端 OAuth Provider 表单中的非密文字段。"""
    return {
        "display_name": str(form.get("display_name") or ""),
        "issuer": str(form.get("issuer") or ""),
        "discovery_url": str(form.get("discovery_url") or "") or None,
        "authorization_endpoint": (
            str(form.get("authorization_endpoint") or "") or None
        ),
        "token_endpoint": str(form.get("token_endpoint") or "") or None,
        "jwks_uri": str(form.get("jwks_uri") or "") or None,
        "client_id": str(form.get("client_id") or ""),
        "token_auth_method": str(
            form.get("token_auth_method") or "client_secret_basic"
        ),
        "scopes": csv_list(form.get("scopes")),
        "username_claim": str(
            form.get("username_claim") or "preferred_username"
        ),
        "role_claim": str(form.get("role_claim") or "groups"),
        "role_claim_required": as_bool(
            form.get("role_claim_required"), True
        ),
        "role_sync_max_age_seconds": int(
            str(form.get("role_sync_max_age_seconds") or "1800")
        ),
        "auto_create": as_bool(form.get("auto_create"), True),
        "client_login_enabled": as_bool(
            form.get("client_login_enabled"), True
        ),
        "admin_login_enabled": as_bool(
            form.get("admin_login_enabled"), True
        ),
        "enabled": as_bool(form.get("enabled"), True),
    }


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
            flash, _ = flash_from_query(request)
            return oauth_providers_page(
                principal=principal,
                csrf=csrf,
                pages=await visible_pages(db, principal),
                providers=await oauth_provider_repo.list(
                    db, tenant_id_of(principal)
                ),
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
            except (TypeError, ValueError, ValidationError):
                return business_redirect("/admin/oauth-providers", error="invalid")
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

    @app.post("/admin/oauth-providers/{code}/update")
    async def oauth_providers_update(code: str, request: Request):
        """更新 Provider 地址、claims、登录用途和 client secret。"""
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
            tenant_id = tenant_id_of(principal)
            row = await oauth_provider_repo.get(db, code, tenant_id)
            if row is None:
                return business_redirect(
                    "/admin/oauth-providers", error="missing"
                )
            try:
                payload = OAuthProviderUpdate(**_provider_form_values(form))
            except (TypeError, ValueError, ValidationError):
                return business_redirect("/admin/oauth-providers", error="invalid")
            fields = payload.model_dump(exclude_unset=True)
            fields["scopes"] = " ".join(payload.scopes or [])
            for key in (
                "auto_create",
                "role_claim_required",
                "client_login_enabled",
                "admin_login_enabled",
                "enabled",
            ):
                fields[key] = int(fields[key])
            secret_kwargs: dict = {}
            if as_bool(form.get("clear_client_secret")):
                secret_kwargs["client_secret"] = ""
            else:
                replacement = str(form.get("client_secret") or "")
                if replacement:
                    secret_kwargs["client_secret"] = replacement
            await oauth_provider_repo.update(
                db,
                row,
                **secret_kwargs,
                **fields,
            )
            await audit_principal_action(
                db,
                principal,
                action="oauth_provider.update",
                target_type="oauth_provider",
                target_id=code,
                tenant_id=tenant_id,
                detail={
                    "client_login": bool(row.client_login_enabled),
                    "admin_login": bool(row.admin_login_enabled),
                },
            )
            await db.commit()
            return business_redirect(
                "/admin/oauth-providers", notice="updated"
            )
        finally:
            await db.close()

    @app.post("/admin/oauth-providers/{code}/toggle")
    async def oauth_providers_toggle(code: str, request: Request):
        """切换 Provider 总开关，不修改客户端/管理员用途开关。"""
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
            tenant_id = tenant_id_of(principal)
            row = await oauth_provider_repo.get(db, code, tenant_id)
            if row is None:
                return business_redirect(
                    "/admin/oauth-providers", error="missing"
                )
            await oauth_provider_repo.update(
                db,
                row,
                enabled=1 if as_bool(form.get("enabled")) else 0,
            )
            await audit_principal_action(
                db,
                principal,
                action="oauth_provider.toggle",
                target_type="oauth_provider",
                target_id=code,
                tenant_id=tenant_id,
                detail={"enabled": bool(row.enabled)},
            )
            await db.commit()
            return business_redirect(
                "/admin/oauth-providers", notice="toggled"
            )
        finally:
            await db.close()

    @app.post("/admin/oauth-providers/{code}/delete")
    async def oauth_providers_delete(code: str, request: Request):
        """删除尚未绑定身份或管理员角色的 OAuth Provider。"""
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
            tenant_id = tenant_id_of(principal)
            row = await oauth_provider_repo.get(db, code, tenant_id)
            if row is None:
                return business_redirect(
                    "/admin/oauth-providers", error="missing"
                )
            try:
                await oauth_provider_repo.delete(db, row)
            except sa.exc.IntegrityError:
                await db.rollback()
                return business_redirect(
                    "/admin/oauth-providers", error="failed"
                )
            await audit_principal_action(
                db,
                principal,
                action="oauth_provider.delete",
                target_type="oauth_provider",
                target_id=code,
                tenant_id=tenant_id,
            )
            await db.commit()
            return business_redirect(
                "/admin/oauth-providers", notice="deleted"
            )
        finally:
            await db.close()
