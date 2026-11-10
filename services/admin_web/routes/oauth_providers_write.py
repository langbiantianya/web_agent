"""OAuth Provider 的写操作（更新 / 启停 / 删除）。

从 ``oauth_providers.py`` 拆出来：单文件行数上限是 350 行。拆分只切
代码位置，路由的路径、方法与注册顺序都没变。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import sqlalchemy as sa
from ludic.web import Request
from pydantic import ValidationError
from starlette.responses import RedirectResponse

from app.repo.oauth_provider_repo import oauth_provider_repo
from app.schemas.oauth_provider import OAuthProviderUpdate
from services.admin_web.audit import audit_principal_action
from services.admin_web.common import (
    as_bool, business_redirect, not_found, operation_authorized, tenant_id_of,
    unauthorized, with_auth,
)
from services.admin_web.form_errors import _oauth_field_errors
from services.admin_web.oauth_forms import _provider_form_values, _submitted_values
from services.admin_web.form_state import invalid_form_redirect


def register(app: Any) -> None:
    """挂载写操作路由。"""

    @app.post("/admin/oauth-providers/{code}/update")
    async def oauth_providers_update(code: str, request: Request):
        """更新 Provider 地址、claims、登录用途和 client secret。"""
        values = await with_auth(request)
        if values is None:
            return unauthorized()
        db, principal, _, _ = values
        try:
            form = await request.form()
            if not await operation_authorized(
                request,
                db,
                principal,
                page_code="oauth_providers",
                component_code="provider_editor",
                form=form,
            ):
                return not_found()
            tenant_id = tenant_id_of(principal)
            row = await oauth_provider_repo.get(db, code, tenant_id)
            if row is None:
                return business_redirect(
                    "/admin/oauth-providers", error="missing"
                )
            try:
                payload = OAuthProviderUpdate(**_provider_form_values(form))
            except (TypeError, ValueError, ValidationError) as exc:
                # 校验失败：回到编辑面板并逐字段标错（UX-03 + UX-09）。
                submitted = _submitted_values(form)
                return RedirectResponse(
                    invalid_form_redirect(
                        "/admin/oauth-providers?resource="
                        + quote(code, safe=""),
                        submitted,
                        _oauth_field_errors(exc),
                    ),
                    status_code=303,
                )
            fields = payload.model_dump(exclude_unset=True)
            fields["scopes"] = " ".join(payload.scopes or [])
            for key in (
                "auto_create",
                "role_claim_required",
                "client_login_enabled",
                "admin_login_enabled",
                "enabled",
            ):
                fields[key] = int(fields[key])
            secret_kwargs: dict = {}
            if as_bool(form.get("clear_client_secret")):
                secret_kwargs["client_secret"] = ""
            else:
                replacement = str(form.get("client_secret") or "")
                if replacement:
                    secret_kwargs["client_secret"] = replacement
            await oauth_provider_repo.update(
                db,
                row,
                **secret_kwargs,
                **fields,
            )
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
            return business_redirect(
                "/admin/oauth-providers", notice="updated"
            )
        finally:
            await db.close()

    @app.post("/admin/oauth-providers/{code}/toggle")
    async def oauth_providers_toggle(code: str, request: Request):
        """切换 Provider 总开关，不修改客户端/管理员用途开关。"""
        values = await with_auth(request)
        if values is None:
            return unauthorized()
        db, principal, _, _ = values
        try:
            form = await request.form()
            if not await operation_authorized(
                request,
                db,
                principal,
                page_code="oauth_providers",
                component_code="provider_editor",
                form=form,
            ):
                return not_found()
            tenant_id = tenant_id_of(principal)
            row = await oauth_provider_repo.get(db, code, tenant_id)
            if row is None:
                return business_redirect(
                    "/admin/oauth-providers", error="missing"
                )
            await oauth_provider_repo.update(
                db,
                row,
                enabled=1 if as_bool(form.get("enabled")) else 0,
            )
            await audit_principal_action(
                db,
                principal,
                action="oauth_provider.toggle",
                target_type="oauth_provider",
                target_id=code,
                tenant_id=tenant_id,
                detail={"enabled": bool(row.enabled)},
            )
            await db.commit()
            return business_redirect(
                "/admin/oauth-providers", notice="toggled"
            )
        finally:
            await db.close()

    @app.post("/admin/oauth-providers/{code}/delete")
    async def oauth_providers_delete(code: str, request: Request):
        """删除尚未绑定身份或管理员角色的 OAuth Provider。"""
        values = await with_auth(request)
        if values is None:
            return unauthorized()
        db, principal, _, _ = values
        try:
            form = await request.form()
            if not await operation_authorized(
                request,
                db,
                principal,
                page_code="oauth_providers",
                component_code="provider_editor",
                form=form,
            ):
                return not_found()
            tenant_id = tenant_id_of(principal)
            row = await oauth_provider_repo.get(db, code, tenant_id)
            if row is None:
                return business_redirect(
                    "/admin/oauth-providers", error="missing"
                )
            try:
                await oauth_provider_repo.delete(db, row)
            except sa.exc.IntegrityError:
                await db.rollback()
                return business_redirect(
                    "/admin/oauth-providers", error="failed"
                )
            await audit_principal_action(
                db,
                principal,
                action="oauth_provider.delete",
                target_type="oauth_provider",
                target_id=code,
                tenant_id=tenant_id,
            )
            await db.commit()
            return business_redirect(
                "/admin/oauth-providers", notice="deleted"
            )
        finally:
            await db.close()
