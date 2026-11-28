"""OAuth Provider 的管理面 JSON API（V4.10 阶段 E-1）。

Ludic 视图层下线前的最后一块：Provider 配置页迁到 SvelteKit，这一组
端点就是它的读写来源。

**client_secret 永远是只写字段**：读取只回 ``hasClientSecret``；写入时
不传表示保持原值，传空串表示清除。密钥**任何时候都不回显**——包括
校验失败时。
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from ludic.web import Request
from pydantic import ValidationError

from app.repo.oauth_provider_repo import oauth_provider_repo
from app.schemas.oauth_provider import OAuthProviderCreate, OAuthProviderUpdate
from services.admin_web.api_support import (
    ApiError,
    api_route,
    authorize,
    json_response,
    read_json_body,
    tenant_of,
)
from services.admin_web.audit import audit_principal_action
from services.admin_web.form_errors import field_errors

PAGE_CODE = "oauth_providers"
COMPONENT_CODE = "provider_editor"

#: 管理面 API 统一 camelCase，pydantic schema 用 snake_case。
_FIELD_ALIASES = {
    "displayName": "display_name",
    "discoveryUrl": "discovery_url",
    "authorizationEndpoint": "authorization_endpoint",
    "tokenEndpoint": "token_endpoint",
    "jwksUri": "jwks_uri",
    "clientId": "client_id",
    "clientSecret": "client_secret",
    "tokenAuthMethod": "token_auth_method",
    "usernameClaim": "username_claim",
    "roleClaim": "role_claim",
    "roleClaimRequired": "role_claim_required",
    "roleSyncMaxAgeSeconds": "role_sync_max_age_seconds",
    "autoCreate": "auto_create",
    "clientLoginEnabled": "client_login_enabled",
    "adminLoginEnabled": "admin_login_enabled",
    "enabled": "enabled",
    "code": "code",
    "scopes": "scopes",
}

#: 布尔字段落库是 int。
_BOOL_FIELDS = (
    "auto_create",
    "role_claim_required",
    "client_login_enabled",
    "admin_login_enabled",
    "enabled",
)


def provider_payload(row: Any) -> dict[str, Any]:
    """把 Provider 行整理成前端形状。

    **密钥不回显**：只给一个 ``hasClientSecret`` 布尔值。
    """
    return {
        "code": row.code,
        "displayName": row.display_name,
        "issuer": row.issuer,
        "discoveryUrl": row.discovery_url or "",
        "authorizationEndpoint": row.authorization_endpoint or "",
        "tokenEndpoint": row.token_endpoint or "",
        "jwksUri": row.jwks_uri or "",
        "clientId": row.client_id,
        "hasClientSecret": bool(row.client_secret_cipher),
        "tokenAuthMethod": row.token_auth_method,
        "scopes": (row.scopes or "").split(),
        "usernameClaim": row.username_claim,
        "roleClaim": row.role_claim,
        "roleClaimRequired": bool(row.role_claim_required),
        "roleSyncMaxAgeSeconds": int(row.role_sync_max_age_seconds),
        "autoCreate": bool(row.auto_create),
        "clientLoginEnabled": bool(row.client_login_enabled),
        "adminLoginEnabled": bool(row.admin_login_enabled),
        "enabled": bool(row.enabled),
    }


def _to_snake(body: dict[str, Any]) -> dict[str, Any]:
    """把 camelCase 的请求体翻译成 schema 认得的字段名。"""
    out: dict[str, Any] = {}
    for key, value in body.items():
        name = _FIELD_ALIASES.get(key, key)
        if name == "scopes" and isinstance(value, str):
            value = value.split()
        out[name] = value
    return out


def _field_errors(exc: Exception) -> dict[str, str]:
    """把校验错误摊平成字段级文案，并把字段名翻回 camelCase。"""
    errors = field_errors(exc) if isinstance(exc, ValidationError) else {}
    if not errors:
        return {"displayName": "The submitted values are invalid."}
    reverse = {value: key for key, value in _FIELD_ALIASES.items()}
    return {reverse.get(name, name): text for name, text in errors.items()}


def _stored_fields(
    payload: Any,
    *,
    exclude: tuple[str, ...] = (),
    only_set: bool = False,
) -> dict[str, Any]:
    """把 schema 的取值整成能落库的形状。

    ``only_set=True`` 用于**局部更新**：没传的字段不动。创建时必须带上
    全部字段（``only_set=False``），否则没传的字段会落成数据库列的默认值
    ——而默认值未必是 schema 里写的那一个。
    """
    fields = payload.model_dump(
        exclude=exclude, exclude_unset=only_set
    )
    if "scopes" in fields:
        fields["scopes"] = " ".join(fields["scopes"] or [])
    for key in _BOOL_FIELDS:
        if key in fields:
            fields[key] = int(fields[key])
    return fields


def register(app: Any) -> None:
    """把 OAuth Provider 的管理面 JSON API 挂到 app 上。"""

    @app.get("/admin/api/oauth-providers")
    @api_route
    async def api_oauth_providers_list(request: Request):
        """列出当前租户的 OAuth Provider。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=False
        )
        try:
            rows = await oauth_provider_repo.list(db, tenant_of(principal))
            return json_response(
                {
                    "items": [provider_payload(row) for row in rows],
                    "total": len(rows),
                }
            )
        finally:
            await db.close()

    @app.post("/admin/api/oauth-providers")
    @api_route
    async def api_oauth_providers_create(request: Request):
        """新建一个 OAuth Provider。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=True
        )
        try:
            body = await read_json_body(request)
            try:
                payload = OAuthProviderCreate(**_to_snake(body))
            except (TypeError, ValueError, ValidationError) as exc:
                raise ApiError(
                    "invalid_input",
                    "提交的供应商配置不合法",
                    status_code=400,
                    field_errors=_field_errors(exc),
                ) from exc
            tenant_id = tenant_of(principal)
            if await oauth_provider_repo.get(db, payload.code, tenant_id):
                raise ApiError(
                    "duplicate", "该供应商 code 已存在", status_code=409
                )
            stored = _stored_fields(payload, exclude=("client_secret", "code"))
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
            return json_response({"item": provider_payload(row)}, status_code=201)
        finally:
            await db.close()

    @app.put("/admin/api/oauth-providers/{code}")
    @api_route
    async def api_oauth_providers_update(code: str, request: Request):
        """局部更新一个 OAuth Provider。

        不传 ``clientSecret`` 表示保持原密钥；传空串表示**清除**密钥。
        """
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=True
        )
        try:
            body = await read_json_body(request)
            tenant_id = tenant_of(principal)
            row = await oauth_provider_repo.get(db, code, tenant_id)
            if row is None:
                raise ApiError("not_found", "供应商不存在", status_code=404)
            snake = _to_snake(body)
            secret = snake.pop("client_secret", None)
            try:
                payload = OAuthProviderUpdate(**snake)
            except (TypeError, ValueError, ValidationError) as exc:
                raise ApiError(
                    "invalid_input",
                    "提交的供应商配置不合法",
                    status_code=400,
                    field_errors=_field_errors(exc),
                ) from exc
            fields = _stored_fields(payload, only_set=True)
            secret_kwargs: dict[str, Any] = {}
            if secret is not None:
                # 空串 = 清除；非空 = 覆盖。不传 = 保持原值。
                secret_kwargs["client_secret"] = secret
            await oauth_provider_repo.update(db, row, **secret_kwargs, **fields)
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
            return json_response({"item": provider_payload(row)})
        finally:
            await db.close()

    @app.post("/admin/api/oauth-providers/{code}/toggle")
    @api_route
    async def api_oauth_providers_toggle(code: str, request: Request):
        """切换 Provider 的总开关。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=True
        )
        try:
            body = await read_json_body(request)
            tenant_id = tenant_of(principal)
            row = await oauth_provider_repo.get(db, code, tenant_id)
            if row is None:
                raise ApiError("not_found", "供应商不存在", status_code=404)
            enabled = bool(body.get("enabled", True))
            await oauth_provider_repo.update(db, row, enabled=int(enabled))
            await audit_principal_action(
                db,
                principal,
                action="oauth_provider.toggle",
                target_type="oauth_provider",
                target_id=code,
                tenant_id=tenant_id,
                detail={"enabled": enabled},
            )
            await db.commit()
            return json_response({"item": provider_payload(row)})
        finally:
            await db.close()

    @app.delete("/admin/api/oauth-providers/{code}")
    @api_route
    async def api_oauth_providers_delete(code: str, request: Request):
        """删除一个尚未绑定身份或管理员角色的 OAuth Provider。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=True
        )
        try:
            tenant_id = tenant_of(principal)
            row = await oauth_provider_repo.get(db, code, tenant_id)
            if row is None:
                raise ApiError("not_found", "供应商不存在", status_code=404)
            try:
                await oauth_provider_repo.delete(db, row)
            except sa.exc.IntegrityError as exc:
                await db.rollback()
                raise ApiError(
                    "in_use", "该供应商已被引用，无法删除", status_code=409
                ) from exc
            await audit_principal_action(
                db,
                principal,
                action="oauth_provider.delete",
                target_type="oauth_provider",
                target_id=code,
                tenant_id=tenant_id,
            )
            await db.commit()
            return json_response({"ok": True})
        finally:
            await db.close()
