"""终端用户 OIDC 登录、服务端会话与 SSE ticket。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import timedelta
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import sqlalchemy as sa
from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse

from app.api.deps import CurrentUserDep, DbSession
from app.config.settings import settings
from app.core.crypto import decrypt, encrypt
from app.core.errors import BadRequestError, NotFoundError
from app.core.oidc import (
    OIDCError,
    exchange_code,
    resolve_provider_endpoints,
    verify_id_token,
)
from app.core.ticket import issue_ticket
from app.core.user_accounts import UserProvisionError, provision_oidc_user
from app.core.user_auth import (
    clear_oauth_state_cookie,
    clear_user_cookies,
    create_user_session,
    oauth_state_cookie_name,
    revoke_all_user_sessions,
    revoke_session,
    set_oauth_state_cookie,
    set_user_cookies,
    token_hash,
)
from app.models.base import utcnow
from app.models.oauth import OAuthLoginAttempt
from app.repo.oauth_provider_repo import oauth_provider_repo
from app.repo.run_repo import run_repo
from app.schemas.auth import (
    AuthProviderOut,
    LogoutOut,
    OAuthStartIn,
    OAuthStartOut,
    SSETicketIn,
    SSETicketOut,
    UserOut,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _safe_return_to(value: str) -> str:
    """只接受站内绝对路径，阻断开放重定向。"""
    parsed = urlsplit(value)
    if (
        not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(ord(ch) < 32 for ch in value)
        or parsed.scheme
        or parsed.netloc
    ):
        raise BadRequestError("return_to 必须是站内相对路径")
    return value


def _callback_uri(request: Request, provider_code: str) -> str:
    """构造固定 redirect_uri，优先使用显式配置的公开根地址。"""
    base = settings.oauth_public_base_url.rstrip("/")
    if not base and settings.user_session_cookie_secure:
        raise BadRequestError(
            "生产登录必须配置 OAUTH_PUBLIC_BASE_URL，不能从 Host 头推导回调地址"
        )
    if not base:
        base = str(request.base_url).rstrip("/")
    return f"{base}/api/auth/oauth/{quote(provider_code, safe='')}/callback"


def _append_query(url: str, **params: str | None) -> str:
    """在保留既有 query 的前提下追加参数。"""
    parsed = urlsplit(url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.extend((key, value) for key, value in params.items() if value is not None)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def _login_error_redirect(return_to: str, code: str) -> RedirectResponse:
    """回调失败只暴露稳定错误码，不携带 provider 原文或 token。"""
    response = RedirectResponse(
        _append_query(return_to, auth_error=code),
        status_code=303,
    )
    clear_oauth_state_cookie(response)
    return response


async def _enabled_provider(db, code: str):
    """读取默认租户中启用的登录供应商。"""
    provider = await oauth_provider_repo.get(db, code, settings.auth_default_tenant)
    if provider is None or provider.enabled != 1:
        raise NotFoundError("登录供应商不存在")
    return provider


@router.get("/providers", response_model=list[AuthProviderOut])
async def list_login_providers(db: DbSession) -> list[AuthProviderOut]:
    """列出可公开选择的登录供应商，不返回 issuer、client id 或 secret。"""
    rows = await oauth_provider_repo.list(
        db, settings.auth_default_tenant, enabled_only=True
    )
    return [
        AuthProviderOut(code=row.code, display_name=row.display_name, kind=row.kind)
        for row in rows
    ]


@router.post("/oauth/{provider_code}/start", response_model=OAuthStartOut)
async def start_oauth(
    provider_code: str,
    payload: OAuthStartIn,
    request: Request,
    response: Response,
    db: DbSession,
) -> OAuthStartOut:
    """创建一次性登录事务并返回 OIDC authorization URL。"""
    provider = await _enabled_provider(db, provider_code)
    return_to = _safe_return_to(payload.return_to)
    try:
        endpoints = await resolve_provider_endpoints(provider)
    except OIDCError as exc:
        raise BadRequestError(
            "登录供应商暂时不可用", data={"code": str(exc)}
        ) from exc

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    binding = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    db.add(
        OAuthLoginAttempt(
            tenant_id=provider.tenant_id,
            provider_id=provider.id,
            state_hash=token_hash(state),
            browser_binding_hash=token_hash(binding),
            nonce_hash=token_hash(nonce),
            code_verifier_cipher=encrypt(code_verifier),
            return_to=return_to,
            expires_at=utcnow()
            + timedelta(seconds=settings.oauth_state_ttl_seconds),
        )
    )
    await db.commit()

    scopes = provider.scopes.split()
    if "openid" not in scopes:
        scopes.insert(0, "openid")
    authorization_url = _append_query(
        endpoints.authorization_endpoint,
        response_type="code",
        client_id=provider.client_id,
        redirect_uri=_callback_uri(request, provider.code),
        scope=" ".join(scopes),
        state=state,
        nonce=nonce,
        code_challenge=challenge,
        code_challenge_method="S256",
    )
    set_oauth_state_cookie(response, binding)
    return OAuthStartOut(authorization_url=authorization_url)


@router.get("/oauth/{provider_code}/callback")
async def oauth_callback(
    provider_code: str,
    request: Request,
    db: DbSession,
    state: str | None = None,
    code: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    """验证 state/binding/PKCE/ID Token，建立本地用户会话。"""
    provider = await _enabled_provider(db, provider_code)
    if not state:
        return _login_error_redirect("/", "state_missing")

    attempt = await db.scalar(
        sa.select(OAuthLoginAttempt).where(
            OAuthLoginAttempt.state_hash == token_hash(state),
            OAuthLoginAttempt.provider_id == provider.id,
        )
    )
    if attempt is None:
        return _login_error_redirect("/", "state_invalid")
    return_to = _safe_return_to(str(attempt.return_to))
    if error:
        return _login_error_redirect(return_to, "provider_error")
    if not code:
        return _login_error_redirect(return_to, "code_missing")

    binding = request.cookies.get(oauth_state_cookie_name(), "")
    if not binding or not hmac.compare_digest(
        token_hash(binding), attempt.browser_binding_hash
    ):
        return _login_error_redirect(return_to, "login_csrf")
    if attempt.consumed_at is not None or attempt.expires_at <= utcnow():
        return _login_error_redirect(return_to, "state_expired")

    # 条件消费必须在任何网络调用之前完成：并发重放只能有一个请求成功。
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
        return _login_error_redirect(return_to, "state_replayed")

    try:
        endpoints = await resolve_provider_endpoints(provider)
        id_token = await exchange_code(
            provider,
            endpoints,
            code=code,
            redirect_uri=_callback_uri(request, provider.code),
            code_verifier=decrypt(attempt.code_verifier_cipher),
        )
        claims = await verify_id_token(
            provider,
            endpoints,
            id_token,
            expected_nonce_hash=attempt.nonce_hash,
        )
        provisioned = await provision_oidc_user(db, provider, claims)
        if provisioned.user.status != "active":
            raise UserProvisionError("account_disabled")
        token, csrf_token = await create_user_session(db, provisioned.user.id)
        await db.commit()
    except (OIDCError, UserProvisionError, ValueError) as exc:
        return _login_error_redirect(return_to, str(exc) or "login_failed")
    except Exception:
        # 不把 token endpoint 正文、SQL 或 stack 写到浏览器。
        return _login_error_redirect(return_to, "login_failed")

    response = RedirectResponse(return_to, status_code=303)
    clear_oauth_state_cookie(response)
    set_user_cookies(response, token, csrf_token)
    return response


@router.get("/me", response_model=UserOut)
async def get_me(user: CurrentUserDep) -> UserOut:
    """返回当前服务端会话绑定的本地用户资料。"""
    return UserOut(
        username=user.username,
        phone=user.phone,
        phone_verified=user.phone_verified,
        email=user.email,
        email_verified=user.email_verified,
        status=user.status,
    )


@router.post("/logout", response_model=LogoutOut)
async def logout(response: Response, user: CurrentUserDep, db: DbSession) -> LogoutOut:
    """撤销当前服务端会话并清除浏览器 Cookie。"""
    await revoke_session(db, user.session_id)
    clear_user_cookies(response)
    return LogoutOut(revoked_sessions=1)


@router.post("/logout-all", response_model=LogoutOut)
async def logout_all(
    response: Response, user: CurrentUserDep, db: DbSession
) -> LogoutOut:
    """撤销当前用户在所有浏览器上的会话。"""
    count = await revoke_all_user_sessions(db, user.user_id)
    clear_user_cookies(response)
    return LogoutOut(revoked_sessions=count)


@router.post("/sse-ticket", response_model=SSETicketOut)
async def create_sse_ticket(
    payload: SSETicketIn,
    user: CurrentUserDep,
    db: DbSession,
) -> SSETicketOut:
    """为当前用户拥有的 run 签发短期、不可跨用户使用的 SSE ticket。"""
    run = await run_repo.get_for_user(
        db, payload.run_id, user.tenant_id, user.user_id
    )
    if run is None:
        raise NotFoundError("run 不存在")
    ticket, ttl = issue_ticket(
        user_id=user.user_id,
        tenant_id=user.tenant_id,
        run_id=payload.run_id,
    )
    return SSETicketOut(ticket=ticket, expires_in=ttl)
