"""管理端登录、登出与 OAuth 登录事务的路由。"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from datetime import timedelta
from typing import Any

import sqlalchemy as sa
from ludic.web import Request
from starlette.responses import JSONResponse, RedirectResponse

from app.config.settings import settings
from app.core.crypto import decrypt, encrypt
from app.core.oidc import (
    OIDCError, exchange_code, resolve_provider_endpoints, verify_id_token,
)
from app.models.admin_control import AdminSession, SuperAdminSession
from app.models.base import utcnow
from app.models.oauth import OAuthLoginAttempt, OAuthProvider
from services.admin_web.accounts import (
    AdminPrincipal, LoginError, authenticate_super_admin, create_admin_session,
    create_super_admin_session,
)
from services.admin_web.audit import audit_principal_action, record_admin_audit
from services.admin_web.common import (
    append_query, callback_uri, login_redirect, origin_ok, principal_of,
    require_csrf, safe_next,
)
from services.admin_web.db import AdminSessionLocal
from services.admin_web.oauth_admin import (
    provider_for_admin_login, provisioning_error_code, sync_oauth_admin,
)
from services.admin_web.rbac import casbin_service
from services.admin_web.security import (
    admin_oauth_cookie_name, clear_admin_cookies, clear_oauth_binding_cookie,
    clear_super_admin_cookies, request_csrf_cookie, set_admin_session_cookies,
    set_oauth_binding_cookie, set_super_session_cookies, token_hash,
)
from services.admin_web.views import login_page

logger = logging.getLogger(__name__)


def register(app: Any) -> None:
    """把登录、登出与 OAuth 登录相关的路由挂到 app 上。"""

    @app.get("/admin/health")
    async def health():
        """admin-web 存活与数据库可用性检查。"""
        try:
            async with AdminSessionLocal() as db:
                await db.scalar(sa.text("SELECT 1"))
        except Exception:  # noqa: BLE001 - 健康检查只返回稳定状态
            logger.exception("admin-web 数据库健康检查失败")
            return JSONResponse(
                {"status": "unavailable", "service": "admin-web"},
                status_code=503,
            )
        return JSONResponse({"status": "ok", "service": "admin-web"})

    @app.get("/admin/login")
    async def login_get(request: Request):
        """渲染管理员登录页。"""
        async with AdminSessionLocal() as db:
            existing = await principal_of(db, request)
            if existing is not None:
                return RedirectResponse("/admin/", status_code=303)
            providers = list(
                await db.scalars(
                    sa.select(OAuthProvider)
                    .where(
                        OAuthProvider.tenant_id == settings.auth_default_tenant,
                        OAuthProvider.enabled == 1,
                        OAuthProvider.admin_login_enabled == 1,
                    )
                    .order_by(OAuthProvider.code)
                )
            )
        error = request.query_params.get("error")
        notice = request.query_params.get("notice")
        message = notice or error
        return login_page(
            providers=providers,
            flash=message,
            flash_kind="warn" if error else "info",
        )

    @app.post("/admin/login/super-admin")
    async def super_admin_login(request: Request):
        """平台超级管理员用户名密码登录。"""
        if not origin_ok(request):
            return login_redirect("invalid_origin")
        form = await request.form()
        username = str(form.get("username") or "").strip()
        password = str(form.get("password") or "")
        next_url = safe_next(str(form.get("next") or "/admin/"))
        async with AdminSessionLocal() as db:
            try:
                account = await authenticate_super_admin(
                    db, username=username, password=password
                )
            except LoginError as exc:
                await record_admin_audit(
                    db,
                    actor_kind="anonymous",
                    actor_id=None,
                    actor_username=username,
                    action="platform_admin.login",
                    target_type="super_admin_account",
                    target_id=username,
                    outcome="denied",
                    detail={"code": str(exc)},
                )
                await db.commit()
                return login_redirect(str(exc))
            await audit_principal_action(
                db,
                AdminPrincipal(
                    kind="platform_super_admin",
                    principal_id=account.id,
                    user_id=None,
                    tenant_id=None,
                    username=account.username,
                    session_id=0,
                    is_owner=account.owner_slot == "superAdmin",
                ),
                action="platform_admin.login",
                target_type="super_admin_account",
                target_id=str(account.id),
            )
            token, csrf = await create_super_admin_session(db, account)
            await db.commit()
        response = RedirectResponse(next_url, status_code=303)
        clear_admin_cookies(response)
        set_super_session_cookies(response, token, csrf)
        return response

    @app.post("/admin/login/oauth/{provider_code}")
    async def admin_oauth_start(provider_code: str, request: Request):
        """创建一次性 OAuth 登录事务并跳转 IdP。"""
        if not origin_ok(request):
            return login_redirect("invalid_origin")
        async with AdminSessionLocal() as db:
            provider = await provider_for_admin_login(db, provider_code)
            if provider is None:
                return login_redirect("provider_not_found")
            try:
                endpoints = await resolve_provider_endpoints(provider)
            except OIDCError as exc:
                return login_redirect(str(exc))
            state = secrets.token_urlsafe(32)
            nonce = secrets.token_urlsafe(32)
            binding = secrets.token_urlsafe(32)
            verifier = secrets.token_urlsafe(64)
            challenge = base64.urlsafe_b64encode(
                hashlib.sha256(verifier.encode("ascii")).digest()
            ).rstrip(b"=").decode("ascii")
            db.add(
                OAuthLoginAttempt(
                    tenant_id=provider.tenant_id,
                    provider_id=provider.id,
                    state_hash=token_hash(state),
                    browser_binding_hash=token_hash(binding),
                    nonce_hash=token_hash(nonce),
                    code_verifier_cipher=encrypt(verifier),
                    return_to="/admin/",
                    expires_at=utcnow()
                    + timedelta(seconds=settings.oauth_state_ttl_seconds),
                )
            )
            await db.commit()
            scopes = provider.scopes.split()
            if "openid" not in scopes:
                scopes.insert(0, "openid")
            url = append_query(
                endpoints.authorization_endpoint,
                response_type="code",
                client_id=provider.client_id,
                redirect_uri=callback_uri(provider.code),
                scope=" ".join(scopes),
                state=state,
                nonce=nonce,
                code_challenge=challenge,
                code_challenge_method="S256",
            )
        response = RedirectResponse(url, status_code=303)
        set_oauth_binding_cookie(response, binding)
        return response

    @app.get("/admin/auth/oauth/{provider_code}/callback")
    async def admin_oauth_callback(
        provider_code: str,
        request: Request,
        state: str | None = None,
        code: str | None = None,
        error: str | None = None,
    ):
        """验证 OAuth、同步 Casbin role 并建立管理员 Session。"""
        if not state:
            return login_redirect("state_missing")
        async with AdminSessionLocal() as db:
            provider = await provider_for_admin_login(db, provider_code)
            if provider is None:
                return login_redirect("provider_not_found")
            attempt = await db.scalar(
                sa.select(OAuthLoginAttempt).where(
                    OAuthLoginAttempt.state_hash == token_hash(state),
                    OAuthLoginAttempt.provider_id == provider.id,
                )
            )
            if attempt is None:
                return login_redirect("state_invalid")
            binding = request.cookies.get(admin_oauth_cookie_name(), "")
            if not binding or token_hash(binding) != attempt.browser_binding_hash:
                return login_redirect("login_csrf")
            if attempt.consumed_at is not None or attempt.expires_at <= utcnow():
                return login_redirect("state_expired")
            consumed = await db.execute(
                sa.update(OAuthLoginAttempt)
                .where(
                    OAuthLoginAttempt.id == attempt.id,
                    OAuthLoginAttempt.consumed_at.is_(None),
                    OAuthLoginAttempt.expires_at > utcnow(),
                )
                .values(consumed_at=utcnow())
            )
            await db.commit()
            if not consumed.rowcount:
                return login_redirect("state_replayed")
            if error:
                return login_redirect("provider_error")
            if not code:
                return login_redirect("code_missing")
            try:
                endpoints = await resolve_provider_endpoints(provider)
                id_token = await exchange_code(
                    provider,
                    endpoints,
                    code=code,
                    redirect_uri=callback_uri(provider.code),
                    code_verifier=decrypt(attempt.code_verifier_cipher),
                )
                claims = await verify_id_token(
                    provider,
                    endpoints,
                    id_token,
                    expected_nonce_hash=attempt.nonce_hash,
                )
                admin = await sync_oauth_admin(db, provider, claims)
                if admin is None:
                    return login_redirect("admin_role_required")
                roles = [
                    row.role
                    for row in await casbin_service.role_rows(
                        db,
                        user_id=admin.user_id,
                        tenant_id=admin.tenant_id,
                    )
                ]
                await record_admin_audit(
                    db,
                    actor_kind="oauth_admin",
                    actor_id=admin.id,
                    actor_username=str(
                        claims.get(provider.username_claim) or admin.user_id
                    ),
                    tenant_id=admin.tenant_id,
                    action="oauth_admin.role_sync",
                    target_type="admin_user",
                    target_id=str(admin.id),
                    detail={"provider_id": provider.id, "roles": roles},
                )
                token, csrf = await create_admin_session(
                    db,
                    admin_user_id=admin.id,
                    role_sync_version=admin.version,
                )
                await db.commit()
            except Exception as exc:  # noqa: BLE001
                logger.warning("OAuth admin login failed: %s", type(exc).__name__)
                return login_redirect(provisioning_error_code(exc))
        response = RedirectResponse("/admin/", status_code=303)
        clear_oauth_binding_cookie(response)
        clear_super_admin_cookies(response)
        set_admin_session_cookies(response, token, csrf)
        return response

    @app.post("/admin/logout")
    async def logout(request: Request):
        """退出当前管理员 Session。"""
        if not origin_ok(request):
            return RedirectResponse("/admin/login?error=invalid_origin", status_code=303)
        async with AdminSessionLocal() as db:
            principal = await principal_of(db, request)
            if principal is not None:
                form = await request.form()
                if await require_csrf(db, request, principal, form):
                    await audit_principal_action(
                        db,
                        principal,
                        action="admin.logout",
                        target_type="admin_session",
                        target_id=str(principal.session_id),
                    )
                    if principal.is_platform_super_admin:
                        await db.execute(
                            sa.update(SuperAdminSession)
                            .where(
                                SuperAdminSession.id == principal.session_id
                            )
                            .values(revoked_at=utcnow())
                        )
                    else:
                        await db.execute(
                            sa.update(AdminSession)
                            .where(AdminSession.id == principal.session_id)
                            .values(revoked_at=utcnow())
                        )
                    await db.commit()
        response = RedirectResponse("/admin/login", status_code=303)
        clear_admin_cookies(response)
        clear_super_admin_cookies(response)
        return response
