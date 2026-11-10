"""访问控制：角色创建、权限、继承与删除。"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from ludic.web import Request

from app.models.admin_control import CasbinRule, OAuthRoleMapping
from app.models.oauth import OAuthProvider
from services.admin_web.access_policy import (
    BUILTIN_ROLES,
    ROLE_CODE_RE,
    creates_role_cycle,
    form_permissions,
    permission_id,
)
from services.admin_web.audit import audit_principal_action
from services.admin_web.common import (
    business_redirect,
    html_error,
    not_found,
    origin_ok,
    require_csrf,
    unauthorized,
    with_auth,
)
from services.admin_web.rbac import casbin_service
from services.admin_web.registry import can_access_page
from services.admin_web.routes.access import _may_bind_permissions
from services.admin_web.routes.access_roles import (
    _invalid,
    _role_exists,
    _target_domain,
)


def register(app: Any) -> None:
    """把角色写操作挂到 app 上。"""

    @app.post("/admin/access/roles")
    async def access_roles_create(request: Request):
        """创建带初始权限集的新角色。"""
        values = await with_auth(request)
        if values is None:
            return unauthorized()
        db, principal, _, _ = values
        try:
            if not origin_ok(request) or not await can_access_page(
                db, principal, "access_roles"
            ):
                return not_found()
            form = await request.form()
            if not await require_csrf(db, request, principal, form):
                return not_found()
            role_code = str(form.get("role_code") or "").strip()
            if not ROLE_CODE_RE.fullmatch(role_code):
                return _invalid("invalid")
            try:
                domain = _target_domain(principal, form)
                permissions = form_permissions(form)
            except PermissionError:
                return html_error(
                    title_text="Forbidden",
                    message="You cannot manage another tenant's roles.",
                    status_code=403,
                )
            except ValueError:
                return _invalid("invalid")
            if not permissions or not await _may_bind_permissions(
                db, principal, permissions
            ):
                return _invalid("invalid")
            if await _role_exists(db, role_code, domain):
                return business_redirect(
                    "/admin/access/roles", error="duplicate"
                )
            for object_name, action in permissions:
                db.add(
                    CasbinRule(
                        ptype="p",
                        v0=f"role:{role_code}",
                        v1=domain,
                        v2=object_name,
                        v3=action,
                    )
                )
            await casbin_service.bump_version(db)
            await audit_principal_action(
                db,
                principal,
                action="casbin.role.create",
                target_type="casbin_role",
                target_id=f"{role_code}@{domain}",
                detail={"permissions": [permission_id(p) for p in permissions]},
            )
            await db.commit()
            return business_redirect("/admin/access/roles", notice="created")
        finally:
            await db.close()

    @app.post("/admin/access/roles/{role_code}/permissions")
    async def access_role_permissions(request: Request, role_code: str):
        """原子替换一个角色的权限集合。"""
        values = await with_auth(request)
        if values is None:
            return unauthorized()
        db, principal, _, _ = values
        try:
            if not origin_ok(request) or not await can_access_page(
                db, principal, "access_roles"
            ):
                return not_found()
            form = await request.form()
            if not await require_csrf(db, request, principal, form):
                return not_found()
            try:
                domain = _target_domain(principal, form)
                permissions = form_permissions(form)
            except PermissionError:
                return not_found()
            except ValueError:
                return _invalid("invalid")
            if not await _role_exists(db, role_code, domain):
                return not_found()
            if not permissions or not await _may_bind_permissions(
                db, principal, permissions
            ):
                return _invalid("invalid")
            if role_code == "tenant_admin" and not {
                ("admin_ui", "manage"),
                ("casbin", "mapping_manage"),
            }.issubset(permissions):
                return _invalid("invalid")
            current = list(
                await db.scalars(
                    sa.select(CasbinRule).where(
                        CasbinRule.ptype == "p",
                        CasbinRule.v0 == f"role:{role_code}",
                        CasbinRule.v1 == domain,
                    )
                )
            )
            wanted = {(rule.v2 or "", rule.v3 or "") for rule in current}
            new_permissions = set(permissions)
            if new_permissions != wanted:
                await db.execute(
                    sa.delete(CasbinRule).where(
                        CasbinRule.ptype == "p",
                        CasbinRule.v0 == f"role:{role_code}",
                        CasbinRule.v1 == domain,
                    )
                )
                for object_name, action in permissions:
                    db.add(
                        CasbinRule(
                            ptype="p",
                            v0=f"role:{role_code}",
                            v1=domain,
                            v2=object_name,
                            v3=action,
                        )
                    )
                await casbin_service.bump_version(db)
                await audit_principal_action(
                    db,
                    principal,
                    action="casbin.role.permissions.update",
                    target_type="casbin_role",
                    target_id=f"{role_code}@{domain}",
                    detail={
                        "before": sorted(permission_id(p) for p in wanted),
                        "after": sorted(permission_id(p) for p in permissions),
                    },
                )
                await db.commit()
            return business_redirect("/admin/access/roles", notice="updated")
        finally:
            await db.close()

    @app.post("/admin/access/roles/{role_code}/inheritance")
    async def access_role_inheritance(request: Request, role_code: str):
        """替换角色的父角色，并拒绝跨域、未知角色和循环继承。"""
        values = await with_auth(request)
        if values is None:
            return unauthorized()
        db, principal, _, _ = values
        try:
            if not origin_ok(request) or not await can_access_page(
                db, principal, "access_roles"
            ):
                return not_found()
            form = await request.form()
            if not await require_csrf(db, request, principal, form):
                return not_found()
            try:
                domain = _target_domain(principal, form)
            except PermissionError:
                return not_found()
            except ValueError:
                return _invalid("invalid")
            if not await _role_exists(db, role_code, domain):
                return not_found()
            parents = sorted(
                {
                    str(value).strip()
                    for value in form.getlist("parents")
                    if str(value).strip()
                }
            )
            missing_parent = False
            for parent in parents:
                if not await _role_exists(db, parent, domain):
                    missing_parent = True
                    break
            if (
                role_code in parents
                or any(not ROLE_CODE_RE.fullmatch(parent) for parent in parents)
                or missing_parent
            ):
                return _invalid("invalid")
            all_role_rules = list(
                await db.scalars(
                    sa.select(CasbinRule).where(
                        CasbinRule.ptype == "g",
                        CasbinRule.v0.like("role:%"),
                        CasbinRule.v2 == domain,
                    )
                )
            )
            if creates_role_cycle(role_code, domain, parents, all_role_rules):
                return _invalid("invalid")
            await db.execute(
                sa.delete(CasbinRule).where(
                    CasbinRule.ptype == "g",
                    CasbinRule.v0 == f"role:{role_code}",
                    CasbinRule.v2 == domain,
                )
            )
            for parent in parents:
                db.add(
                    CasbinRule(
                        ptype="g",
                        v0=f"role:{role_code}",
                        v1=f"role:{parent}",
                        v2=domain,
                    )
                )
            await casbin_service.bump_version(db)
            await audit_principal_action(
                db,
                principal,
                action="casbin.role.inheritance.update",
                target_type="casbin_role",
                target_id=f"{role_code}@{domain}",
                detail={"parents": parents},
            )
            await db.commit()
            return business_redirect("/admin/access/roles", notice="updated")
        finally:
            await db.close()

    @app.post("/admin/access/roles/{role_code}/delete")
    async def access_role_delete(request: Request, role_code: str):
        """删除未被映射、成员或子角色引用的自定义角色。"""
        values = await with_auth(request)
        if values is None:
            return unauthorized()
        db, principal, _, _ = values
        try:
            if not origin_ok(request) or not await can_access_page(
                db, principal, "access_roles"
            ):
                return not_found()
            form = await request.form()
            if not await require_csrf(db, request, principal, form):
                return not_found()
            try:
                domain = _target_domain(principal, form)
            except PermissionError:
                return not_found()
            except ValueError:
                return _invalid("invalid")
            if role_code in BUILTIN_ROLES:
                return _invalid("in_use")
            if not await _role_exists(db, role_code, domain):
                return not_found()
            reference_statement = (
                sa.select(sa.func.count())
                .select_from(CasbinRule)
                .where(
                    CasbinRule.ptype == "g",
                    CasbinRule.v1 == f"role:{role_code}",
                )
            )
            if domain not in {"tenant:*", "*"}:
                reference_statement = reference_statement.where(
                    CasbinRule.v2.in_((domain, "tenant:*", "*"))
                )
            references = await db.scalar(reference_statement)
            mapping_statement = (
                sa.select(sa.func.count())
                .select_from(OAuthRoleMapping)
                .join(
                    OAuthProvider,
                    OAuthProvider.id == OAuthRoleMapping.provider_id,
                )
                .where(OAuthRoleMapping.role_code == role_code)
            )
            if not principal.is_platform_super_admin:
                mapping_statement = mapping_statement.where(
                    OAuthProvider.tenant_id == principal.tenant_id
                )
            mapping_count = await db.scalar(mapping_statement)
            if references or mapping_count:
                return _invalid("in_use")
            await db.execute(
                sa.delete(CasbinRule).where(
                    sa.or_(
                        sa.and_(
                            CasbinRule.ptype == "p",
                            CasbinRule.v0 == f"role:{role_code}",
                            CasbinRule.v1 == domain,
                        ),
                        sa.and_(
                            CasbinRule.ptype == "g",
                            CasbinRule.v0 == f"role:{role_code}",
                            CasbinRule.v2 == domain,
                        ),
                    )
                )
            )
            await casbin_service.bump_version(db)
            await audit_principal_action(
                db,
                principal,
                action="casbin.role.delete",
                target_type="casbin_role",
                target_id=f"{role_code}@{domain}",
            )
            await db.commit()
            return business_redirect("/admin/access/roles", notice="deleted")
        finally:
            await db.close()
