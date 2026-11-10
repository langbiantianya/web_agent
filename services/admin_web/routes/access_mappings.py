"""访问控制：OAuth 外部角色到 Casbin 角色的映射。"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from ludic.web import Request
from starlette.responses import RedirectResponse

from app.models.admin_control import CasbinRule, OAuthRoleMapping
from app.models.oauth import OAuthProvider
from services.admin_web.audit import audit_principal_action
from services.admin_web.common import (
    as_bool,
    business_redirect,
    flash_from_query,
    not_found,
    origin_ok,
    require_csrf,
    unauthorized,
    with_auth,
)
from services.admin_web.rbac import casbin_service
from services.admin_web.registry import can_access_page, visible_pages
from services.admin_web.views import mappings_page


async def _mapping_provider(db, principal, mapping: OAuthRoleMapping):
    """读取映射对应供应商，并验证当前管理员可见。"""
    provider = await db.get(OAuthProvider, mapping.provider_id)
    if provider is None:
        return None
    if (
        not principal.is_platform_super_admin
        and provider.tenant_id != principal.tenant_id
    ):
        return None
    return provider


async def _role_exists_for_tenant(db, role_code: str, tenant_id: str) -> bool:
    """检查角色在供应商租户是否有权限策略。"""
    return (
        await db.scalar(
            sa.select(CasbinRule.id).where(
                CasbinRule.ptype == "p",
                CasbinRule.v0 == f"role:{role_code}",
                CasbinRule.v1.in_(
                    (f"tenant:{tenant_id}", "tenant:*", "*")
                ),
            )
        )
        is not None
    )


async def _available_role_codes(db, principal) -> list[str]:
    """列出当前管理员可映射的 Casbin 角色代码。"""
    rules = await casbin_service.list_policies(
        db,
        tenant_id=(
            None
            if principal.is_platform_super_admin
            else principal.tenant_id
        ),
    )
    return sorted(
        {
            (rule.v0 or "").removeprefix("role:")
            for rule in rules
            if rule.ptype == "p" and (rule.v0 or "").startswith("role:")
        }
    )


def _provider_visible(principal, provider: OAuthProvider) -> bool:
    """判断供应商是否在管理员可管理租户内。"""
    return principal.is_platform_super_admin or (
        provider.tenant_id == principal.tenant_id
    )


def register(app: Any) -> None:
    """把 OAuth 角色映射查询和写操作挂到 app 上。"""

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
                OAuthProvider,
                OAuthProvider.id == OAuthRoleMapping.provider_id,
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
            flash, flash_kind = flash_from_query(request)
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
                role_codes=await _available_role_codes(db, principal),
                theme=pref.theme,
                color_mode=pref.color_mode,
                density=pref.density,
                flash=flash,
                flash_kind=flash_kind,
            )
        finally:
            await db.close()

    @app.post("/admin/access/mappings")
    async def access_mappings_create(request: Request):
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
            if (
                provider is None
                or not _provider_visible(principal, provider)
                or provider.admin_login_enabled != 1
            ):
                return not_found()
            claim_name = str(form.get("claim_name") or "groups").strip()
            external_value = str(form.get("external_value") or "").strip()
            role_code = str(form.get("role_code") or "").strip()
            if not external_value or not role_code:
                return business_redirect(
                    "/admin/access/mappings", error="invalid"
                )
            if not await _role_exists_for_tenant(
                db, role_code, provider.tenant_id
            ):
                return business_redirect(
                    "/admin/access/mappings", error="invalid"
                )
            if await db.scalar(
                sa.select(OAuthRoleMapping.id).where(
                    OAuthRoleMapping.provider_id == provider_id,
                    OAuthRoleMapping.claim_name == claim_name,
                    OAuthRoleMapping.external_value == external_value,
                )
            ):
                return business_redirect(
                    "/admin/access/mappings", error="duplicate"
                )
            mapping = OAuthRoleMapping(
                provider_id=provider_id,
                claim_name=claim_name,
                external_value=external_value,
                role_code=role_code,
            )
            db.add(mapping)
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
            return business_redirect(
                "/admin/access/mappings", notice="created"
            )
        finally:
            await db.close()

    @app.post("/admin/access/mappings/{mapping_id}/update")
    async def access_mappings_update(request: Request, mapping_id: int):
        """修改映射目标角色或启停状态。"""
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
            mapping = await db.get(OAuthRoleMapping, mapping_id)
            if mapping is None:
                return not_found()
            provider = await _mapping_provider(db, principal, mapping)
            if provider is None:
                return not_found()
            role_code = str(form.get("role_code") or "").strip()
            if not await _role_exists_for_tenant(
                db, role_code, provider.tenant_id
            ):
                return business_redirect(
                    "/admin/access/mappings", error="invalid"
                )
            before = {"role_code": mapping.role_code, "enabled": bool(mapping.enabled)}
            mapping.role_code = role_code
            mapping.enabled = 1 if as_bool(form.get("enabled")) else 0
            mapping.version = (mapping.version or 1) + 1
            await audit_principal_action(
                db,
                principal,
                action="casbin.oauth_mapping.update",
                target_type="oauth_role_mapping",
                target_id=str(mapping.id),
                tenant_id=provider.tenant_id,
                detail={
                    "before": before,
                    "after": {
                        "role_code": mapping.role_code,
                        "enabled": bool(mapping.enabled),
                    },
                },
            )
            await db.commit()
            return business_redirect(
                "/admin/access/mappings", notice="updated"
            )
        finally:
            await db.close()

    @app.post("/admin/access/mappings/{mapping_id}/delete")
    async def access_mappings_delete(request: Request, mapping_id: int):
        """删除 OAuth 外部角色映射。"""
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
            mapping = await db.get(OAuthRoleMapping, mapping_id)
            if mapping is None:
                return not_found()
            provider = await _mapping_provider(db, principal, mapping)
            if provider is None:
                return not_found()
            await audit_principal_action(
                db,
                principal,
                action="casbin.oauth_mapping.delete",
                target_type="oauth_role_mapping",
                target_id=str(mapping.id),
                tenant_id=provider.tenant_id,
                detail={
                    "claim_name": mapping.claim_name,
                    "external_value": mapping.external_value,
                    "role_code": mapping.role_code,
                },
            )
            await db.delete(mapping)
            await db.commit()
            return business_redirect(
                "/admin/access/mappings", notice="deleted"
            )
        finally:
            await db.close()
