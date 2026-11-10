"""访问控制：Casbin 角色与权限矩阵查询。"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from ludic.web import Request
from starlette.responses import RedirectResponse

from app.models.admin_control import CasbinRule, OAuthRoleMapping
from app.models.oauth import OAuthProvider
from services.admin_web.access_policy import (
    KNOWN_PERMISSIONS,
    ROLE_DOMAIN_RE,
    permission_id,
    role_summary_rows,
)
from services.admin_web.common import (
    business_redirect,
    flash_from_query,
    not_found,
    unauthorized,
    with_auth,
)
from services.admin_web.listing import parse_list_params
from services.admin_web.rbac import casbin_service
from services.admin_web.registry import can_access_page, visible_pages
from services.admin_web.views import roles_page


def _target_domain(principal, form) -> str:
    """解析角色域名并限制租户管理员只能管理本租户。"""
    raw = str(form.get("domain") or "tenant:*").strip()
    if not principal.is_platform_super_admin:
        expected = f"tenant:{principal.tenant_id or ''}"
        if raw != expected:
            raise PermissionError("cross_tenant_forbidden")
    if not ROLE_DOMAIN_RE.fullmatch(raw):
        raise ValueError("invalid_domain")
    return raw


async def _role_exists(db, role_code: str, domain: str) -> bool:
    """判断角色是否至少有一条权限策略。"""
    return (
        await db.scalar(
            sa.select(CasbinRule.id).where(
                CasbinRule.ptype == "p",
                CasbinRule.v0 == f"role:{role_code}",
                CasbinRule.v1 == domain,
            )
        )
        is not None
    )


async def _mapping_counts(db, principal) -> dict[str, int]:
    """统计每个角色被多少条 OAuth 映射引用。"""
    statement = (
        sa.select(OAuthRoleMapping.role_code, sa.func.count())
        .join(OAuthProvider, OAuthProvider.id == OAuthRoleMapping.provider_id)
        .group_by(OAuthRoleMapping.role_code)
    )
    if not principal.is_platform_super_admin:
        statement = statement.where(OAuthProvider.tenant_id == principal.tenant_id)
    return {
        str(role_code): int(count)
        for role_code, count in (await db.execute(statement)).all()
    }


def _invalid(message: str) -> RedirectResponse:
    """返回角色页固定错误提示。"""
    return business_redirect(
        "/admin/access/roles",
        error="in_use" if message == "in_use" else "invalid",
    )


def register(app: Any) -> None:
    """把角色策略查询挂到 app 上。"""

    @app.get("/admin/access/roles")
    async def access_roles_get(request: Request):
        """角色、权限与继承管理页。"""
        values = await with_auth(request)
        if values is None:
            return unauthorized()
        db, principal, pref, csrf = values
        try:
            if not await can_access_page(db, principal, "access_roles"):
                return not_found()
            rules = await casbin_service.list_policies(
                db,
                tenant_id=(
                    None
                    if principal.is_platform_super_admin
                    else principal.tenant_id
                ),
            )
            all_roles = role_summary_rows(
                rules,
                mapping_counts=await _mapping_counts(db, principal),
            )
            complete_roles = all_roles
            list_params = parse_list_params(
                request, allowed_sorts=("subject", "type")
            )
            keyword = list_params.q.casefold()
            if keyword:
                all_roles = [
                    role
                    for role in all_roles
                    if keyword
                    in " ".join(
                        (
                            role.code,
                            role.domain,
                            *(
                                permission_id(permission)
                                for permission in role.permissions
                            ),
                        )
                    ).casefold()
                ]
            total = len(all_roles)
            start = list_params.offset
            roles = all_roles[start:start + list_params.size]
            flash, flash_kind = flash_from_query(request)
            return roles_page(
                principal=principal,
                csrf=csrf,
                pages=await visible_pages(db, principal),
                rules=rules,
                roles=roles,
                all_roles=complete_roles,
                permission_options=sorted(KNOWN_PERMISSIONS),
                theme=pref.theme,
                color_mode=pref.color_mode,
                density=pref.density,
                list_params=list_params,
                list_total=total,
                flash=flash,
                flash_kind=flash_kind,
            )
        finally:
            await db.close()
