"""独立后台与客户端 API 共用的管理员 Session 解析。"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta

import sqlalchemy as sa
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.models.admin_control import (
    AdminSession,
    SuperAdminAccount,
    SuperAdminSession,
)
from app.models.admin_user import AdminUser
from app.models.base import utcnow
from app.models.oauth import OAuthProvider
from app.models.user import AppUser

ADMIN_SESSION_COOKIE_SECURE = "__Host-wa_admin_session"
ADMIN_CSRF_COOKIE_SECURE = "__Host-wa_admin_csrf"
ADMIN_OAUTH_COOKIE_SECURE = "__Host-wa_admin_oauth_state"
ADMIN_SESSION_COOKIE_INSECURE = "wa_admin_session"
ADMIN_CSRF_COOKIE_INSECURE = "wa_admin_csrf"
ADMIN_OAUTH_COOKIE_INSECURE = "wa_admin_oauth_state"

SUPER_SESSION_COOKIE_SECURE = "__Host-wa_super_admin_session"
SUPER_CSRF_COOKIE_SECURE = "__Host-wa_super_admin_csrf"
SUPER_SESSION_COOKIE_INSECURE = "wa_super_admin_session"
SUPER_CSRF_COOKIE_INSECURE = "wa_super_admin_csrf"


@dataclass(frozen=True)
class AdminPrincipal:
    """一次请求已经验证过的后台主体。"""

    kind: str
    principal_id: int
    user_id: str | None
    tenant_id: str | None
    username: str
    session_id: int
    is_owner: bool = False
    csrf_hash: str = ""

    @property
    def is_platform_super_admin(self) -> bool:
        """平台超级管理员不进入 Casbin。"""
        return self.kind == "platform_super_admin"


def token_hash(token: str) -> str:
    """计算 Session/CSRF Cookie 的 SHA-256。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _naive(value: object) -> datetime:
    """把数据库返回的时间统一成 naive UTC datetime。"""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    return datetime.fromisoformat(str(value)).replace(tzinfo=None)


def _cookie_name(secure: str, insecure: str) -> str:
    """按 Secure 配置返回 Cookie 名。"""
    return secure if settings.admin_session_cookie_secure else insecure


def admin_session_cookie_name() -> str:
    """OAuth 后台管理员 Session Cookie 名。"""
    return _cookie_name(ADMIN_SESSION_COOKIE_SECURE, ADMIN_SESSION_COOKIE_INSECURE)


def admin_csrf_cookie_name() -> str:
    """OAuth 后台管理员 CSRF Cookie 名。"""
    return _cookie_name(ADMIN_CSRF_COOKIE_SECURE, ADMIN_CSRF_COOKIE_INSECURE)


def admin_oauth_cookie_name() -> str:
    """OAuth 登录浏览器绑定 Cookie 名。"""
    return _cookie_name(ADMIN_OAUTH_COOKIE_SECURE, ADMIN_OAUTH_COOKIE_INSECURE)


def super_session_cookie_name() -> str:
    """平台超级管理员 Session Cookie 名。"""
    return _cookie_name(SUPER_SESSION_COOKIE_SECURE, SUPER_SESSION_COOKIE_INSECURE)


def super_csrf_cookie_name() -> str:
    """平台超级管理员 CSRF Cookie 名。"""
    return _cookie_name(SUPER_CSRF_COOKIE_SECURE, SUPER_CSRF_COOKIE_INSECURE)


def request_session_token(request: Request, *, super_admin: bool = False) -> str | None:
    """从请求读取对应后台 Session Cookie。"""
    name = super_session_cookie_name() if super_admin else admin_session_cookie_name()
    return request.cookies.get(name)


def request_csrf_cookie(request: Request, *, super_admin: bool = False) -> str | None:
    """从请求读取对应后台 CSRF Cookie。"""
    name = super_csrf_cookie_name() if super_admin else admin_csrf_cookie_name()
    return request.cookies.get(name)


def has_admin_session_cookie(request: Request) -> bool:
    """请求是否携带任一后台 Session Cookie。"""
    return bool(
        request_session_token(request)
        or request_session_token(request, super_admin=True)
    )


