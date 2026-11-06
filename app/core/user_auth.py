"""终端用户会话、Cookie 与 CSRF 的公共实现。"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

import sqlalchemy as sa
from fastapi import Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.models.base import utcnow
from app.models.user import AppUser, UserSession

SESSION_COOKIE_SECURE = "__Host-wa_session"
SESSION_COOKIE_INSECURE = "wa_session"
CSRF_COOKIE_SECURE = "__Host-wa_csrf"
CSRF_COOKIE_INSECURE = "wa_csrf"
OAUTH_STATE_COOKIE_SECURE = "__Host-wa_oauth_state"
OAUTH_STATE_COOKIE_INSECURE = "wa_oauth_state"


@dataclass(frozen=True)
class UserPrincipal:
    """一次请求已经验证过的终端用户身份。"""

    user_id: str
    tenant_id: str
    username: str
    phone: str | None
    phone_verified: bool | None
    email: str | None
    email_verified: bool | None
    status: str
    session_id: int
    csrf_hash: str


def token_hash(token: str) -> str:
    """计算 Cookie token 的单向摘要；数据库不保存原值。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def session_cookie_name() -> str:
    """返回符合当前 Secure 配置的 session Cookie 名。"""
    return (
        SESSION_COOKIE_SECURE
        if settings.user_session_cookie_secure
        else SESSION_COOKIE_INSECURE
    )


def csrf_cookie_name() -> str:
    """返回符合当前 Secure 配置的 CSRF Cookie 名。"""
    return (
        CSRF_COOKIE_SECURE
        if settings.user_session_cookie_secure
        else CSRF_COOKIE_INSECURE
    )


def oauth_state_cookie_name() -> str:
    """返回 OAuth 浏览器绑定 Cookie 名。"""
    return (
        OAUTH_STATE_COOKIE_SECURE
        if settings.user_session_cookie_secure
        else OAUTH_STATE_COOKIE_INSECURE
    )


def _cookie_kwargs(*, httponly: bool, max_age: int | None = None) -> dict:
    """构造所有身份 Cookie 共用的安全属性。"""
    kwargs: dict = {
        "path": "/",
        "secure": settings.user_session_cookie_secure,
        "httponly": httponly,
        "samesite": "lax" if httponly else "strict",
    }
    if max_age is not None:
        kwargs["max_age"] = max_age
    return kwargs


def set_oauth_state_cookie(response: Response, binding: str) -> None:
    """把一次性浏览器绑定值写入 HttpOnly Cookie。"""
    response.set_cookie(
        oauth_state_cookie_name(),
        binding,
        **_cookie_kwargs(
            httponly=True,
            max_age=settings.oauth_state_ttl_seconds,
        ),
    )


def clear_oauth_state_cookie(response: Response) -> None:
    """清除 OAuth 浏览器绑定 Cookie。"""
    response.delete_cookie(
        oauth_state_cookie_name(),
        path="/",
        secure=settings.user_session_cookie_secure,
        httponly=True,
        samesite="lax",
    )


def set_user_cookies(response: Response, token: str, csrf_token: str) -> None:
    """写入 HttpOnly session Cookie 与可被前端读取的 CSRF Cookie。"""
    max_age = int(timedelta(hours=settings.user_session_absolute_hours).total_seconds())
    response.set_cookie(
        session_cookie_name(),
        token,
        **_cookie_kwargs(httponly=True, max_age=max_age),
    )
    response.set_cookie(
        csrf_cookie_name(),
        csrf_token,
        **_cookie_kwargs(httponly=False, max_age=max_age),
    )


def clear_user_cookies(response: Response) -> None:
    """清除浏览器两个会话 Cookie。"""
    for name, httponly, samesite in (
        (session_cookie_name(), True, "lax"),
        (csrf_cookie_name(), False, "strict"),
    ):
        response.delete_cookie(
            name,
            path="/",
            secure=settings.user_session_cookie_secure,
            httponly=httponly,
            samesite=samesite,
        )


def new_session_secrets() -> tuple[str, str]:
    """生成 session token 与 CSRF token 的原始高熵值。"""
    return secrets.token_urlsafe(32), secrets.token_urlsafe(32)


async def create_user_session(db: AsyncSession, user_id: str) -> tuple[str, str]:
    """创建服务端会话并返回只应写入浏览器的原始 token。"""
    token, csrf_token = new_session_secrets()
    now = utcnow()
    absolute = now + timedelta(hours=settings.user_session_absolute_hours)
    idle = min(
        now + timedelta(minutes=settings.user_session_idle_minutes),
        absolute,
    )
    db.add(
        UserSession(
            token_hash=token_hash(token),
            csrf_hash=token_hash(csrf_token),
            user_id=user_id,
            auth_at=now,
            last_seen_at=now,
            idle_expires_at=idle,
            absolute_expires_at=absolute,
        )
    )
    await db.flush()
    return token, csrf_token


async def resolve_user_session(
    db: AsyncSession, raw_token: str | None
) -> UserPrincipal | None:
    """按 Cookie token 解析用户；过期、撤销或停用时返回 ``None``。"""
    if not raw_token:
        return None
    stmt = (
        sa.select(UserSession, AppUser)
        .join(AppUser, AppUser.id == UserSession.user_id)
        .where(UserSession.token_hash == token_hash(raw_token))
    )
    row = (await db.execute(stmt)).first()
    if row is None:
        return None
    session, user = row
    now = utcnow()

    def _naive(value: object) -> datetime:
        """SQLite 有时返回字符串；统一成可比较的 UTC naive datetime。"""
        if isinstance(value, datetime):
            return value.replace(tzinfo=None)
        return datetime.fromisoformat(str(value)).replace(tzinfo=None)

    if (
        session.revoked_at is not None
        or _naive(session.idle_expires_at) <= now
        or _naive(session.absolute_expires_at) <= now
        or user.status != "active"
    ):
        return None

    last_seen = _naive(session.last_seen_at)
    if now - last_seen >= timedelta(minutes=5):
        session.last_seen_at = now
        session.idle_expires_at = min(
            now + timedelta(minutes=settings.user_session_idle_minutes),
            _naive(session.absolute_expires_at),
        )
        await db.commit()

    return UserPrincipal(
        user_id=user.id,
        tenant_id=user.tenant_id,
        username=user.username,
        phone=user.phone,
        phone_verified=(
            None if user.phone_verified is None else bool(user.phone_verified)
        ),
        email=user.email,
        email_verified=(
            None if user.email_verified is None else bool(user.email_verified)
        ),
        status=user.status,
        session_id=session.id,
        csrf_hash=session.csrf_hash,
    )


async def revoke_session(db: AsyncSession, session_id: int) -> None:
    """撤销单个当前会话。"""
    await db.execute(
        sa.update(UserSession)
        .where(UserSession.id == session_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    await db.commit()


async def revoke_all_user_sessions(db: AsyncSession, user_id: str) -> int:
    """撤销一个用户的全部有效会话，返回受影响行数。"""
    result = await db.execute(
        sa.update(UserSession)
        .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    await db.commit()
    return int(result.rowcount or 0)


def request_session_token(request: Request) -> str | None:
    """从请求 Cookie 中读取 session token。"""
    return request.cookies.get(session_cookie_name())


def request_csrf_token(request: Request) -> str | None:
    """从请求 Cookie 中读取浏览器侧 CSRF token。"""
    return request.cookies.get(csrf_cookie_name())
