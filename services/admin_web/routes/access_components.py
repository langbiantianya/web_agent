"""访问控制：组件权限配置。"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from ludic.web import Request
from starlette.responses import RedirectResponse

from app.models.admin_control import (
    AdminComponentDefinition, AdminComponentPermission, permission_binding_scope,
)
from services.admin_web.access_policy import form_permissions
from services.admin_web.audit import audit_principal_action
from services.admin_web.common import (
    html_error, not_found, origin_ok, require_csrf, unauthorized, with_auth,
)
from services.admin_web.registry import can_access_page, visible_pages
from services.admin_web.routes.access import (
    _may_bind_permissions, _target_binding_tenant,
)
from services.admin_web.views import access_components_page


def register(app: Any) -> None:
    """把组件权限路由挂到 app 上。"""

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
            permissions = form_permissions(form)
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