async def resolve_admin_session(
    db: AsyncSession, raw_token: str | None
) -> AdminPrincipal | None:
    """解析 OAuth 管理员 Session，并校验角色同步版本和时效。"""
    if not raw_token:
        return None
    row = (
        await db.execute(
            sa.select(AdminSession, AdminUser, AppUser, OAuthProvider)
            .join(AdminUser, AdminUser.id == AdminSession.admin_user_id)
            .join(AppUser, AppUser.id == AdminUser.user_id)
            .outerjoin(OAuthProvider, OAuthProvider.id == AdminUser.provider_id)
            .where(AdminSession.token_hash == token_hash(raw_token))
        )
    ).first()
    if row is None:
        return None
    session, admin, user, provider = row
    now = utcnow()
    sync_age = (
        (now - _naive(admin.last_role_sync_at)).total_seconds()
        if admin.last_role_sync_at is not None
        else None
    )
    max_sync_age = (
        int(provider.role_sync_max_age_seconds)
        if provider is not None
        else 0
    )
    if (
        session.revoked_at is not None
        or _naive(session.absolute_expires_at) <= now
        or _naive(session.idle_expires_at) <= now
        or session.role_sync_version != admin.version
        or admin.status != "active"
        or user.status != "active"
        or provider is None
        or provider.enabled != 1
        or max_sync_age <= 0
        or sync_age is None
        or sync_age > max_sync_age
    ):
        return None
    session.last_seen_at = now
    session.idle_expires_at = min(
        now + timedelta(minutes=settings.admin_session_idle_minutes),
        _naive(session.absolute_expires_at),
    )
    return AdminPrincipal(
        kind="oauth_admin",
        principal_id=admin.id,
        user_id=user.id,
        tenant_id=user.tenant_id,
        username=user.username,
        session_id=session.id,
        csrf_hash=session.csrf_hash,
    )


async def resolve_super_admin_session(
    db: AsyncSession, raw_token: str | None
) -> AdminPrincipal | None:
    """解析平台超级管理员 Session，并检查密码版本和过期宽限。"""
    if not raw_token:
        return None
    row = (
        await db.execute(
            sa.select(SuperAdminSession, SuperAdminAccount)
            .join(
                SuperAdminAccount,
                SuperAdminAccount.id == SuperAdminSession.account_id,
            )
            .where(SuperAdminSession.token_hash == token_hash(raw_token))
        )
    ).first()
    if row is None:
        return None
    session, account = row
    now = utcnow()
    grace_end = (
        _naive(account.password_expires_at)
        + timedelta(hours=settings.super_admin_rotation_grace_hours)
        if account.password_expires_at is not None
        else now
    )
    if (
        session.revoked_at is not None
        or _naive(session.absolute_expires_at) <= now
        or _naive(session.idle_expires_at) <= now
        or session.password_version != account.password_version
        or account.status != "active"
        or grace_end <= now
    ):
        return None
    session.idle_expires_at = min(
        now + timedelta(minutes=settings.admin_session_idle_minutes),
        _naive(session.absolute_expires_at),
    )
    return AdminPrincipal(
        kind="platform_super_admin",
        principal_id=account.id,
        user_id=None,
        tenant_id=None,
        username=account.username,
        session_id=session.id,
        is_owner=account.owner_slot == "superAdmin",
        csrf_hash=session.csrf_hash,
    )


async def resolve_admin_request(
    db: AsyncSession, request: Request
) -> AdminPrincipal | None:
    """解析两条后台身份链；冲突时撤销双方并要求重新认证。"""
    oauth = await resolve_admin_session(
        db, request_session_token(request)
    )
    super_admin = await resolve_super_admin_session(
        db, request_session_token(request, super_admin=True)
    )
    if oauth and super_admin:
        now = utcnow()
        await db.execute(
            sa.update(AdminSession)
            .where(AdminSession.id == oauth.session_id)
            .values(revoked_at=now)
        )
        await db.execute(
            sa.update(SuperAdminSession)
            .where(SuperAdminSession.id == super_admin.session_id)
            .values(revoked_at=now)
        )
        await db.commit()
        return None
    return oauth or super_admin


async def verify_admin_csrf(
    principal: AdminPrincipal, raw_token: str | None
) -> bool:
    """校验原始 CSRF token 与解析后的 Session 摘要一致。"""
    if not raw_token or not principal.csrf_hash:
        return False
    return hmac.compare_digest(
        token_hash(raw_token),
        principal.csrf_hash,
    )
