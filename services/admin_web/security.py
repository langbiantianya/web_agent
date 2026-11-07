"""管理员会话、CSRF 与密码生成。"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import string
from datetime import datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Request
from starlette.responses import Response

from app.config.settings import settings
from app.models.base import utcnow

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

_PASSWORD_ALPHABET = (
    string.ascii_uppercase + string.ascii_lowercase + string.digits + "_@"
)
_PASSWORD_HASHER = PasswordHasher(
    time_cost=2,
    memory_cost=19456,
    parallelism=1,
    hash_len=32,
    salt_len=16,
)
_SUPER_ADMIN_USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,63}$")


def token_hash(token: str) -> str:
    """计算 Cookie/一次性 token 的 SHA-256。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    """生成 256-bit URL-safe 随机令牌。"""
    return secrets.token_urlsafe(32)


def hash_password(password: str) -> str:
    """使用 Argon2id 生成密码摘要。"""
    return _PASSWORD_HASHER.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """常量时间验证 Argon2id 摘要。"""
    try:
        return _PASSWORD_HASHER.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def generate_password() -> str:
    """生成 8 位随机密码，包含大小写、数字和 `_`/`@`。"""
    required = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice("_@"),
    ]
    required.extend(secrets.choice(_PASSWORD_ALPHABET) for _ in range(4))
    secrets.SystemRandom().shuffle(required)
    return "".join(required)


def normalize_super_admin_username(value: object) -> str | None:
    """规范化平台超级管理员用户名；该账号体系区分大小写。"""
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return (
        normalized
        if _SUPER_ADMIN_USERNAME_RE.fullmatch(normalized)
        else None
    )


def _cookie_secure() -> bool:
    """当前部署是否使用 Secure Cookie。"""
    return settings.admin_session_cookie_secure


def _cookie_name(secure: str, insecure: str) -> str:
    """按 Secure 设置返回 Cookie 名。"""
    return secure if _cookie_secure() else insecure


def admin_session_cookie_name() -> str:
    """OAuth 后台管理员 Session Cookie 名。"""
    return _cookie_name(ADMIN_SESSION_COOKIE_SECURE, ADMIN_SESSION_COOKIE_INSECURE)


def admin_csrf_cookie_name() -> str:
    """OAuth 后台管理员 CSRF Cookie 名。"""
    return _cookie_name(ADMIN_CSRF_COOKIE_SECURE, ADMIN_CSRF_COOKIE_INSECURE)


def admin_oauth_cookie_name() -> str:
    """OAuth 登录状态绑定 Cookie 名。"""
    return _cookie_name(ADMIN_OAUTH_COOKIE_SECURE, ADMIN_OAUTH_COOKIE_INSECURE)


def super_session_cookie_name() -> str:
    """平台超级管理员 Session Cookie 名。"""
    return _cookie_name(SUPER_SESSION_COOKIE_SECURE, SUPER_SESSION_COOKIE_INSECURE)


def super_csrf_cookie_name() -> str:
    """平台超级管理员 CSRF Cookie 名。"""
    return _cookie_name(SUPER_CSRF_COOKIE_SECURE, SUPER_CSRF_COOKIE_INSECURE)


def _set_cookie(
    response: Response,
    name: str,
    value: str,
    *,
    httponly: bool,
    max_age: int,
) -> None:
    """写入统一安全属性的 admin Cookie。"""
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        path="/",
        secure=_cookie_secure(),
        httponly=httponly,
        samesite="lax" if httponly else "strict",
    )


def set_admin_session_cookies(
    response: Response, token: str, csrf_token: str
) -> None:
    """写入 OAuth 管理员 Session 和 CSRF Cookie。"""
    max_age = int(
        timedelta(hours=settings.admin_session_absolute_hours).total_seconds()
    )
    _set_cookie(
        response,
        admin_session_cookie_name(),
        token,
        httponly=True,
        max_age=max_age,
    )
    _set_cookie(
        response,
        admin_csrf_cookie_name(),
        csrf_token,
        httponly=False,
        max_age=max_age,
    )


def set_super_session_cookies(
    response: Response, token: str, csrf_token: str
) -> None:
    """写入平台超级管理员 Session 和 CSRF Cookie。"""
    max_age = int(
        timedelta(hours=settings.admin_session_absolute_hours).total_seconds()
    )
    _set_cookie(
        response,
        super_session_cookie_name(),
        token,
        httponly=True,
        max_age=max_age,
    )
    _set_cookie(
        response,
        super_csrf_cookie_name(),
        csrf_token,
        httponly=False,
        max_age=max_age,
    )


def clear_admin_cookies(response: Response) -> None:
    """清除全部 OAuth 管理员 Cookie。"""
    for name, httponly, samesite in (
        (admin_session_cookie_name(), True, "lax"),
        (admin_csrf_cookie_name(), False, "strict"),
        (admin_oauth_cookie_name(), True, "lax"),
    ):
        response.delete_cookie(
            name,
            path="/",
            secure=_cookie_secure(),
            httponly=httponly,
            samesite=samesite,
        )


def clear_super_admin_cookies(response: Response) -> None:
    """清除平台超级管理员 Cookie。"""
    for name, httponly, samesite in (
        (super_session_cookie_name(), True, "lax"),
        (super_csrf_cookie_name(), False, "strict"),
    ):
        response.delete_cookie(
            name,
            path="/",
            secure=_cookie_secure(),
            httponly=httponly,
            samesite=samesite,
        )


def set_oauth_binding_cookie(response: Response, value: str) -> None:
    """写入短期 OAuth browser-binding Cookie。"""
    _set_cookie(
        response,
        admin_oauth_cookie_name(),
        value,
        httponly=True,
        max_age=settings.oauth_state_ttl_seconds,
    )


def clear_oauth_binding_cookie(response: Response) -> None:
    """清除 OAuth browser-binding Cookie。"""
    response.delete_cookie(
        admin_oauth_cookie_name(),
        path="/",
        secure=_cookie_secure(),
        httponly=True,
        samesite="lax",
    )


def csrf_matches(cookie_value: str | None, form_value: str | None) -> bool:
    """验证表单和 Cookie 中的 CSRF token 一致。"""
    return bool(
        cookie_value
        and form_value
        and hmac.compare_digest(cookie_value, form_value)
    )


def session_expiries() -> tuple[datetime, datetime]:
    """构造 admin Session 的空闲/绝对过期时间。"""
    now = utcnow()
    absolute = now + timedelta(hours=settings.admin_session_absolute_hours)
    idle = min(
        now + timedelta(minutes=settings.admin_session_idle_minutes),
        absolute,
    )
    return idle, absolute


def request_session_token(request: Request, *, super_admin: bool = False) -> str | None:
    """从请求读取对应管理员 Session Cookie。"""
    name = (
        super_session_cookie_name()
        if super_admin
        else admin_session_cookie_name()
    )
    return request.cookies.get(name)


def request_csrf_cookie(request: Request, *, super_admin: bool = False) -> str | None:
    """从请求读取对应管理员 CSRF Cookie。"""
    name = super_csrf_cookie_name() if super_admin else admin_csrf_cookie_name()
    return request.cookies.get(name)
