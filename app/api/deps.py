"""API 层公共依赖。"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Header, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.db import get_db
from app.config.settings import settings
from app.core.admin import AdminPrincipal, require_min_role, resolve_admin
from app.core.errors import ForbiddenError, UnauthorizedError
from app.core.ticket import verify_ticket
from app.core.user_auth import (
    UserPrincipal,
    request_csrf_token,
    request_session_token,
    resolve_user_session,
    token_hash,
)

DbSession = Annotated[AsyncSession, Depends(get_db)]


def _validate_origin(request: Request) -> None:
    """对带 Origin 的写请求校验同源或显式白名单。"""
    origin = request.headers.get("origin")
    if not origin:
        return
    if settings.auth_allowed_origins:
        allowed = {item.rstrip("/") for item in settings.auth_allowed_origins}
        if origin.rstrip("/") not in allowed:
            raise ForbiddenError("请求来源不在允许列表中")
        return
    expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
    if origin.rstrip("/") != expected.rstrip("/"):
        raise ForbiddenError("请求来源与站点不一致")


async def current_user(
    request: Request,
    db: DbSession,
    csrf_header: Annotated[
        str | None,
        Header(alias="X-CSRF-Token", description="当前服务端会话的 CSRF token"),
    ] = None,
) -> UserPrincipal:
    """要求有效服务端会话，并保护所有修改请求。

    身份只来自 HttpOnly session Cookie。即使客户端提交 user_id，也不会进入
    这个依赖；缺失/过期会话统一返回 401。
    """
    principal = await resolve_user_session(db, request_session_token(request))
    if principal is None:
        raise UnauthorizedError("请先登录")

    if request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
        _validate_origin(request)
        csrf_cookie = request_csrf_token(request)
        if (
            not csrf_cookie
            or not csrf_header
            or not hmac.compare_digest(token_hash(csrf_cookie), principal.csrf_hash)
            or not hmac.compare_digest(csrf_header, csrf_cookie)
        ):
            raise ForbiddenError("CSRF 校验失败")
    return principal


CurrentUserDep = Annotated[UserPrincipal, Depends(current_user)]


def sse_ticket(
    ticket: Annotated[str, Query(description="由 /api/auth/sse-ticket 签发的短期票据")],
) -> dict:
    """SSE 连接鉴权依赖。EventSource 不能带自定义 header，只能走 query。"""
    return verify_ticket(ticket)


TicketDep = Annotated[dict, Depends(sse_ticket)]


def resume_from_seq(
    last_seq: Annotated[int, Query(ge=0, description="上次收到的 seq")] = 0,
    last_event_id: Annotated[
        str | None,
        Header(alias="last-event-id", description="浏览器自动重连时携带"),
    ] = None,
) -> int:
    """断点位置：优先用 ``Last-Event-ID`` 头，回退到 ``last_seq`` query。

    浏览器内置的 EventSource 重连会自动带上 Last-Event-ID；前端主动重连则用
    query 传 last_seq。两者都支持才能无缝覆盖两种重连方式（设计文档 §5.5）。
    """
    if last_event_id:
        try:
            parsed = int(last_event_id)
        except (TypeError, ValueError):
            parsed = 0
        return max(parsed, last_seq)
    return last_seq


ResumeFrom = Annotated[int, Depends(resume_from_seq)]


async def admin_key(
    db: DbSession,
    key: Annotated[
        str | None, Header(alias="X-Admin-Key", description="管理员密钥")
    ] = None,
) -> AdminPrincipal:
    """管理员门禁依赖。

    未配置 ``ADMIN_KEY`` 时一律 401（fail-closed）——这道门后面是「谁能改模型的
    行为」，默认开着等于没有门。

    Returns:
        管理员标识，写进审计的 ``author``。
    """
    return await resolve_admin(db, key)


AdminDep = Annotated[AdminPrincipal, Depends(admin_key)]


async def optional_admin_key(
    db: DbSession,
    key: Annotated[
        str | None, Header(alias="X-Admin-Key", description="管理员密钥")
    ] = None,
) -> AdminPrincipal | None:
    """可选管理员身份。

    公开读接口在未携带 Key 时使用默认租户；携带 Key 时按该管理员的租户读取。
    一旦携带了 Key，非法 Key 仍返回 401，不能借“可选”绕过鉴权。
    """
    if not key:
        return None
    return await resolve_admin(db, key)


OptionalAdminDep = Annotated[
    AdminPrincipal | None,
    Depends(optional_admin_key),
]


async def operator_key(admin: AdminDep) -> AdminPrincipal:
    """要求 operator 或 admin 角色。"""
    return require_min_role(admin, "operator")


async def super_admin_key(admin: AdminDep) -> AdminPrincipal:
    """要求 admin 角色。"""
    return require_min_role(admin, "admin")


OperatorDep = Annotated[AdminPrincipal, Depends(operator_key)]
SuperAdminDep = Annotated[AdminPrincipal, Depends(super_admin_key)]


__all__ = [
    "AdminDep",
    "CurrentUserDep",
    "DbSession",
    "OperatorDep",
    "OptionalAdminDep",
    "ResumeFrom",
    "SuperAdminDep",
    "TicketDep",
    "UnauthorizedError",
    "admin_key",
    "current_user",
    "get_db",
    "optional_admin_key",
    "resume_from_seq",
    "sse_ticket",
]
