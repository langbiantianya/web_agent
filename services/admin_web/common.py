"""跨业务域共用的请求处理辅助。

原先是 ``app.py`` 里的私有函数，拆包后被多个路由模块用到，因此提升为
公开 API 并集中到一处——集中在这里才能避免路由模块反向 import
``app.py``（那会造成循环导入）。
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import sqlalchemy as sa
from ludic.web import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.config.settings import settings
from app.models.admin_control import (
    AdminSession,
    AdminUiPreference,
    SuperAdminSession,
)
from app.models.base import utcnow
from services.admin_web.accounts import (
    AdminPrincipal,
    resolve_admin_session,
    resolve_super_admin_session,
    verify_session_csrf,
)
from services.admin_web.db import AdminSessionLocal
from services.admin_web.registry import can_access_page, can_show_component
from services.admin_web.security import (
    clear_admin_cookies,
    clear_super_admin_cookies,
    csrf_matches,
    request_csrf_cookie,
    request_session_token,
)
from services.admin_web.views import error_page


def json_response_no_store(
    payload: dict[str, Any],
    status_code: int = 200,
) -> JSONResponse:
    """返回禁止中间层缓存的 JSON，供含凭据或探测结果的接口复用。"""
    response = JSONResponse(payload, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    return response


def origin_ok(request: Request) -> bool:
    """对 POST 请求执行同源或白名单 Origin 校验。"""
    origin = request.headers.get("origin")
    if not origin:
        return True
    allowed = {item.rstrip("/") for item in settings.auth_allowed_origins}
    if allowed:
        return origin.rstrip("/") in allowed
    expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
    return origin.rstrip("/") == expected.rstrip("/")


def safe_next(raw: str | None) -> str:
    """只接受 /admin 内部路径。"""
    value = (raw or "/admin/").strip()
    if (
        not value.startswith("/admin/")
        or value.startswith("//")
        or "\\" in value
        or any(ord(ch) < 32 for ch in value)
    ):
        return "/admin/"
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return "/admin/"
    return value


def callback_uri(provider_code: str) -> str:
    """构造固定 admin OAuth callback URL。"""
    base = settings.admin_public_base_url.rstrip("/")
    if not base:
        base = "http://localhost:8001/admin"
    return f"{base}/auth/oauth/{quote(provider_code, safe='')}/callback"


def append_query(url: str, **params: str | None) -> str:
    """保留 endpoint query 并追加 OAuth 参数。"""
    parsed = urlsplit(url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.extend((key, value) for key, value in params.items() if value is not None)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


async def principal_of(db, request: Request) -> AdminPrincipal | None:
    """解析 OAuth/平台超级管理员 Session；双 Session 同时存在时拒绝。"""
    oauth_principal = await resolve_admin_session(
        db, request_session_token(request)
    )
    super_principal = await resolve_super_admin_session(
        db, request_session_token(request, super_admin=True)
    )
    if oauth_principal and super_principal:
        # 两个身份链同时出现时无法判断调用者意图，必须让双方重新认证。
        await db.execute(
            sa.update(AdminSession)
            .where(AdminSession.id == oauth_principal.session_id)
            .values(revoked_at=utcnow())
        )
        await db.execute(
            sa.update(SuperAdminSession)
            .where(SuperAdminSession.id == super_principal.session_id)
            .values(revoked_at=utcnow())
        )
        await db.commit()
        return None
    return oauth_principal or super_principal


async def preference_of(db, principal: AdminPrincipal) -> AdminUiPreference:
    """读取或创建管理员主题偏好。"""
    kind = "platform_super_admin" if principal.is_platform_super_admin else "oauth_admin"
    pref = await db.get(AdminUiPreference, (kind, principal.principal_id))
    if pref is None:
        pref = AdminUiPreference(
            principal_kind=kind,
            principal_id=principal.principal_id,
            theme=settings.admin_ui_default_theme,
            color_mode="system",
            density="comfortable",
        )
        db.add(pref)
        await db.commit()
        await db.refresh(pref)
    return pref


async def authenticated(
    request: Request,
) -> tuple[object, AdminPrincipal, AdminUiPreference, str]:
    """为页面处理函数读取 DB、主体、偏好和 CSRF。"""
    db = AdminSessionLocal()
    principal = await principal_of(db, request)
    if principal is None:
        await db.close()
        raise PermissionError("admin_login_required")
    pref = await preference_of(db, principal)
    csrf = request_csrf_cookie(
        request, super_admin=principal.is_platform_super_admin
    ) or ""
    return db, principal, pref, csrf


async def require_csrf(
    db, request: Request, principal: AdminPrincipal, form
) -> bool:
    """验证 Cookie、表单与数据库 Session 三方 CSRF。"""
    form_token = str(form.get("csrf_token") or "")
    if not csrf_matches(
        request_csrf_cookie(
            request, super_admin=principal.is_platform_super_admin
        ),
        form_token,
    ):
        return False
    return await verify_session_csrf(db, principal, form_token)


def login_redirect(message: str) -> RedirectResponse:
    """统一跳回登录页。"""
    return RedirectResponse(f"/admin/login?error={quote(message)}", status_code=303)


async def with_auth(request: Request):
    """包装认证读取并清理数据库会话。"""
    try:
        return await authenticated(request)
    except PermissionError:
        return None


def unauthorized() -> RedirectResponse:
    """匿名或冲突会话统一回登录，并清除两条管理员 Cookie 链。"""
    response = RedirectResponse("/admin/login", status_code=303)
    clear_admin_cookies(response)
    clear_super_admin_cookies(response)
    return response


def not_found() -> Any:
    """返回无页面权限错误页，并使用真实 HTTP 404。"""
    return html_error(
        title_text="Page not found",
        message="The requested management page is unavailable.",
        status_code=404,
    )


def html_error(
    *, title_text: str, message: str, status_code: int
) -> HTMLResponse:
    """把 Ludic 错误页包装为带状态码的 Starlette 响应。"""
    return HTMLResponse(
        str(
            error_page(
                title_text=title_text,
                message=message,
                status_hint=status_code,
            )
        ),
        status_code=status_code,
    )


def tenant_id_of(principal: AdminPrincipal) -> str:
    """返回后台主体当前操作的租户。"""
    return principal.tenant_id or settings.auth_default_tenant or "default"


def flash_from_query(request: Request) -> tuple[str | None, str]:
    """把固定的 notice/error 查询值转换为页面提示，绝不回显任意查询文本。"""
    notices = {
        "created": "Resource created.",
        "updated": "Changes saved.",
        "deleted": "Resource deleted.",
        "restored": "Resource restored.",
        "toggled": "Resource state updated.",
        "refreshed": "MCP tool catalog refreshed.",
        "saved": "Preferences saved.",
    }
    errors = {
        "invalid": "The submitted values are invalid.",
        "duplicate": "A resource with that code already exists.",
        "missing": "The requested resource no longer exists.",
        "in_use": "This resource is in use and cannot be changed.",
        "agent_in_use": "This Agent is referenced by one or more workflows.",
        "owner_protected": "The owner account cannot be deleted.",
        "forbidden": "The operation is not permitted.",
        "failed": "The operation could not be completed.",
        "undo_failed": (
            "The deleted resource could not be restored. "
            "The undo window may have expired."
        ),
        # MCP 目录刷新失败要能区分原因：连不上与「工具仍被 Agent 占用」
        # 的处理方式完全不同（UX-34）。
        "mcp_unreachable": (
            "The MCP server could not be reached, or its tool catalog "
            "could not be read. Check the URL or command and try again."
        ),
        "mcp_tools_in_use": (
            "Some MCP tools are still used by Agents and cannot be removed "
            "from the catalog. Release them first, then refresh again."
        ),
    }
    notice = request.query_params.get("notice", "")
    if notice in notices:
        return notices[notice], "info"
    error = request.query_params.get("error", "")
    if error in errors:
        return errors[error], "warn"
    return None, "info"


def undo_id_from_query(request: Request) -> int | None:
    """读取 URL 上的撤销令牌。

    只接受**正整数**：令牌是内部生成的，URL 上出现别的形态说明请求
    不是本模块构造的，直接当作没有撤销入口。
    """
    raw = request.query_params.get("undo", "")
    if not raw.isdigit():
        return None
    value = int(raw)
    return value if value > 0 else None


def as_bool(value: object, default: bool = False) -> bool:
    """把 HTML 表单值规范成布尔值。"""
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def csv_list(value: object) -> list[str]:
    """解析逗号分隔的多值表单字段。"""
    return [
        item.strip()
        for item in str(value or "").replace("\n", ",").split(",")
        if item.strip()
    ]


def json_object(value: object) -> dict:
    """解析必须是 JSON object 的表单字段。"""
    parsed = json.loads(str(value or "{}"))
    if not isinstance(parsed, dict):
        raise ValueError("json_object_required")
    return parsed


async def can_edit_resource(
    db, principal: AdminPrincipal, *, page_code: str, component_code: str
) -> bool:
    """同时检查页面可见性与写组件权限。"""
    return await can_access_page(db, principal, page_code) and await can_show_component(
        db, principal, page_code, component_code
    )


async def operation_authorized(
    request: Request, db, principal: AdminPrincipal, *, page_code: str,
    component_code: str, form,
) -> bool:
    """校验写操作的同源、CSRF 和页面组件权限。"""
    return (
        origin_ok(request)
        and await can_edit_resource(
            db,
            principal,
            page_code=page_code,
            component_code=component_code,
        )
        and await require_csrf(db, request, principal, form)
    )


def business_redirect(path: str, notice: str = "", error: str = "") -> RedirectResponse:
    """返回业务页静态提示重定向。"""
    suffix = f"?notice={notice}" if notice else f"?error={error}"
    return RedirectResponse(f"{path}{suffix}", status_code=303)


__all__ = [
    "origin_ok", "safe_next", "callback_uri", "append_query", "principal_of",
    "preference_of", "authenticated", "with_auth", "require_csrf",
    "login_redirect", "unauthorized", "not_found", "html_error",
    "tenant_id_of", "flash_from_query", "undo_id_from_query", "as_bool", "csv_list",
    "json_object", "can_edit_resource", "operation_authorized",
    "business_redirect",
]
