"""访问控制：组件权限配置、角色策略与 OAuth 角色映射。"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from ludic.web import Request
from starlette.responses import RedirectResponse

from app.models.admin_control import (
    AdminComponentDefinition, AdminComponentPermission, CasbinRule,
    OAuthRoleMapping, permission_binding_scope,
)
from app.models.oauth import OAuthProvider
from services.admin_web.audit import audit_principal_action
from services.admin_web.common import (
    html_error, not_found, origin_ok, require_csrf, unauthorized, with_auth,
)
from services.admin_web.rbac import casbin_service
from services.admin_web.casbin_rules import _filter_rules, _sort_rules
from services.admin_web.listing import parse_list_params
from services.admin_web.registry import can_access_page, visible_pages
from services.admin_web.routes.access import (
    _may_bind_permissions, _parse_permissions, _target_binding_tenant,
)
from services.admin_web.views import (
    access_components_page, mappings_page, roles_page,
)


def register(app: Any) -> None:
    """把组件权限、角色策略与角色映射的路由挂到 app 上。"""

    @app.get("/admin/access/components")
    async def access_components_get(request: Request):
        """组件权限配置页。"""
        values = await with_auth(request)
        if values is None:
            return unauthorized()
        db, principal, pref, csrf = values
        try:
            if not await can_access_page(db, principal, "access_components"):
                return not_found()
            components = list(
                await db.scalars(
                    sa.select(AdminComponentDefinition).order_by(
                        AdminComponentDefinition.page_code,
                        AdminComponentDefinition.sort_order,
                    )
                )
            )
            statements = sa.select(AdminComponentPermission)
            if not principal.is_platform_super_admin:
                statements = statements.where(
                    sa.or_(
                        AdminComponentPermission.tenant_id.is_(None),
                        AdminComponentPermission.tenant_id == principal.tenant_id,
                    )
                )
            rows = list(await db.scalars(statements))
            bindings: dict[tuple[str, str], list[AdminComponentPermission]] = {}
            for row in rows:
                key = (row.page_code, row.component_code)
                bindings.setdefault(key, []).append(row)
            if not principal.is_platform_super_admin:
                for key in set(bindings):
                    tenant_rows = [row for row in bindings[key] if row.tenant_id]
                    if tenant_rows:
                        bindings[key] = tenant_rows
            return access_components_page(
                principal=principal,
                csrf=csrf,
                pages=await visible_pages(db, principal),
                components=components,
                bindings=bindings,
                theme=pref.theme,
                color_mode=pref.color_mode,
                density=pref.density,
            )
        finally:
            await db.close()

    @app.post(
        "/admin/access/components/{page_code}/{component_code}/permissions"
    )
    async def access_component_permissions_post(
        page_code: str, component_code: str, request: Request
    ):
        """保存组件权限绑定。"""
        values = await with_auth(request)
        if values is None:
            return unauthorized()
        db, principal, _, _ = values
        try:
            if not origin_ok(request) or not await can_access_page(
                db, principal, "access_components"
            ):
                return not_found()
            form = await request.form()
            if not await require_csrf(db, request, principal, form):
                return html_error(
                    title_text="Request rejected",
                    message="CSRF validation failed.",
                    status_code=403,
                )
            component = await db.get(
                AdminComponentDefinition, (page_code, component_code)
            )
            if component is None:
                return not_found()
            permissions = _parse_permissions(str(form.get("permissions") or ""))
            if not await _may_bind_permissions(db, principal, permissions):
                return html_error(
                    title_text="Forbidden",
                    message="You cannot bind permissions outside your own scope.",
                    status_code=403,
                )
            try:
                tenant_id = _target_binding_tenant(principal, form)
            except ValueError:
                return html_error(
                    title_text="Invalid policy",
                    message="The requested tenant scope is invalid.",
                    status_code=400,
                )
            except PermissionError:
                return html_error(
                    title_text="Forbidden",
                    message="You cannot change another tenant's UI policy.",
                    status_code=403,
                )
            await db.execute(
                sa.delete(AdminComponentPermission).where(
                    AdminComponentPermission.page_code == page_code,
                    AdminComponentPermission.component_code == component_code,
                    (
                        AdminComponentPermission.tenant_id.is_(None)
                        if tenant_id is None
                        else AdminComponentPermission.tenant_id == tenant_id
                    ),
                )
            )
            for object_name, action in permissions:
                db.add(
                    AdminComponentPermission(
                        tenant_id=tenant_id,
                        binding_scope=permission_binding_scope(tenant_id),
                        page_code=page_code,
                        component_code=component_code,
                        object_name=object_name,
                        action_name=action,
                    )
                )
            await audit_principal_action(
                db,
                principal,
                action="admin_ui.component_permissions.update",
                target_type="admin_component",
                target_id=f"{page_code}/{component_code}",
                tenant_id=tenant_id,
                detail={
                    "scope": permission_binding_scope(tenant_id),
                    "permissions": [
                        f"{object_name}.{action}"
                        for object_name, action in permissions
                    ],
                },
            )
            await db.commit()
            return RedirectResponse(
                "/admin/access/components?notice=saved", status_code=303
            )
        finally:
            await db.close()

    @app.get("/admin/access/roles")
    async def access_roles_get(request: Request):
        """Casbin policy 只读页面。"""
        values = await with_auth(request)
        if values is None:
            return unauthorized()
        db, principal, pref, csrf = values
        try:
            if not await can_access_page(db, principal, "access_roles"):
                return not_found()
            # 列表页的搜索 / 排序 / 分页（UX-06）。
            list_params = parse_list_params(
                request,
                allowed_sorts=("type", "subject"),
            )
            rules = await casbin_service.list_policies(
                db,
                tenant_id=(
                    None
                    if principal.is_platform_super_admin
                    else principal.tenant_id
                ),
            )
            rules = _filter_rules(rules, list_params.q)
            rules = _sort_rules(rules, list_params.sort)
            total = len(rules)
            start = list_params.offset
            rules = rules[start:start + list_params.size]
            return roles_page(
                principal=principal,
                csrf=csrf,
                pages=await visible_pages(db, principal),
                rules=rules,
                theme=pref.theme,
                color_mode=pref.color_mode,
                density=pref.density,
                list_params=list_params,
                list_total=total,
            )
        finally:
            await db.close()

    @app.get("/admin/access/mappings")
    async def access_mappings_get(request: Request):
        """OAuth 角色映射页。"""
        values = await with_auth(request)
        if values is None:
            return unauthorized()
        db, principal, pref, csrf = values
        try:
            if not await can_access_page(db, principal, "access_mappings"):
                return not_found()
            mapping_query = sa.select(OAuthRoleMapping).join(
                OAuthProvider, OAuthProvider.id == OAuthRoleMapping.provider_id
            )
            provider_query = sa.select(OAuthProvider).where(
                OAuthProvider.admin_login_enabled == 1
            )
            if not principal.is_platform_super_admin:
                mapping_query = mapping_query.where(
                    OAuthProvider.tenant_id == principal.tenant_id
                )
                provider_query = provider_query.where(
                    OAuthProvider.tenant_id == principal.tenant_id
                )
            return mappings_page(
                principal=principal,
                csrf=csrf,
                pages=await visible_pages(db, principal),
                mappings=list(
                    await db.scalars(
                        mapping_query.order_by(OAuthRoleMapping.id)
                    )
                ),
                providers=list(
                    await db.scalars(provider_query.order_by(OAuthProvider.code))
                ),
                theme=pref.theme,
                color_mode=pref.color_mode,
                density=pref.density,
            )
        finally:
            await db.close()

    @app.post("/admin/access/mappings")
    async def access_mappings_post(request: Request):
        """新增 OAuth 外部角色映射。"""
        values = await with_auth(request)
        if values is None:
            return unauthorized()
        db, principal, _, _ = values
        try:
            if not origin_ok(request) or not await can_access_page(
                db, principal, "access_mappings"
            ):
                return not_found()
            form = await request.form()
            if not await require_csrf(db, request, principal, form):
                return not_found()
            try:
                provider_id = int(str(form.get("provider_id") or ""))
            except ValueError:
                return not_found()
            provider = await db.get(OAuthProvider, provider_id)
            if provider is None or (
                not principal.is_platform_super_admin
                and provider.tenant_id != principal.tenant_id
            ) or provider.admin_login_enabled != 1:
                return not_found()
            claim_name = str(form.get("claim_name") or "groups").strip()
            external_value = str(form.get("external_value") or "").strip()
            role_code = str(form.get("role_code") or "").strip()
            if not external_value or not role_code:
                return RedirectResponse(
                    "/admin/access/mappings?error=missing_fields", status_code=303
                )
            role_exists = await db.scalar(
                sa.select(CasbinRule.id).where(
                    CasbinRule.ptype == "p",
                    CasbinRule.v0 == f"role:{role_code}",
                    CasbinRule.v1.in_(
                        (
                            f"tenant:{provider.tenant_id}",
                            "tenant:*",
                            "*",
                        )
                    ),
                )
            )
            if role_exists is None:
                return RedirectResponse(
                    "/admin/access/mappings?error=unknown_role", status_code=303
                )
            if await db.scalar(
                sa.select(OAuthRoleMapping.id).where(
                    OAuthRoleMapping.provider_id == provider_id,
                    OAuthRoleMapping.claim_name == claim_name,
                    OAuthRoleMapping.external_value == external_value,
                )
            ):
                return RedirectResponse(
                    "/admin/access/mappings?error=already_exists", status_code=303
                )
            db.add(
                OAuthRoleMapping(
                    provider_id=provider_id,
                    claim_name=claim_name,
                    external_value=external_value,
                    role_code=role_code,
                )
            )
            await audit_principal_action(
                db,
                principal,
                action="casbin.oauth_mapping.create",
                target_type="oauth_role_mapping",
                target_id=f"{provider_id}:{claim_name}:{external_value}",
                tenant_id=provider.tenant_id,
                detail={"role_code": role_code},
            )
            await db.commit()
            return RedirectResponse(
                "/admin/access/mappings?notice=saved", status_code=303
            )
        finally:
            await db.close()
