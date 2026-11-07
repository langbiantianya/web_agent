"""API 层公共依赖。"""

from __future__ import annotations

import hmac
from dataclasses import replace
from typing import Annotated

from fastapi import Depends, Header, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.db import get_db
from app.config.settings import settings
from app.core.admin_auth import (
    AdminPrincipal,
    has_admin_session_cookie,
    request_csrf_cookie,
    resolve_admin_request,
    verify_admin_csrf,
)
from app.core.admin_authz import casbin_service
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


async def admin_session(
    request: Request,
    db: DbSession,
    csrf_header: Annotated[
        str | None,
        Header(alias="X-CSRF-Token", description="后台 Session CSRF token"),
    ] = None,
    tenant_header: Annotated[
        str | None,
        Header(alias="X-Tenant-ID", description="平台超级管理员选择的租户"),
    ] = None,
) -> AdminPrincipal:
    """要求有效 OAuth 管理员或平台超级管理员 Session。"""
    principal = await resolve_admin_request(db, request)
    if principal is None:
        raise UnauthorizedError("请先登录独立管理后台")
    await db.commit()
    if principal.is_platform_super_admin:
        effective_tenant = (tenant_header or settings.auth_default_tenant).strip()
        if not effective_tenant:
            raise ForbiddenError("平台管理员必须指定租户")
        principal = replace(principal, tenant_id=effective_tenant)
    elif tenant_header and tenant_header != principal.tenant_id:
        raise ForbiddenError("OAuth 管理员不能跨租户操作")

    if request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
        _validate_origin(request)
        # 管理后台和普通用户可能在同一个浏览器域内同时持有 Session。
        # 浏览器给后台 API 显式发送该头时，优先按后台 Session 校验，避免
        # 与普通用户写入共用 X-CSRF-Token 时互相覆盖。
        effective_csrf = request.headers.get("X-Admin-CSRF-Token") or csrf_header
        csrf_cookie = request_csrf_cookie(
            request, super_admin=principal.is_platform_super_admin
        )
        if (
            not csrf_cookie
            or not effective_csrf
            or not hmac.compare_digest(csrf_cookie, effective_csrf)
            or not await verify_admin_csrf(principal, effective_csrf)
        ):
            raise ForbiddenError("CSRF 校验失败")
    return principal


AdminDep = Annotated[AdminPrincipal, Depends(admin_session)]


async def optional_admin_session(
    request: Request,
    db: DbSession,
    tenant_header: Annotated[
        str | None,
        Header(alias="X-Tenant-ID", description="平台超级管理员选择的租户"),
    ] = None,
) -> AdminPrincipal | None:
    """可选后台身份；Cookie 存在但无效时仍返回 401。"""
    if not has_admin_session_cookie(request):
        return None
    principal = await resolve_admin_request(db, request)
    if principal is None:
        raise UnauthorizedError("后台会话无效或已过期")
    await db.commit()
    if principal.is_platform_super_admin:
        return replace(
            principal,
            tenant_id=(tenant_header or settings.auth_default_tenant).strip(),
        )
    if tenant_header and tenant_header != principal.tenant_id:
        raise ForbiddenError("OAuth 管理员不能跨租户读取")
    return principal


OptionalAdminDep = Annotated[
    AdminPrincipal | None,
    Depends(optional_admin_session),
]


def permission_dependency(object_name: str, action: str):
    """构造一个 Casbin object/action 权限依赖。"""

    async def dependency(
        db: DbSession,
        principal: AdminDep,
    ) -> AdminPrincipal:
        """平台超级管理员直接放行，OAuth 管理员执行 Casbin。"""
        if principal.is_platform_super_admin:
            return principal
        if not await casbin_service.can(
            db,
            user_id=principal.user_id or "",
            tenant_id=principal.tenant_id or settings.auth_default_tenant,
            object_name=object_name,
            action=action,
        ):
            raise ForbiddenError(
                f"缺少权限 {object_name}.{action}"
            )
        return principal

    return dependency


WorkflowReadDep = Annotated[
    AdminPrincipal, Depends(permission_dependency("workflow", "read"))
]
WorkflowWriteDep = Annotated[
    AdminPrincipal, Depends(permission_dependency("workflow", "write"))
]
AgentReadDep = Annotated[
    AdminPrincipal, Depends(permission_dependency("agent", "read"))
]
AgentWriteDep = Annotated[
    AdminPrincipal, Depends(permission_dependency("agent", "write"))
]
SkillReadDep = Annotated[
    AdminPrincipal, Depends(permission_dependency("skill", "read"))
]
SkillWriteDep = Annotated[
    AdminPrincipal, Depends(permission_dependency("skill", "write"))
]
McpReadDep = Annotated[
    AdminPrincipal, Depends(permission_dependency("mcp", "read"))
]
McpWriteDep = Annotated[
    AdminPrincipal, Depends(permission_dependency("mcp", "write"))
]
McpRefreshDep = Annotated[
    AdminPrincipal, Depends(permission_dependency("mcp", "refresh"))
]
LlmReadDep = Annotated[
    AdminPrincipal, Depends(permission_dependency("llm", "read"))
]
LlmWriteDep = Annotated[
    AdminPrincipal, Depends(permission_dependency("llm", "write"))
]
AuditReadDep = Annotated[
    AdminPrincipal, Depends(permission_dependency("audit", "read"))
]
OAuthProviderReadDep = Annotated[
    AdminPrincipal,
    Depends(permission_dependency("oauth_provider", "read")),
]
OAuthProviderManageDep = Annotated[
    AdminPrincipal,
    Depends(permission_dependency("oauth_provider", "manage")),
]


__all__ = [
    "AdminDep",
    "CurrentUserDep",
    "DbSession",
    "AgentReadDep",
    "AgentWriteDep",
    "AuditReadDep",
    "LlmReadDep",
    "LlmWriteDep",
    "McpReadDep",
    "McpRefreshDep",
    "McpWriteDep",
    "OAuthProviderManageDep",
    "OAuthProviderReadDep",
    "OptionalAdminDep",
    "ResumeFrom",
    "SkillReadDep",
    "SkillWriteDep",
    "TicketDep",
    "UnauthorizedError",
    "WorkflowReadDep",
    "WorkflowWriteDep",
    "admin_session",
    "current_user",
    "get_db",
    "optional_admin_session",
    "permission_dependency",
    "resume_from_seq",
    "sse_ticket",
]
