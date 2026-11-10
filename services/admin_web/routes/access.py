"""访问控制：页面权限配置与权限解析辅助。"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from ludic.web import Request
from starlette.responses import RedirectResponse

from app.config.settings import settings
from app.models.admin_control import (
    AdminPageDefinition, AdminPagePermission, permission_binding_scope,
)
from app.models.oauth import OAuthProvider
from services.admin_web.accounts import AdminPrincipal
from services.admin_web.access_policy import (
    CRITICAL_PAGES,
    KNOWN_PERMISSIONS,
    PLATFORM_ONLY_PERMISSIONS,
    TENANT_ID_RE,
    form_permissions,
    parse_permission_values,
)
from services.admin_web.audit import audit_principal_action
from services.admin_web.common import (
    business_redirect,
    flash_from_query,
    html_error,
    not_found,
    origin_ok,
    require_csrf,
    unauthorized,
    with_auth,
)
from services.admin_web.rbac import casbin_service
from services.admin_web.registry import (
    can_access_page,
    page_spec,
    visible_pages,
)
from services.admin_web.views import access_pages_page


def _parse_permissions(raw: str) -> list[tuple[str, str]]:
    """解析 `object.action` 逗号列表并拒绝未知权限。"""
    return parse_permission_values([raw])


async def _may_bind_permissions(
    db, principal: AdminPrincipal, permissions: list[tuple[str, str]]
) -> bool:
    """租户管理员只能绑定自己拥有的普通权限。"""
    if principal.is_platform_super_admin:
        return True
    for object_name, action in permissions:
        if (object_name, action) not in KNOWN_PERMISSIONS:
            return False
        if object_name in {"cross_tenant", "oauth_provider", "platform_admin"}:
            return False
        if (object_name, action) in PLATFORM_ONLY_PERMISSIONS:
            return False
        if not await casbin_service.can(
            db,
            user_id=principal.user_id or "",
            tenant_id=principal.tenant_id or "default",
            object_name=object_name,
            action=action,
        ):
            return False
    return True


async def _bindable_permissions(
    db, principal: AdminPrincipal
) -> list[tuple[str, str]]:
    """列出当前管理员有权继续绑定的权限。"""
    if principal.is_platform_super_admin:
        return sorted(KNOWN_PERMISSIONS)
    return [
        permission
        for permission in sorted(KNOWN_PERMISSIONS)
        if await _may_bind_permissions(db, principal, [permission])
    ]


async def _available_tenants(
    db, principal: AdminPrincipal, rows: list[Any]
) -> list[str]:
    """列出可创建覆盖的租户。"""
    if not principal.is_platform_super_admin:
        return [principal.tenant_id] if principal.tenant_id else []
    values = {
        row.tenant_id for row in rows if getattr(row, "tenant_id", None)
    }
    if settings.auth_default_tenant:
        values.add(settings.auth_default_tenant)
    values.update(
        tenant_id
        for tenant_id in await db.scalars(
            sa.select(OAuthProvider.tenant_id).distinct()
        )
        if tenant_id
    )
    return sorted(values)


def _target_binding_tenant(principal: AdminPrincipal, form) -> str | None:
    """解析并限制页面/组件权限写入的目标租户。"""
    raw = str(form.get("tenant_id") or "").strip()
    if principal.is_platform_super_admin:
        if not raw:
            return None
        if not TENANT_ID_RE.fullmatch(raw):
            raise ValueError("invalid_tenant")
        return raw
    if not principal.tenant_id:
        raise PermissionError("tenant_required")
    if raw and raw != principal.tenant_id:
        raise PermissionError("cross_tenant_forbidden")
    return principal.tenant_id


def register(app: Any) -> None:
    """把页面权限配置的路由挂到 app 上。"""

    @app.get("/admin/access/pages")
    async def access_pages_get(request: Request):
        """页面权限配置页。"""
        values = await with_auth(request)
        if values is None:
            return unauthorized()
        db, principal, pref, csrf = values
        try:
            if not await can_access_page(db, principal, "access_pages"):
                return not_found()
            definitions = list(
                await db.scalars(
                    sa.select(AdminPageDefinition).order_by(AdminPageDefinition.nav_order)
                )
            )
            statements = sa.select(AdminPagePermission)
            if not principal.is_platform_super_admin:
                statements = statements.where(
                    sa.or_(
                        AdminPagePermission.tenant_id.is_(None),
                        AdminPagePermission.tenant_id == principal.tenant_id,
                    )
                )
            rows = list(await db.scalars(statements))
            bindings: dict[str, list[AdminPagePermission]] = {}
            for row in rows:
                if (
                    principal.is_platform_super_admin
                    or row.tenant_id is None
                    or row.tenant_id == principal.tenant_id
                ):
                    bindings.setdefault(row.page_code, []).append(row)
            if not principal.is_platform_super_admin:
                # 租户管理员只编辑当前租户的覆盖值，平台 owner 则看到全部作用域。
                for page_code in {row.page_code for row in rows}:
                    tenant_rows = [
                        row
                        for row in rows
                        if row.page_code == page_code and row.tenant_id is not None
                    ]
                    if tenant_rows:
                        bindings[page_code] = tenant_rows
            flash, flash_kind = flash_from_query(request)
            bindable = await _bindable_permissions(db, principal)
            bindable_set = set(bindable)
            return access_pages_page(
                principal=principal,
                csrf=csrf,
                pages=await visible_pages(db, principal),
                definitions=definitions,
                bindings=bindings,
                recommended={
                    page.code: [
                        permission
                        for permission in spec.permissions
                        if permission in bindable_set
                    ]
                    for page in definitions
                    if (spec := page_spec(page.code)) is not None
                },
                permission_options=bindable,
                tenant_options=await _available_tenants(db, principal, rows),
                theme=pref.theme,
                color_mode=pref.color_mode,
                density=pref.density,
                flash=flash,
                flash_kind=flash_kind,
            )
        finally:
            await db.close()

    @app.post("/admin/access/pages/{page_code}/permissions")
    async def access_page_permissions_post(page_code: str, request: Request):
        """保存页面权限绑定。"""
        values = await with_auth(request)
        if values is None:
            return unauthorized()
        db, principal, _, _ = values
        try:
            if not origin_ok(request):
                return not_found()
            if not await can_access_page(db, principal, "access_pages"):
                return not_found()
            form = await request.form()
            if not await require_csrf(db, request, principal, form):
                return html_error(
                    title_text="Request rejected",
                    message="CSRF validation failed.",
                    status_code=403,
                )
            definition = await db.get(AdminPageDefinition, page_code)
            if definition is None:
                return not_found()
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
            operation = str(form.get("operation") or "save")
            if operation not in {"save", "reset"}:
                return business_redirect(
                    "/admin/access/pages", error="invalid"
                )
            if operation == "reset":
                if tenant_id is None:
                    return business_redirect(
                        "/admin/access/pages", error="invalid"
                    )
                await db.execute(
                    sa.delete(AdminPagePermission).where(
                        AdminPagePermission.page_code == page_code,
                        AdminPagePermission.tenant_id == tenant_id,
                    )
                )
                await audit_principal_action(
                    db,
                    principal,
                    action="admin_ui.page_permissions.reset",
                    target_type="admin_page",
                    target_id=page_code,
                    tenant_id=tenant_id,
                    detail={"scope": permission_binding_scope(tenant_id)},
                )
                await db.commit()
                return business_redirect(
                    "/admin/access/pages", notice="updated"
                )
            permissions = form_permissions(form)
            if page_code in CRITICAL_PAGES and not permissions:
                return html_error(
                    title_text="Invalid policy",
                    message="Critical system pages cannot be made inaccessible.",
                    status_code=400,
                )
            if tenant_id is not None and not permissions:
                return business_redirect(
                    "/admin/access/pages", error="invalid"
                )
            if not await _may_bind_permissions(db, principal, permissions):
                return html_error(
                    title_text="Forbidden",
                    message="You cannot bind permissions outside your own scope.",
                    status_code=403,
                )
            await db.execute(
                sa.delete(AdminPagePermission).where(
                    AdminPagePermission.page_code == page_code,
                    (
                        AdminPagePermission.tenant_id.is_(None)
                        if tenant_id is None
                        else AdminPagePermission.tenant_id == tenant_id
                    ),
                )
            )
            for object_name, action in permissions:
                db.add(
                    AdminPagePermission(
                        tenant_id=tenant_id,
                        binding_scope=permission_binding_scope(tenant_id),
                        page_code=page_code,
                        object_name=object_name,
                        action_name=action,
                    )
                )
            await audit_principal_action(
                db,
                principal,
                action="admin_ui.page_permissions.update",
                target_type="admin_page",
                target_id=page_code,
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
                "/admin/access/pages?notice=updated", status_code=303
            )
        finally:
            await db.close()
