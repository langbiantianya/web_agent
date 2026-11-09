"""访问控制：页面权限配置与权限解析辅助。"""

from __future__ import annotations

import re
from typing import Any

import sqlalchemy as sa
from ludic.web import Request
from starlette.responses import RedirectResponse

from app.models.admin_control import (
    AdminPageDefinition, AdminPagePermission, permission_binding_scope,
)
from services.admin_web.accounts import AdminPrincipal
from services.admin_web.audit import audit_principal_action
from services.admin_web.common import (
    html_error, not_found, origin_ok, require_csrf, unauthorized, with_auth,
)
from services.admin_web.rbac import casbin_service
from services.admin_web.registry import can_access_page, visible_pages
from services.admin_web.views import access_pages_page


CRITICAL_PAGES = {
    "dashboard",
    "access_pages",
    "access_components",
    "platform_admins",
}


TENANT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


KNOWN_PERMISSIONS = {
    ("admin", "read"),
    ("admin_ui", "read"),
    ("admin_ui", "manage"),
    ("workflow", "read"),
    ("workflow", "write"),
    ("agent", "read"),
    ("agent", "write"),
    ("skill", "read"),
    ("skill", "write"),
    ("mcp", "read"),
    ("mcp", "write"),
    ("mcp", "refresh"),
    ("llm", "read"),
    ("llm", "write"),
    ("audit", "read"),
    ("admin_user", "read"),
    ("admin_user", "manage"),
    ("casbin", "role_read"),
    ("casbin", "role_manage"),
    ("casbin", "mapping_manage"),
    ("oauth_provider", "read"),
    ("oauth_provider", "manage"),
    ("cross_tenant", "read"),
    ("cross_tenant", "manage"),
    ("platform_admin", "owner"),
}


def _parse_permissions(raw: str) -> list[tuple[str, str]]:
    """解析 `object.action` 逗号列表并拒绝未知权限。"""
    result: list[tuple[str, str]] = []
    for chunk in raw.replace("\n", ",").split(","):
        value = chunk.strip()
        if not value:
            continue
        if "." not in value:
            raise ValueError("invalid_permission")
        object_name, action = value.rsplit(".", 1)
        item = (object_name.strip(), action.strip())
        if item not in KNOWN_PERMISSIONS:
            raise ValueError("unknown_permission")
        result.append(item)
    return sorted(set(result))


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
        if not await casbin_service.can(
            db,
            user_id=principal.user_id or "",
            tenant_id=principal.tenant_id or "default",
            object_name=object_name,
            action=action,
        ):
            return False
    return True


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
                if row.tenant_id is None or row.tenant_id == principal.tenant_id:
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
            return access_pages_page(
                principal=principal,
                csrf=csrf,
                pages=await visible_pages(db, principal),
                definitions=definitions,
                bindings=bindings,
                theme=pref.theme,
                color_mode=pref.color_mode,
                density=pref.density,
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
            permissions = _parse_permissions(str(form.get("permissions") or ""))
            if page_code in CRITICAL_PAGES and not permissions:
                return html_error(
                    title_text="Invalid policy",
                    message="Critical system pages cannot be made inaccessible.",
                    status_code=400,
                )
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
                "/admin/access/pages?notice=saved", status_code=303
            )
        finally:
            await db.close()
