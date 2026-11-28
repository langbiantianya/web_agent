"""OAuth 外部角色映射的 JSON API（V4.10）。

把「外部 IdP 里的角色值」映射到 Casbin 角色。与页面路由
``routes/access_mappings.py`` 共用可见性判定（供应商必须在管理员的租户
内、必须开启后台登录）与「角色必须在该租户存在」的检查。
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from ludic.web import Request

from app.models.admin_control import CasbinRule, OAuthRoleMapping
from app.models.oauth import OAuthProvider
from services.admin_web.api_support import (
    ApiError,
    api_route,
    authorize,
    json_response,
    read_json_body,
)
from services.admin_web.audit import audit_principal_action
from services.admin_web.rbac import casbin_service
from services.admin_web.routes.access_mappings import (
    _available_role_codes,
    _mapping_provider,
    _provider_visible,
    _role_exists_for_tenant,
)

PAGE_CODE = "access_mappings"
COMPONENT_CODE = "mapping_list"


def _mapping_payload(mapping: Any, provider: Any) -> dict[str, Any]:
    """把映射行投影成前端形状。"""
    return {
        "id": mapping.id,
        "providerId": mapping.provider_id,
        "providerCode": provider.code if provider is not None else "",
        "providerName": provider.display_name if provider is not None else "",
        "tenantId": provider.tenant_id if provider is not None else None,
        "claimName": mapping.claim_name,
        "externalValue": mapping.external_value,
        "roleCode": mapping.role_code,
        "enabled": bool(mapping.enabled),
    }


def register(app: Any) -> None:
    """把 OAuth 角色映射的 JSON API 挂到 app 上。"""

    @app.get("/admin/api/access/mappings")
    @api_route
    async def api_access_mappings_list(request: Request):
        """列出映射、可用供应商与可映射角色。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=False
        )
        try:
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
            mappings = list(await db.scalars(mapping_query.order_by(OAuthRoleMapping.id)))
            providers = list(
                await db.scalars(provider_query.order_by(OAuthProvider.code))
            )
            items = []
            for mapping in mappings:
                provider = await db.get(OAuthProvider, mapping.provider_id)
                items.append(_mapping_payload(mapping, provider))
            return json_response(
                {
                    "items": items,
                    "providers": [
                        {
                            "id": provider.id,
                            "code": provider.code,
                            "name": provider.display_name,
                            "tenantId": provider.tenant_id,
                        }
                        for provider in providers
                    ],
                    "roleCodes": await _available_role_codes(db, principal),
                }
            )
        finally:
            await db.close()

    @app.post("/admin/api/access/mappings")
    @api_route
    async def api_access_mappings_create(request: Request):
        """新增一条外部角色映射。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=True
        )
        try:
            body = await read_json_body(request)
            try:
                provider_id = int(str(body.get("providerId") or ""))
            except ValueError as exc:
                raise ApiError(
                    "invalid_input", "供应商不合法", status_code=400
                ) from exc
            provider = await db.get(OAuthProvider, provider_id)
            if (
                provider is None
                or not _provider_visible(principal, provider)
                or provider.admin_login_enabled != 1
            ):
                raise ApiError("not_found", "供应商不存在", status_code=404)
            claim_name = str(body.get("claimName") or "groups").strip()
            external_value = str(body.get("externalValue") or "").strip()
            role_code = str(body.get("roleCode") or "").strip()
            if not external_value or not role_code:
                raise ApiError(
                    "invalid_input", "外部值与目标角色不能为空", status_code=400
                )
            if not await _role_exists_for_tenant(db, role_code, provider.tenant_id):
                raise ApiError(
                    "invalid_input", "目标角色在该租户下不存在", status_code=400
                )
            if await db.scalar(
                sa.select(OAuthRoleMapping.id).where(
                    OAuthRoleMapping.provider_id == provider_id,
                    OAuthRoleMapping.claim_name == claim_name,
                    OAuthRoleMapping.external_value == external_value,
                )
            ):
                raise ApiError("duplicate", "同一供应商下的映射已存在", status_code=409)
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
            return json_response({"item": _mapping_payload(mapping, provider)}, status_code=201)
        finally:
            await db.close()

    @app.put("/admin/api/access/mappings/{mapping_id}")
    @api_route
    async def api_access_mappings_update(mapping_id: int, request: Request):
        """修改映射的目标角色或启停状态。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=True
        )
        try:
            body = await read_json_body(request)
            mapping = await db.get(OAuthRoleMapping, mapping_id)
            if mapping is None:
                raise ApiError("not_found", "映射不存在", status_code=404)
            provider = await _mapping_provider(db, principal, mapping)
            if provider is None:
                raise ApiError("not_found", "映射不存在", status_code=404)
            role_code = str(body.get("roleCode") or "").strip()
            if not await _role_exists_for_tenant(db, role_code, provider.tenant_id):
                raise ApiError(
                    "invalid_input", "目标角色在该租户下不存在", status_code=400
                )
            before = {
                "role_code": mapping.role_code,
                "enabled": bool(mapping.enabled),
            }
            mapping.role_code = role_code
            mapping.enabled = 1 if body.get("enabled") else 0
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
            return json_response({"item": _mapping_payload(mapping, provider)})
        finally:
            await db.close()

    @app.delete("/admin/api/access/mappings/{mapping_id}")
    @api_route
    async def api_access_mappings_delete(mapping_id: int, request: Request):
        """删除一条外部角色映射。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=True
        )
        try:
            mapping = await db.get(OAuthRoleMapping, mapping_id)
            if mapping is None:
                raise ApiError("not_found", "映射不存在", status_code=404)
            provider = await _mapping_provider(db, principal, mapping)
            if provider is None:
                raise ApiError("not_found", "映射不存在", status_code=404)
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
            return json_response({"item": {"id": mapping_id}})
        finally:
            await db.close()
