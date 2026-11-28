"""Casbin 角色写操作的 JSON API（V4.10）。

覆盖角色创建、权限替换、继承替换与删除。判定与页面路由
``routes/access_roles_write.py`` 完全一致：同样的角色码正则、同样的
租户域限制、同样的循环继承检测、同样的「被引用就不能删」。

差别只有两点：请求体是 JSON，失败返回 ``{"code": ..., "message": ...}``。
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from ludic.web import Request

from app.models.admin_control import CasbinRule, OAuthRoleMapping
from app.models.oauth import OAuthProvider
from services.admin_web.access_api import parse_api_permissions
from services.admin_web.access_policy import (
    BUILTIN_ROLES,
    ROLE_CODE_RE,
    ROLE_DOMAIN_RE,
    creates_role_cycle,
    permission_id,
)
from services.admin_web.api_support import (
    ApiError,
    api_route,
    authorize,
    json_response,
    read_json_body,
)
from services.admin_web.audit import audit_principal_action
from services.admin_web.rbac import casbin_service
from services.admin_web.routes.access import _may_bind_permissions
from services.admin_web.routes.access_roles import _role_exists

PAGE_CODE = "access_roles"
COMPONENT_CODE = "policy_list"


def _resolve_domain(principal: Any, raw: Any) -> str:
    """解析角色所在域名，并挡住跨租户操作。"""
    text = str(raw or "tenant:*").strip()
    if not principal.is_platform_super_admin:
        expected = f"tenant:{principal.tenant_id or ''}"
        if text != expected:
            raise ApiError("forbidden", "不能管理其他租户的角色", status_code=403)
    if not ROLE_DOMAIN_RE.fullmatch(text):
        raise ApiError("invalid_input", "角色域名不合法", status_code=400)
    return text


def register(app: Any) -> None:
    """把角色写操作的 JSON API 挂到 app 上。"""

    @app.post("/admin/api/access/roles")
    @api_route
    async def api_access_roles_create(request: Request):
        """创建带初始权限集的新角色。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=True
        )
        try:
            body = await read_json_body(request)
            role_code = str(body.get("code") or "").strip()
            if not ROLE_CODE_RE.fullmatch(role_code):
                raise ApiError("invalid_input", "角色编码不合法", status_code=400)
            domain = _resolve_domain(principal, body.get("domain"))
            permissions = parse_api_permissions(body.get("permissions"))
            if not permissions:
                raise ApiError("invalid_input", "角色至少要有一项权限", status_code=400)
            if not await _may_bind_permissions(db, principal, permissions):
                raise ApiError(
                    "invalid_input", "不能绑定超出自身范围的权限", status_code=400
                )
            if await _role_exists(db, role_code, domain):
                raise ApiError("duplicate", "角色已存在", status_code=409)
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
            return json_response(
                {
                    "item": {
                        "code": role_code,
                        "domain": domain,
                        "permissions": [permission_id(p) for p in permissions],
                        "parents": [],
                    }
                },
                status_code=201,
            )
        finally:
            await db.close()

    @app.put("/admin/api/access/roles/{role_code}")
    @api_route
    async def api_access_roles_update(role_code: str, request: Request):
        """原子替换一个角色的权限集合。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=True
        )
        try:
            body = await read_json_body(request)
            domain = _resolve_domain(principal, body.get("domain"))
            permissions = parse_api_permissions(body.get("permissions"))
            if not await _role_exists(db, role_code, domain):
                raise ApiError("not_found", "角色不存在", status_code=404)
            if not permissions:
                raise ApiError("invalid_input", "角色至少要有一项权限", status_code=400)
            if not await _may_bind_permissions(db, principal, permissions):
                raise ApiError(
                    "invalid_input", "不能绑定超出自身范围的权限", status_code=400
                )
            if role_code == "tenant_admin" and not {
                ("admin_ui", "manage"),
                ("casbin", "mapping_manage"),
            }.issubset(permissions):
                raise ApiError(
                    "invalid_input",
                    "租户管理员必须保留界面管理与映射管理权限",
                    status_code=400,
                )
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
            if set(permissions) != wanted:
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
            return json_response(
                {
                    "item": {
                        "code": role_code,
                        "domain": domain,
                        "permissions": [permission_id(p) for p in permissions],
                    }
                }
            )
        finally:
            await db.close()

    @app.put("/admin/api/access/roles/{role_code}/inheritance")
    @api_route
    async def api_access_roles_inheritance(role_code: str, request: Request):
        """替换角色的父角色，并拒绝跨域、未知角色和循环继承。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=True
        )
        try:
            body = await read_json_body(request)
            domain = _resolve_domain(principal, body.get("domain"))
            if not await _role_exists(db, role_code, domain):
                raise ApiError("not_found", "角色不存在", status_code=404)
            raw_parents = body.get("parents") or []
            if not isinstance(raw_parents, list):
                raise ApiError("invalid_input", "parents 必须是数组", status_code=400)
            parents = sorted({str(value).strip() for value in raw_parents if str(value).strip()})
            if role_code in parents or any(
                not ROLE_CODE_RE.fullmatch(parent) for parent in parents
            ):
                raise ApiError("invalid_input", "父角色不合法", status_code=400)
            for parent in parents:
                if not await _role_exists(db, parent, domain):
                    raise ApiError(
                        "invalid_input", "父角色在该域名下不存在", status_code=400
                    )
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
                raise ApiError("invalid_input", "继承关系不能形成环", status_code=400)
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
            return json_response(
                {
                    "item": {
                        "code": role_code,
                        "domain": domain,
                        "parents": parents,
                    }
                }
            )
        finally:
            await db.close()

    @app.delete("/admin/api/access/roles/{role_code}")
    @api_route
    async def api_access_roles_delete(role_code: str, request: Request):
        """删除未被映射、成员或子角色引用的自定义角色。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=True
        )
        try:
            domain = _resolve_domain(principal, request.query_params.get("domain"))
            if role_code in BUILTIN_ROLES:
                raise ApiError("in_use", "内置角色不可删除", status_code=409)
            if not await _role_exists(db, role_code, domain):
                raise ApiError("not_found", "角色不存在", status_code=404)
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
                raise ApiError("in_use", "角色仍被引用，不能删除", status_code=409)
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
            return json_response({"item": {"code": role_code, "domain": domain}})
        finally:
            await db.close()
