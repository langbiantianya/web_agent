"""独立 Ludic admin-web ASGI 应用。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import secrets
from contextlib import asynccontextmanager
from datetime import timedelta
from urllib.parse import quote, parse_qsl, urlencode, urlsplit, urlunsplit

import sqlalchemy as sa
from ludic.web import LudicApp, Request
from pydantic import ValidationError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

from app.config.settings import settings
from app.core.crypto import decrypt, encrypt
from app.core.mcp.catalog import McpCatalogError, refresh_catalog
from app.core.mcp.transport import (
    McpTransportError,
    check_command_allowed,
    check_url_allowed,
)
from app.core.oidc import (
    OIDCError,
    exchange_code,
    resolve_provider_endpoints,
    verify_id_token,
)
from app.core.skill_names import (
    SkillConfigError,
    normalize_skill_codes,
)
from app.core.skill_pack import compute_content_hash
from app.core.tools import normalize_tool_names
from app.core.user_profile import normalize_email, normalize_phone
from app.core.workflow_def import WorkflowDefinition, collect_graph_issues
from app.models.admin_control import (
    AdminComponentDefinition,
    AdminComponentPermission,
    AdminPageDefinition,
    AdminPagePermission,
    AdminSession,
    AdminUiPreference,
    AuthzPolicyVersion,
    CasbinRule,
    OAuthRoleMapping,
    SuperAdminAccount,
    SuperAdminSession,
    permission_binding_scope,
)
from app.models.admin_user import AdminUser
from app.models.base import utcnow
from app.models.conversation import Conversation
from app.models.llm_endpoint import LlmEndpoint
from app.models.mcp_server import McpServer
from app.models.oauth import OAuthLoginAttempt, OAuthProvider
from app.models.run import Run
from app.models.skill import Skill
from app.models.workflow import Workflow
from app.repo.agent_repo import agent_repo
from app.repo.llm_endpoint_repo import llm_endpoint_repo
from app.repo.mcp_repo import mcp_server_repo, mcp_tool_repo
from app.repo.oauth_provider_repo import oauth_provider_repo
from app.repo.skill_repo import skill_repo
from app.repo.tool_audit_repo import tool_audit_repo
from app.repo.workflow_repo import workflow_repo
from app.schemas.agent import AgentCreate, AgentUpdate
from app.schemas.llm_endpoint import LlmEndpointCreate, LlmEndpointUpdate
from app.schemas.mcp import McpServerCreate, McpServerUpdate
from app.schemas.oauth_provider import OAuthProviderCreate, OAuthProviderUpdate
from app.schemas.skill import SkillCreate, SkillUpdate
from app.schemas.workflow import WorkflowCreate, WorkflowUpdate
from services.admin_web.accounts import (
    AdminPrincipal,
    LoginError,
    authenticate_super_admin,
    create_admin_session,
    create_super_admin_account,
    create_super_admin_session,
    ensure_owner_super_admin,
    reset_super_admin_password,
    resolve_admin_session,
    resolve_super_admin_session,
    revoke_super_admin_sessions,
    rotate_expired_super_admins,
    verify_session_csrf,
)
from services.admin_web.audit import (
    audit_principal_action,
    record_admin_audit,
)
from services.admin_web.db import AdminSessionLocal, dispose_admin_engine
from services.admin_web.notifications import notify_password
from services.admin_web.oauth_admin import (
    provider_for_admin_login,
    provisioning_error_code,
    sync_oauth_admin,
)
from services.admin_web.rbac import casbin_service
from services.admin_web.registry import (
    can_access_page,
    can_show_component,
    page_spec,
    seed_registry,
    visible_pages,
)
from services.admin_web.security import (
    admin_oauth_cookie_name,
    clear_admin_cookies,
    clear_oauth_binding_cookie,
    clear_super_admin_cookies,
    csrf_matches,
    normalize_super_admin_username,
    request_csrf_cookie,
    request_session_token,
    set_admin_session_cookies,
    set_oauth_binding_cookie,
    set_super_session_cookies,
    token_hash,
)
from services.admin_web.views import (
    access_components_page,
    access_pages_page,
    agents_page,
    audit_page,
    dashboard,
    error_page,
    llm_page,
    login_page,
    mappings_page,
    mcp_page,
    oauth_providers_page,
    platform_admins_page,
    preferences_page,
    roles_page,
    skills_page,
    workflows_page,
)

logger = logging.getLogger(__name__)
STATIC_DIR = __import__("pathlib").Path(__file__).with_name("static")
ALLOWED_THEMES = {"fluent", "cloudscape", "material"}
ALLOWED_COLOR_MODES = {"light", "dark", "system"}
ALLOWED_DENSITIES = {"comfortable", "compact"}
CRITICAL_PAGES = {
    "dashboard",
    "access_pages",
    "access_components",
    "platform_admins",
}
TENANT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")

KNOWN_PERMISSIONS = {
    ("admin", "read"),
    ("admin_ui", "read"),
    ("admin_ui", "manage"),
    ("workflow", "read"),
    ("workflow", "write"),
    ("agent", "read"),
    ("agent", "write"),
    ("skill", "read"),
    ("skill", "write"),
    ("mcp", "read"),
    ("mcp", "write"),
    ("mcp", "refresh"),
    ("llm", "read"),
    ("llm", "write"),
    ("audit", "read"),
    ("admin_user", "read"),
    ("admin_user", "manage"),
    ("casbin", "role_read"),
    ("casbin", "role_manage"),
    ("casbin", "mapping_manage"),
    ("oauth_provider", "read"),
    ("oauth_provider", "manage"),
    ("cross_tenant", "read"),
    ("cross_tenant", "manage"),
    ("platform_admin", "owner"),
}


async def _rotation_loop() -> None:
    """每小时执行一次 30 天超级管理员密码轮换。"""
    while True:
        try:
            async with AdminSessionLocal() as db:
                count = await rotate_expired_super_admins(db)
            if count:
                logger.info("已轮换 %s 个平台超级管理员密码", count)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("平台超级管理员密码轮换失败")
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(_: LudicApp):
    """初始化 owner、registry/Casbin，并启动密码轮换任务。"""
    async with AdminSessionLocal() as db:
        initial_password = await ensure_owner_super_admin(db)
        if initial_password is not None:
            logger.warning(
                "首次安装已创建平台超级管理员 superAdmin；"
                "初始密码仅本次日志输出：%s",
                initial_password,
            )
        await seed_registry(db)
        await casbin_service.refresh(db, force=True)
    task = asyncio.create_task(_rotation_loop())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await dispose_admin_engine()


app = LudicApp(lifespan=lifespan)
app.mount("/admin/static", StaticFiles(directory=str(STATIC_DIR)), name="admin-static")


async def security_headers(request: Request, call_next):
    """添加后台安全响应头。"""
    response: Response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self'; "
        "script-src 'self'; connect-src 'self'; frame-ancestors 'none'",
    )
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


app.add_middleware(BaseHTTPMiddleware, dispatch=security_headers)


def _origin_ok(request: Request) -> bool:
    """对 POST 请求执行同源或白名单 Origin 校验。"""
    origin = request.headers.get("origin")
    if not origin:
        return True
    allowed = {item.rstrip("/") for item in settings.auth_allowed_origins}
    if allowed:
        return origin.rstrip("/") in allowed
    expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
    return origin.rstrip("/") == expected.rstrip("/")


def _safe_next(raw: str | None) -> str:
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


def _callback_uri(provider_code: str) -> str:
    """构造固定 admin OAuth callback URL。"""
    base = settings.admin_public_base_url.rstrip("/")
    if not base:
        base = "http://localhost:8001/admin"
    return f"{base}/auth/oauth/{quote(provider_code, safe='')}/callback"


def _append_query(url: str, **params: str | None) -> str:
    """保留 endpoint query 并追加 OAuth 参数。"""
    parsed = urlsplit(url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.extend((key, value) for key, value in params.items() if value is not None)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


async def _principal(
    db, request: Request
) -> AdminPrincipal | None:
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


async def _preference(
    db, principal: AdminPrincipal
) -> AdminUiPreference:
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


async def _authenticated(
    request: Request,
) -> tuple[object, AdminPrincipal, AdminUiPreference, str]:
    """为页面处理函数读取 DB、主体、偏好和 CSRF。"""
    db = AdminSessionLocal()
    principal = await _principal(db, request)
    if principal is None:
        await db.close()
        raise PermissionError("admin_login_required")
    pref = await _preference(db, principal)
    csrf = request_csrf_cookie(
        request, super_admin=principal.is_platform_super_admin
    ) or ""
    return db, principal, pref, csrf


async def _require_csrf(
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


def _login_redirect(message: str) -> RedirectResponse:
    """统一跳回登录页。"""
    return RedirectResponse(f"/admin/login?error={quote(message)}", status_code=303)


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
        existing = await _principal(db, request)
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
    if not _origin_ok(request):
        return _login_redirect("invalid_origin")
    form = await request.form()
    username = str(form.get("username") or "").strip()
    password = str(form.get("password") or "")
    next_url = _safe_next(str(form.get("next") or "/admin/"))
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
            return _login_redirect(str(exc))
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
    if not _origin_ok(request):
        return _login_redirect("invalid_origin")
    async with AdminSessionLocal() as db:
        provider = await provider_for_admin_login(db, provider_code)
        if provider is None:
            return _login_redirect("provider_not_found")
        try:
            endpoints = await resolve_provider_endpoints(provider)
        except OIDCError as exc:
            return _login_redirect(str(exc))
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
        url = _append_query(
            endpoints.authorization_endpoint,
            response_type="code",
            client_id=provider.client_id,
            redirect_uri=_callback_uri(provider.code),
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
        return _login_redirect("state_missing")
    async with AdminSessionLocal() as db:
        provider = await provider_for_admin_login(db, provider_code)
        if provider is None:
            return _login_redirect("provider_not_found")
        attempt = await db.scalar(
            sa.select(OAuthLoginAttempt).where(
                OAuthLoginAttempt.state_hash == token_hash(state),
                OAuthLoginAttempt.provider_id == provider.id,
            )
        )
        if attempt is None:
            return _login_redirect("state_invalid")
        binding = request.cookies.get(admin_oauth_cookie_name(), "")
        if not binding or token_hash(binding) != attempt.browser_binding_hash:
            return _login_redirect("login_csrf")
        if attempt.consumed_at is not None or attempt.expires_at <= utcnow():
            return _login_redirect("state_expired")
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
            return _login_redirect("state_replayed")
        if error:
            return _login_redirect("provider_error")
        if not code:
            return _login_redirect("code_missing")
        try:
            endpoints = await resolve_provider_endpoints(provider)
            id_token = await exchange_code(
                provider,
                endpoints,
                code=code,
                redirect_uri=_callback_uri(provider.code),
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
                return _login_redirect("admin_role_required")
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
            return _login_redirect(provisioning_error_code(exc))
    response = RedirectResponse("/admin/", status_code=303)
    clear_oauth_binding_cookie(response)
    clear_super_admin_cookies(response)
    set_admin_session_cookies(response, token, csrf)
    return response


@app.post("/admin/logout")
async def logout(request: Request):
    """退出当前管理员 Session。"""
    if not _origin_ok(request):
        return RedirectResponse("/admin/login?error=invalid_origin", status_code=303)
    async with AdminSessionLocal() as db:
        principal = await _principal(db, request)
        if principal is not None:
            form = await request.form()
            if await _require_csrf(db, request, principal, form):
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


async def _with_auth(request: Request):
    """包装认证读取并清理数据库会话。"""
    try:
        return await _authenticated(request)
    except PermissionError:
        return None


def _unauthorized() -> RedirectResponse:
    """匿名或冲突会话统一回登录，并清除两条管理员 Cookie 链。"""
    response = RedirectResponse("/admin/login", status_code=303)
    clear_admin_cookies(response)
    clear_super_admin_cookies(response)
    return response


def _not_found() -> Any:
    """返回无页面权限错误页，并使用真实 HTTP 404。"""
    return _html_error(
        title_text="Page not found",
        message="The requested management page is unavailable.",
        status_code=404,
    )


def _html_error(
    *,
    title_text: str,
    message: str,
    status_code: int,
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


def _tenant_id(principal: AdminPrincipal) -> str:
    """返回后台主体当前操作的租户。"""
    return principal.tenant_id or settings.auth_default_tenant or "default"


def _flash_from_query(request: Request) -> tuple[str | None, str]:
    """把固定的 notice/error 查询值转换为页面提示，绝不回显任意查询文本。"""
    notices = {
        "created": "Resource created.",
        "updated": "Changes saved.",
        "deleted": "Resource deleted.",
        "toggled": "Resource state updated.",
        "refreshed": "MCP tool catalog refreshed.",
    }
    errors = {
        "invalid": "The submitted values are invalid.",
        "duplicate": "A resource with that code already exists.",
        "missing": "The requested resource no longer exists.",
        "forbidden": "The operation is not permitted.",
        "failed": "The operation could not be completed.",
    }
    notice = request.query_params.get("notice", "")
    if notice in notices:
        return notices[notice], "info"
    error = request.query_params.get("error", "")
    if error in errors:
        return errors[error], "warn"
    return None, "info"


def _as_bool(value: object, default: bool = False) -> bool:
    """把 HTML 表单值规范成布尔值。"""
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _csv(value: object) -> list[str]:
    """解析逗号分隔的多值表单字段。"""
    return [
        item.strip()
        for item in str(value or "").replace("\n", ",").split(",")
        if item.strip()
    ]


def _json_object(value: object) -> dict:
    """解析必须是 JSON object 的表单字段。"""
    parsed = json.loads(str(value or "{}"))
    if not isinstance(parsed, dict):
        raise ValueError("json_object_required")
    return parsed


async def _can_edit_resource(
    db,
    principal: AdminPrincipal,
    *,
    page_code: str,
    component_code: str,
) -> bool:
    """同时检查页面可见性与写组件权限。"""
    return await can_access_page(db, principal, page_code) and await can_show_component(
        db, principal, page_code, component_code
    )


async def _operation_authorized(
    request: Request,
    db,
    principal: AdminPrincipal,
    *,
    page_code: str,
    component_code: str,
    form,
) -> bool:
    """校验写操作的同源、CSRF 和页面组件权限。"""
    return (
        _origin_ok(request)
        and await _can_edit_resource(
            db,
            principal,
            page_code=page_code,
            component_code=component_code,
        )
        and await _require_csrf(db, request, principal, form)
    )


def _business_redirect(path: str, notice: str = "", error: str = "") -> RedirectResponse:
    """返回业务页静态提示重定向。"""
    suffix = f"?notice={notice}" if notice else f"?error={error}"
    return RedirectResponse(f"{path}{suffix}", status_code=303)


@app.get("/admin/")
async def dashboard_get(request: Request):
    """后台总览。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, pref, csrf = values
    try:
        if not await can_access_page(db, principal, "dashboard"):
            return _not_found()
        pages = await visible_pages(db, principal)
        conversation_query = sa.select(sa.func.count()).select_from(Conversation)
        run_query = sa.select(sa.func.count()).select_from(Run).join(
            Conversation, Conversation.id == Run.conversation_id
        )
        admin_query = sa.select(sa.func.count()).select_from(AdminUser).where(
            AdminUser.status == "active"
        )
        if not principal.is_platform_super_admin:
            conversation_query = conversation_query.where(
                Conversation.tenant_id == principal.tenant_id
            )
            run_query = run_query.where(
                Conversation.tenant_id == principal.tenant_id
            )
            admin_query = admin_query.where(
                AdminUser.tenant_id == principal.tenant_id
            )
        counts = {
            "conversations": int(await db.scalar(conversation_query) or 0),
            "runs": int(await db.scalar(run_query) or 0),
            "admins": int(await db.scalar(admin_query) or 0),
            "platform_admins": (
                int(
                    await db.scalar(
                        sa.select(sa.func.count()).select_from(SuperAdminAccount)
                    )
                    or 0
                )
                if principal.is_platform_super_admin
                else 0
            ),
        }
        overview_visible = await can_show_component(
            db, principal, "dashboard", "metrics"
        )
        quick_visible = await can_show_component(
            db, principal, "dashboard", "quick_actions"
        )
        return dashboard(
            principal=principal,
            csrf=csrf,
            pages=pages,
            theme=pref.theme,
            color_mode=pref.color_mode,
            density=pref.density,
            counts=counts,
            overview_visible=overview_visible,
            quick_actions_visible=quick_visible,
        )
    finally:
        await db.close()


@app.get("/admin/workflows")
async def workflows_get(request: Request):
    """渲染 Workflow 编排列表。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, pref, csrf = values
    try:
        if not await can_access_page(db, principal, "workflows"):
            return _not_found()
        flash, kind = _flash_from_query(request)
        return workflows_page(
            principal=principal,
            csrf=csrf,
            pages=await visible_pages(db, principal),
            rows=await workflow_repo.list(db, limit=500),
            theme=pref.theme,
            color_mode=pref.color_mode,
            density=pref.density,
            can_write=await _can_edit_resource(
                db,
                principal,
                page_code="workflows",
                component_code="workflow_editor",
            ),
            flash=flash,
        )
    finally:
        await db.close()


#: pydantic 给 ``ValueError`` 包装时加的英文前缀，归一时要剥掉。
_VALUE_ERROR_PREFIX = "Value error, "

#: pydantic 错误类型 → 中文文案模板（能定位到字段时用）。
_PYDANTIC_MESSAGE_WITH_FIELD: dict[str, str] = {
    "missing": "缺少必填字段 {field}",
    "extra_forbidden": "不支持的字段 {field}",
    "string_type": "字段 {field} 必须是字符串",
    "int_type": "字段 {field} 必须是整数",
    "int_parsing": "字段 {field} 必须是整数",
    "bool_type": "字段 {field} 必须是布尔值",
    "too_long": "字段 {field} 长度超出限制",
    "string_too_long": "字段 {field} 长度超出限制",
    "list_too_long": "字段 {field} 长度超出限制",
    "too_short": "字段 {field} 长度不足",
    "string_too_short": "字段 {field} 长度不足",
    "greater_than_equal": "字段 {field} 小于允许的最小值",
    "less_than_equal": "字段 {field} 大于允许的最大值",
}

#: pydantic 错误类型 → 中文文案模板（定位不到字段时用）。
_PYDANTIC_MESSAGE_NO_FIELD: dict[str, str] = {
    "missing": "缺少必填字段",
    "extra_forbidden": "存在不支持的字段",
    "string_type": "字段必须是字符串",
    "int_type": "字段必须是整数",
    "int_parsing": "字段必须是整数",
    "bool_type": "字段必须是布尔值",
    "too_long": "字段长度超出限制",
    "string_too_long": "字段长度超出限制",
    "list_too_long": "字段长度超出限制",
    "too_short": "字段长度不足",
    "string_too_short": "字段长度不足",
    "greater_than_equal": "字段小于允许的最小值",
    "less_than_equal": "字段大于允许的最大值",
}


def _strip_value_error_prefix(message: str) -> str:
    """剥掉 pydantic 给 ``ValueError`` 加的英文前缀。

    剥掉后剩下的通常是我们自己写的中文消息（节点字段组合校验、
    模型级图校验），直接透出即可。
    """
    if message.startswith(_VALUE_ERROR_PREFIX):
        return message[len(_VALUE_ERROR_PREFIX) :]
    return message


def _pydantic_field_path(loc: tuple, nodes: list) -> str | None:
    """把 pydantic 的 ``loc`` 渲染成可读的字段路径。

    - ``('nodes', 0, 'id')`` 且能反查到节点 id 时给 ``step1.id``；
      反查不到（节点自己就缺 id）时退回 ``nodes[0].id``；
    - ``('nodes', 0)`` 定位的是**节点整体**，没有字段名；
    - ``('nodes',)`` 与空 ``loc`` 是容器/整体层面的报错，也没有字段名。

    Returns:
        可读字段路径；渲染不出来时返回 None。
    """
    if not loc:
        return None
    last = loc[-1]
    if not isinstance(last, str):
        return None
    if len(loc) == 1:
        # ('nodes',) / ('edges',) 是容器层面的报错，('entry',) 才是字段名
        return None if last in {"nodes", "edges"} else last
    index = loc[1]
    if not isinstance(index, int):
        return last
    container = str(loc[0])
    if container == "nodes":
        node = nodes[index] if index < len(nodes) else None
        node_id = node.get("id") if isinstance(node, dict) else None
        if isinstance(node_id, str) and node_id:
            return f"{node_id}.{last}"
        return f"nodes[{index}].{last}"
    return f"{container}[{index}].{last}"


def _pydantic_message(error_type: str, raw: str, field: str | None) -> str:
    """把 pydantic 的英文内部文案翻成中文。

    未知错误类型保留原始 ``msg`` 兜底——不吞掉信息。
    """
    if error_type == "value_error":
        # 我们自己抛的 ValueError，消息本来就是中文，只去掉英文前缀
        return _strip_value_error_prefix(raw)
    if field:
        template = _PYDANTIC_MESSAGE_WITH_FIELD.get(error_type)
        if template is not None:
            return template.format(field=field)
    else:
        message = _PYDANTIC_MESSAGE_NO_FIELD.get(error_type)
        if message is not None:
            return message
    return raw


def _definition_errors(definition: dict) -> list[dict]:
    """把一份 definition 的全部问题摊平成画布能定位的条目。

    两路串联，缺一不可：

    1. **pydantic** 负责**节点级**问题（id 缺失/重复、未知节点类型、
       agent 节点缺 agentCode、approval 节点缺 prompt…）。它给出的
       ``loc`` 形如 ``("nodes", 0, "id")``，据此能反查是哪个节点。
    2. **collect_graph_issues** 负责**图级**问题（环、孤立节点、条件边
       混用、when 表达式），并直接带出 ``node_ids`` / ``edge_refs``。

    只跑其中一路会漏：pydantic 的 model 级报错会把整张图压成一条
    没有定位的信息；collect_graph_issues 则完全不看节点自身的字段组合。

    模型级条目与图级条目重复时只留图级那条（带定位），详见循环内的说明。
    """
    errors: list[dict] = []
    nodes = definition.get("nodes") or []
    graph_issues = collect_graph_issues(definition)
    graph_messages = {issue.message for issue in graph_issues}
    try:
        WorkflowDefinition.model_validate(definition)
    except ValidationError as exc:
        for item in exc.errors():
            loc = item.get("loc") or ()
            message = str(item.get("msg") or "invalid")
            if not loc:
                # 模型级报错：WorkflowDefinition 的模型级校验器把
                # collect_graph_issues 的首条 issue 原样抛上来，与下面那条
                # 图级条目重复。按**消息**去重而不是无条件丢弃——将来
                # WorkflowDefinition 新增别的模型级校验器时不会误丢消息。
                if _strip_value_error_prefix(message) in graph_messages:
                    continue
            field = _pydantic_field_path(loc, nodes)
            entry: dict[str, str] = {
                "message": _pydantic_message(
                    str(item.get("type") or ""), message, field
                )
            }
            if field:
                entry["field"] = field
            if len(loc) >= 2 and loc[0] == "nodes" and isinstance(loc[1], int):
                node = nodes[loc[1]] if loc[1] < len(nodes) else None
                if isinstance(node, dict) and isinstance(node.get("id"), str):
                    entry["node"] = node["id"]
            errors.append(entry)
    for issue in graph_issues:
        entry = {"message": issue.message}
        if issue.node_ids:
            entry["node"] = issue.node_ids[0]
        if issue.edge_refs:
            entry["edge"] = issue.edge_refs[0]
        errors.append(entry)
    return errors


@app.post("/admin/workflows/validate")
async def workflows_validate(request: Request):
    """校验一份 Workflow definition，不写 Workflow 数据。

    画布用它做实时反馈。规则实现在 ``app.core.workflow_def``，与保存时
    走的是同一份代码——前端不自己判断「这个图合不合法」。

    只读指的是 **Workflow 数据**：登录态与主题偏好的读取沿用既有机制，
    管理员尚无偏好行时后台仍会补建一行（与其它后台页面一致）。
    """
    values = await _with_auth(request)
    if values is None:
        # 本端点服务于画布的 fetch()，必须回 JSON：表单端点那种 303 跳登录页
        # 会被 fetch 跟随后拿到登录页 HTML，前端连 JSON 都解析不出来，
        # 更分不清「登录过期」与「校验失败」。
        return JSONResponse(
            {"ok": False, "errors": [{"message": "forbidden"}]},
            status_code=403,
        )
    db, principal, _pref, _csrf = values
    try:
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return JSONResponse(
                {"ok": False, "errors": [{"message": "invalid_json"}]},
                status_code=400,
            )
        if not isinstance(body, dict):
            return JSONResponse(
                {"ok": False, "errors": [{"message": "invalid_json"}]},
                status_code=400,
            )
        definition = body.get("definition")
        if not isinstance(definition, dict):
            return JSONResponse(
                {"ok": False, "errors": [{"message": "definition_required"}]},
                status_code=400,
            )
        # 形状门是**纵深防御**：collect_graph_issues 自身已能处理畸形负载，
        # 并把畸形部分报成 issue（见 app/core/workflow_def.py 的
        # _sanitize_payload）。这里仍然先挡一道，让形状不对的负载拿到
        # 400 而不是 200 + issues——前端据此区分「负载没送对」与「图不合法」。
        nodes = definition.get("nodes")
        edges = definition.get("edges", [])
        shape_ok = (
            isinstance(nodes, list)
            and isinstance(edges, list)
            and all(isinstance(item, dict) for item in nodes)
            and all(isinstance(item, dict) for item in edges)
        )
        if not shape_ok:
            return JSONResponse(
                {"ok": False, "errors": [{"message": "definition_shape_invalid"}]},
                status_code=400,
            )
        authorized = (
            _origin_ok(request)
            and await _can_edit_resource(
                db,
                principal,
                page_code="workflows",
                component_code="workflow_editor",
            )
            and await _require_csrf(
                db, request, principal, {"csrf_token": body.get("csrf_token")}
            )
        )
        if not authorized:
            return JSONResponse(
                {"ok": False, "errors": [{"message": "forbidden"}]},
                status_code=403,
            )
        errors = _definition_errors(definition)
        if not errors:
            return JSONResponse({"ok": True})
        return JSONResponse({"ok": False, "errors": errors})
    finally:
        await db.close()


@app.post("/admin/workflows")
async def workflows_post(request: Request):
    """通过共享 Workflow 仓储创建编排。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, _, _ = values
    try:
        form = await request.form()
        if not await _operation_authorized(
            request,
            db,
            principal,
            page_code="workflows",
            component_code="workflow_editor",
            form=form,
        ):
            return _not_found()
        try:
            payload = WorkflowCreate(
                code=str(form.get("code") or ""),
                name=str(form.get("name") or ""),
                description=str(form.get("description") or ""),
                definition=_json_object(form.get("definition")),
                enabled=_as_bool(form.get("enabled"), True),
            )
        except (ValidationError, ValueError):
            return _business_redirect("/admin/workflows", error="invalid")
        if await workflow_repo.get_by_code(db, payload.code):
            return _business_redirect("/admin/workflows", error="duplicate")
        row = await workflow_repo.create(
            db,
            code=payload.code,
            name=payload.name,
            description=payload.description,
            definition=payload.definition.model_dump(
                by_alias=True, exclude_none=True
            ),
            enabled=1 if payload.enabled else 0,
        )
        await audit_principal_action(
            db,
            principal,
            action="workflow.create",
            target_type="workflow",
            target_id=row.code,
            tenant_id=principal.tenant_id,
        )
        await db.commit()
        return _business_redirect("/admin/workflows", notice="created")
    finally:
        await db.close()


@app.post("/admin/workflows/{code}/update")
async def workflows_update(code: str, request: Request):
    """更新 Workflow 名称、启用状态或图定义。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, _, _ = values
    try:
        form = await request.form()
        if not await _operation_authorized(
            request,
            db,
            principal,
            page_code="workflows",
            component_code="workflow_editor",
            form=form,
        ):
            return _not_found()
        if await workflow_repo.get_by_code(db, code) is None:
            return _business_redirect("/admin/workflows", error="missing")
        try:
            payload = WorkflowUpdate(
                name=str(form.get("name") or ""),
                description=str(form.get("description") or ""),
                definition=_json_object(form.get("definition")),
                enabled=_as_bool(form.get("enabled"), True),
            )
        except (ValidationError, ValueError):
            return _business_redirect("/admin/workflows", error="invalid")
        fields = payload.model_dump(exclude_unset=True)
        fields["definition"] = payload.definition.model_dump(
            by_alias=True, exclude_none=True
        )
        fields["enabled"] = 1 if payload.enabled else 0
        await workflow_repo.update(db, code, fields)
        await audit_principal_action(
            db,
            principal,
            action="workflow.update",
            target_type="workflow",
            target_id=code,
            tenant_id=principal.tenant_id,
        )
        await db.commit()
        return _business_redirect("/admin/workflows", notice="updated")
    finally:
        await db.close()


@app.post("/admin/workflows/{code}/toggle")
async def workflows_toggle(code: str, request: Request):
    """切换 Workflow 启用状态。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, _, _ = values
    try:
        form = await request.form()
        if not await _operation_authorized(
            request,
            db,
            principal,
            page_code="workflows",
            component_code="workflow_editor",
            form=form,
        ):
            return _not_found()
        row = await workflow_repo.update(
            db,
            code,
            {"enabled": 1 if _as_bool(form.get("enabled")) else 0},
        )
        if row is None:
            return _business_redirect("/admin/workflows", error="missing")
        await audit_principal_action(
            db,
            principal,
            action="workflow.toggle",
            target_type="workflow",
            target_id=code,
            tenant_id=principal.tenant_id,
            detail={"enabled": bool(row.enabled)},
        )
        await db.commit()
        return _business_redirect("/admin/workflows", notice="toggled")
    finally:
        await db.close()


@app.post("/admin/workflows/{code}/delete")
async def workflows_delete(code: str, request: Request):
    """永久删除 Workflow。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, _, _ = values
    try:
        form = await request.form()
        if not await _operation_authorized(
            request,
            db,
            principal,
            page_code="workflows",
            component_code="workflow_editor",
            form=form,
        ):
            return _not_found()
        if not await workflow_repo.delete(db, code):
            return _business_redirect("/admin/workflows", error="missing")
        await audit_principal_action(
            db,
            principal,
            action="workflow.delete",
            target_type="workflow",
            target_id=code,
            tenant_id=principal.tenant_id,
        )
        await db.commit()
        return _business_redirect("/admin/workflows", notice="deleted")
    finally:
        await db.close()


async def _agent_form_values(db, tenant_id: str, form) -> dict:
    """解析并校验 Agent 表单，同时过滤失效 Skill/MCP 绑定。"""
    skills = [row.code for row in await skill_repo.list(
        db, only_enabled=True, limit=1000, tenant_id=tenant_id
    )]
    mcp_names = await mcp_tool_repo.qualified_names(db, tenant_id=tenant_id)
    tools = normalize_tool_names(_csv(form.get("tools")), mcp_names)
    try:
        normalized_skills = normalize_skill_codes(_csv(form.get("skills")), set(skills))
    except SkillConfigError as exc:
        raise ValueError("invalid_skill_binding") from exc
    return {
        "name": str(form.get("name") or ""),
        "description": str(form.get("description") or ""),
        "system_prompt": str(form.get("system_prompt") or ""),
        "model": str(form.get("model") or ""),
        "base_url": str(form.get("base_url") or ""),
        "api_key": str(form.get("api_key") or ""),
        "tools": tools,
        "skills": [] if normalized_skills is None else normalized_skills,
        "enabled": _as_bool(form.get("enabled"), True),
    }


@app.get("/admin/agents")
async def agents_get(request: Request):
    """渲染当前租户的 Agent 配置列表。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, pref, csrf = values
    try:
        if not await can_access_page(db, principal, "agents"):
            return _not_found()
        flash, _ = _flash_from_query(request)
        return agents_page(
            principal=principal,
            csrf=csrf,
            pages=await visible_pages(db, principal),
            rows=await agent_repo.list(
                db,
                limit=500,
                tenant_id=_tenant_id(principal),
            ),
            theme=pref.theme,
            color_mode=pref.color_mode,
            density=pref.density,
            can_write=await _can_edit_resource(
                db,
                principal,
                page_code="agents",
                component_code="agent_editor",
            ),
            flash=flash,
        )
    finally:
        await db.close()


@app.post("/admin/agents")
async def agents_post(request: Request):
    """通过共享 Agent 仓储创建租户配置。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, _, _ = values
    try:
        form = await request.form()
        if not await _operation_authorized(
            request,
            db,
            principal,
            page_code="agents",
            component_code="agent_editor",
            form=form,
        ):
            return _not_found()
        try:
            fields = await _agent_form_values(db, _tenant_id(principal), form)
            payload = AgentCreate(
                code=str(form.get("code") or ""),
                **fields,
            )
        except (ValidationError, ValueError):
            return _business_redirect("/admin/agents", error="invalid")
        if await agent_repo.get_by_code(db, payload.code):
            return _business_redirect("/admin/agents", error="duplicate")
        row = await agent_repo.create(
            db,
            code=payload.code,
            tenant_id=_tenant_id(principal),
            name=payload.name,
            description=payload.description,
            system_prompt=payload.system_prompt,
            model=payload.model,
            base_url=payload.base_url,
            api_key_cipher=encrypt(payload.api_key),
            tools=payload.tools,
            skills=payload.skills,
            enabled=1 if payload.enabled else 0,
        )
        await audit_principal_action(
            db,
            principal,
            action="agent.create",
            target_type="agent",
            target_id=row.code,
            tenant_id=_tenant_id(principal),
        )
        await db.commit()
        return _business_redirect("/admin/agents", notice="created")
    finally:
        await db.close()


@app.post("/admin/agents/{code}/update")
async def agents_update(code: str, request: Request):
    """更新当前租户的 Agent 配置。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, _, _ = values
    try:
        form = await request.form()
        if not await _operation_authorized(
            request,
            db,
            principal,
            page_code="agents",
            component_code="agent_editor",
            form=form,
        ):
            return _not_found()
        existing = await agent_repo.get_by_code(
            db, code, tenant_id=_tenant_id(principal)
        )
        if existing is None:
            return _business_redirect("/admin/agents", error="missing")
        try:
            raw = await _agent_form_values(db, _tenant_id(principal), form)
            if not raw["api_key"]:
                raw.pop("api_key")
            payload = AgentUpdate(**raw)
        except (ValidationError, ValueError):
            return _business_redirect("/admin/agents", error="invalid")
        fields = payload.model_dump(exclude_unset=True)
        if "api_key" in fields:
            fields["api_key_cipher"] = encrypt(fields.pop("api_key"))
        if "enabled" in fields:
            fields["enabled"] = 1 if fields["enabled"] else 0
        await agent_repo.update(
            db,
            code,
            fields,
            tenant_id=_tenant_id(principal),
        )
        await audit_principal_action(
            db,
            principal,
            action="agent.update",
            target_type="agent",
            target_id=code,
            tenant_id=_tenant_id(principal),
        )
        await db.commit()
        return _business_redirect("/admin/agents", notice="updated")
    finally:
        await db.close()


@app.post("/admin/agents/{code}/toggle")
async def agents_toggle(code: str, request: Request):
    """切换当前租户 Agent 的启用状态。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, _, _ = values
    try:
        form = await request.form()
        if not await _operation_authorized(
            request,
            db,
            principal,
            page_code="agents",
            component_code="agent_editor",
            form=form,
        ):
            return _not_found()
        row = await agent_repo.update(
            db,
            code,
            {"enabled": 1 if _as_bool(form.get("enabled")) else 0},
            tenant_id=_tenant_id(principal),
        )
        if row is None:
            return _business_redirect("/admin/agents", error="missing")
        await audit_principal_action(
            db,
            principal,
            action="agent.toggle",
            target_type="agent",
            target_id=code,
            tenant_id=_tenant_id(principal),
            detail={"enabled": bool(row.enabled)},
        )
        await db.commit()
        return _business_redirect("/admin/agents", notice="toggled")
    finally:
        await db.close()


@app.post("/admin/agents/{code}/delete")
async def agents_delete(code: str, request: Request):
    """删除当前租户的 Agent。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, _, _ = values
    try:
        form = await request.form()
        if not await _operation_authorized(
            request,
            db,
            principal,
            page_code="agents",
            component_code="agent_editor",
            form=form,
        ):
            return _not_found()
        if not await agent_repo.delete(
            db, code, tenant_id=_tenant_id(principal)
        ):
            return _business_redirect("/admin/agents", error="missing")
        await audit_principal_action(
            db,
            principal,
            action="agent.delete",
            target_type="agent",
            target_id=code,
            tenant_id=_tenant_id(principal),
        )
        await db.commit()
        return _business_redirect("/admin/agents", notice="deleted")
    finally:
        await db.close()


@app.get("/admin/skills")
async def skills_get(request: Request):
    """渲染当前租户的技能定义列表。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, pref, csrf = values
    try:
        if not await can_access_page(db, principal, "skills"):
            return _not_found()
        flash, _ = _flash_from_query(request)
        return skills_page(
            principal=principal,
            csrf=csrf,
            pages=await visible_pages(db, principal),
            rows=await skill_repo.list(
                db,
                limit=500,
                tenant_id=_tenant_id(principal),
            ),
            theme=pref.theme,
            color_mode=pref.color_mode,
            density=pref.density,
            can_write=await _can_edit_resource(
                db,
                principal,
                page_code="skills",
                component_code="skill_editor",
            ),
            flash=flash,
        )
    finally:
        await db.close()


@app.post("/admin/skills")
async def skills_post(request: Request):
    """创建手动维护的技能定义。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, _, _ = values
    try:
        form = await request.form()
        if not await _operation_authorized(
            request,
            db,
            principal,
            page_code="skills",
            component_code="skill_editor",
            form=form,
        ):
            return _not_found()
        try:
            payload = SkillCreate(
                code=str(form.get("code") or ""),
                description=str(form.get("description") or ""),
                body=str(form.get("body") or ""),
                status=str(form.get("status") or "published"),
                enabled=_as_bool(form.get("enabled"), True),
            )
        except ValidationError:
            return _business_redirect("/admin/skills", error="invalid")
        tenant_id = _tenant_id(principal)
        if await skill_repo.get_by_code(db, payload.code, tenant_id):
            return _business_redirect("/admin/skills", error="duplicate")
        row = await skill_repo.create(
            db,
            tenant_id=tenant_id,
            code=payload.code,
            name=payload.code,
            description=payload.description,
            body=payload.body,
            status=payload.status,
            enabled=1 if payload.enabled else 0,
            origin="manual",
            content_hash=compute_content_hash(payload.body, []),
        )
        await audit_principal_action(
            db,
            principal,
            action="skill.create",
            target_type="skill",
            target_id=row.code,
            tenant_id=tenant_id,
        )
        await db.commit()
        return _business_redirect("/admin/skills", notice="created")
    finally:
        await db.close()


@app.post("/admin/skills/{code}/update")
async def skills_update(code: str, request: Request):
    """更新技能内容，并把变更写入可回滚的修订表。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, _, _ = values
    try:
        form = await request.form()
        if not await _operation_authorized(
            request,
            db,
            principal,
            page_code="skills",
            component_code="skill_editor",
            form=form,
        ):
            return _not_found()
        tenant_id = _tenant_id(principal)
        existing = await skill_repo.get_by_code(db, code, tenant_id)
        if existing is None:
            return _business_redirect("/admin/skills", error="missing")
        try:
            payload = SkillUpdate(
                description=str(form.get("description") or ""),
                body=str(form.get("body") or ""),
                status=str(form.get("status") or existing.status),
                enabled=_as_bool(form.get("enabled"), bool(existing.enabled)),
            )
        except ValidationError:
            return _business_redirect("/admin/skills", error="invalid")
        resources = await skill_repo.list_resources(db, existing.id)
        await skill_repo.add_revision(
            db,
            existing.id,
            existing.version,
            existing.body or "",
            [
                {
                    "path": item.path,
                    "sha256": hashlib.sha256(item.content).hexdigest(),
                    "is_text": bool(item.is_text),
                    "size_bytes": item.size_bytes,
                }
                for item in resources
            ],
            principal.username,
            "admin web update",
            resource_blobs=[
                {
                    "path": item.path,
                    "mime_type": item.mime_type,
                    "is_text": bool(item.is_text),
                    "content": item.content,
                }
                for item in resources
            ],
        )
        fields = payload.model_dump(exclude_unset=True)
        fields["enabled"] = 1 if fields["enabled"] else 0
        fields["content_hash"] = compute_content_hash(
            payload.body or "",
            [(item.path, item.content) for item in resources],
        )
        await skill_repo.update(
            db,
            code,
            fields,
            tenant_id=tenant_id,
        )
        await audit_principal_action(
            db,
            principal,
            action="skill.update",
            target_type="skill",
            target_id=code,
            tenant_id=tenant_id,
        )
        await db.commit()
        return _business_redirect("/admin/skills", notice="updated")
    finally:
        await db.close()


@app.post("/admin/skills/{code}/archive")
async def skills_archive(code: str, request: Request):
    """归档技能，保留资源和修订以便恢复。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, _, _ = values
    try:
        form = await request.form()
        if not await _operation_authorized(
            request,
            db,
            principal,
            page_code="skills",
            component_code="skill_editor",
            form=form,
        ):
            return _not_found()
        tenant_id = _tenant_id(principal)
        if not await skill_repo.soft_delete(db, code, tenant_id):
            return _business_redirect("/admin/skills", error="missing")
        await audit_principal_action(
            db,
            principal,
            action="skill.archive",
            target_type="skill",
            target_id=code,
            tenant_id=tenant_id,
        )
        await db.commit()
        return _business_redirect("/admin/skills", notice="deleted")
    finally:
        await db.close()


@app.get("/admin/mcp")
async def mcp_get(request: Request):
    """渲染当前租户的 MCP 服务器列表。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, pref, csrf = values
    try:
        if not await can_access_page(db, principal, "mcp"):
            return _not_found()
        flash, _ = _flash_from_query(request)
        return mcp_page(
            principal=principal,
            csrf=csrf,
            pages=await visible_pages(db, principal),
            rows=await mcp_server_repo.list(
                db,
                limit=500,
                tenant_id=_tenant_id(principal),
            ),
            theme=pref.theme,
            color_mode=pref.color_mode,
            density=pref.density,
            can_write=await _can_edit_resource(
                db,
                principal,
                page_code="mcp",
                component_code="mcp_editor",
            ),
            can_refresh=await can_show_component(
                db, principal, "mcp", "mcp_refresh"
            ),
            flash=flash,
        )
    finally:
        await db.close()


async def _validate_mcp_connection(
    transport: str,
    *,
    command: str | None,
    url: str | None,
) -> None:
    """在保存前执行 MCP 命令和 URL 的边界校验。"""
    if transport == "stdio":
        if not command:
            raise ValueError("command_required")
        if url:
            raise ValueError("url_not_allowed_for_stdio")
        check_command_allowed(command)
    elif transport == "http":
        if not url:
            raise ValueError("url_required")
        if command:
            raise ValueError("command_not_allowed_for_http")
        await check_url_allowed(url, require_dns=False)
    else:
        raise ValueError("invalid_transport")


@app.post("/admin/mcp")
async def mcp_post(request: Request):
    """注册当前租户的 MCP 服务器。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, _, _ = values
    try:
        form = await request.form()
        if not await _operation_authorized(
            request,
            db,
            principal,
            page_code="mcp",
            component_code="mcp_editor",
            form=form,
        ):
            return _not_found()
        try:
            payload = McpServerCreate(
                code=str(form.get("code") or ""),
                name=str(form.get("name") or ""),
                transport=str(form.get("transport") or "http"),
                url=str(form.get("url") or "") or None,
                command=str(form.get("command") or "") or None,
                timeout_sec=int(str(form.get("timeout_sec") or "30")),
                enabled=True,
            )
            await _validate_mcp_connection(
                payload.transport,
                command=payload.command,
                url=payload.url,
            )
        except (
            McpTransportError,
            TypeError,
            ValueError,
            ValidationError,
        ):
            return _business_redirect("/admin/mcp", error="invalid")
        tenant_id = _tenant_id(principal)
        if await mcp_server_repo.get_by_code(db, payload.code, tenant_id):
            return _business_redirect("/admin/mcp", error="duplicate")
        row = await mcp_server_repo.create(
            db,
            code=payload.code,
            tenant_id=tenant_id,
            name=payload.name or payload.code,
            transport=payload.transport,
            command=payload.command,
            url=payload.url,
            timeout_sec=payload.timeout_sec,
            auth_type="none",
            enabled=1,
        )
        await audit_principal_action(
            db,
            principal,
            action="mcp.create",
            target_type="mcp_server",
            target_id=row.code,
            tenant_id=tenant_id,
        )
        await db.commit()
        return _business_redirect("/admin/mcp", notice="created")
    finally:
        await db.close()


@app.post("/admin/mcp/{code}/update")
async def mcp_update(code: str, request: Request):
    """更新 MCP 连接名称、端点、命令或超时。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, _, _ = values
    try:
        form = await request.form()
        if not await _operation_authorized(
            request,
            db,
            principal,
            page_code="mcp",
            component_code="mcp_editor",
            form=form,
        ):
            return _not_found()
        tenant_id = _tenant_id(principal)
        existing = await mcp_server_repo.get_by_code(db, code, tenant_id)
        if existing is None:
            return _business_redirect("/admin/mcp", error="missing")
        command = str(form.get("command") or "") or None
        url = str(form.get("url") or "") or None
        try:
            payload = McpServerUpdate(
                name=str(form.get("name") or code),
                timeout_sec=int(str(form.get("timeout_sec") or "30")),
                command=command,
                url=url,
            )
            await _validate_mcp_connection(
                existing.transport,
                command=payload.command,
                url=payload.url,
            )
        except (
            McpTransportError,
            TypeError,
            ValueError,
            ValidationError,
        ):
            return _business_redirect("/admin/mcp", error="invalid")
        await mcp_server_repo.update(
            db,
            code,
            payload.model_dump(exclude_unset=True),
            tenant_id=tenant_id,
        )
        await mcp_tool_repo.replace_for_server(db, existing.id, [])
        await audit_principal_action(
            db,
            principal,
            action="mcp.update",
            target_type="mcp_server",
            target_id=code,
            tenant_id=tenant_id,
        )
        await db.commit()
        return _business_redirect("/admin/mcp", notice="updated")
    finally:
        await db.close()


@app.post("/admin/mcp/{code}/toggle")
async def mcp_toggle(code: str, request: Request):
    """切换 MCP 服务器启用状态。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, _, _ = values
    try:
        form = await request.form()
        if not await _operation_authorized(
            request,
            db,
            principal,
            page_code="mcp",
            component_code="mcp_editor",
            form=form,
        ):
            return _not_found()
        tenant_id = _tenant_id(principal)
        row = await mcp_server_repo.update(
            db,
            code,
            {"enabled": 1 if _as_bool(form.get("enabled")) else 0},
            tenant_id=tenant_id,
        )
        if row is None:
            return _business_redirect("/admin/mcp", error="missing")
        await audit_principal_action(
            db,
            principal,
            action="mcp.toggle",
            target_type="mcp_server",
            target_id=code,
            tenant_id=tenant_id,
            detail={"enabled": bool(row.enabled)},
        )
        await db.commit()
        return _business_redirect("/admin/mcp", notice="toggled")
    finally:
        await db.close()


@app.post("/admin/mcp/{code}/delete")
async def mcp_delete(code: str, request: Request):
    """删除 MCP 服务器及其工具快照。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, _, _ = values
    try:
        form = await request.form()
        if not await _operation_authorized(
            request,
            db,
            principal,
            page_code="mcp",
            component_code="mcp_editor",
            form=form,
        ):
            return _not_found()
        tenant_id = _tenant_id(principal)
        if not await mcp_server_repo.delete(db, code, tenant_id):
            return _business_redirect("/admin/mcp", error="missing")
        await audit_principal_action(
            db,
            principal,
            action="mcp.delete",
            target_type="mcp_server",
            target_id=code,
            tenant_id=tenant_id,
        )
        await db.commit()
        return _business_redirect("/admin/mcp", notice="deleted")
    finally:
        await db.close()


@app.post("/admin/mcp/{code}/refresh")
async def mcp_refresh(code: str, request: Request):
    """刷新 MCP 工具目录快照。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, _, _ = values
    try:
        form = await request.form()
        if not await _operation_authorized(
            request,
            db,
            principal,
            page_code="mcp",
            component_code="mcp_refresh",
            form=form,
        ):
            return _not_found()
        tenant_id = _tenant_id(principal)
        server = await mcp_server_repo.get_by_code(db, code, tenant_id)
        if server is None:
            return _business_redirect("/admin/mcp", error="missing")
        try:
            result = await refresh_catalog(db, server=server)
        except McpCatalogError:
            return _business_redirect("/admin/mcp", error="failed")
        await audit_principal_action(
            db,
            principal,
            action="mcp.refresh",
            target_type="mcp_server",
            target_id=code,
            tenant_id=tenant_id,
            detail={"imported": len(result.imported)},
        )
        await db.commit()
        return _business_redirect("/admin/mcp", notice="refreshed")
    finally:
        await db.close()


@app.get("/admin/llm")
async def llm_get(request: Request):
    """渲染 LLM 接口优先级配置页。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, pref, csrf = values
    try:
        if not await can_access_page(db, principal, "llm"):
            return _not_found()
        flash, _ = _flash_from_query(request)
        return llm_page(
            principal=principal,
            csrf=csrf,
            pages=await visible_pages(db, principal),
            rows=await llm_endpoint_repo.list(db, limit=500),
            theme=pref.theme,
            color_mode=pref.color_mode,
            density=pref.density,
            can_write=await _can_edit_resource(
                db,
                principal,
                page_code="llm",
                component_code="llm_editor",
            ),
            flash=flash,
        )
    finally:
        await db.close()


@app.post("/admin/llm")
async def llm_post(request: Request):
    """新增一个 OpenAI 兼容接口配置。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, _, _ = values
    try:
        form = await request.form()
        if not await _operation_authorized(
            request,
            db,
            principal,
            page_code="llm",
            component_code="llm_editor",
            form=form,
        ):
            return _not_found()
        try:
            payload = LlmEndpointCreate(
                code=str(form.get("code") or ""),
                name=str(form.get("name") or ""),
                base_url=str(form.get("base_url") or ""),
                api_key=str(form.get("api_key") or ""),
                priority=int(str(form.get("priority") or "100")),
            )
        except (TypeError, ValueError, ValidationError):
            return _business_redirect("/admin/llm", error="invalid")
        if await llm_endpoint_repo.get_by_code(db, payload.code):
            return _business_redirect("/admin/llm", error="duplicate")
        row = await llm_endpoint_repo.create(
            db,
            code=payload.code,
            name=payload.name or payload.code,
            base_url=payload.base_url,
            api_key_cipher=encrypt(payload.api_key),
            priority=payload.priority,
            enabled=1,
        )
        await audit_principal_action(
            db,
            principal,
            action="llm.create",
            target_type="llm_endpoint",
            target_id=row.code,
            tenant_id=principal.tenant_id,
        )
        await db.commit()
        return _business_redirect("/admin/llm", notice="created")
    finally:
        await db.close()


@app.post("/admin/llm/{code}/update")
async def llm_update(code: str, request: Request):
    """更新 LLM 接口；空 API Key 表示保留旧密钥。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, _, _ = values
    try:
        form = await request.form()
        if not await _operation_authorized(
            request,
            db,
            principal,
            page_code="llm",
            component_code="llm_editor",
            form=form,
        ):
            return _not_found()
        if await llm_endpoint_repo.get_by_code(db, code) is None:
            return _business_redirect("/admin/llm", error="missing")
        api_key = str(form.get("api_key") or "")
        try:
            values = {
                "name": str(form.get("name") or code),
                "base_url": str(form.get("base_url") or ""),
                "priority": int(str(form.get("priority") or "100")),
            }
            if api_key:
                values["api_key"] = api_key
            payload = LlmEndpointUpdate(
                **values,
            )
        except (TypeError, ValueError, ValidationError):
            return _business_redirect("/admin/llm", error="invalid")
        fields = payload.model_dump(exclude_unset=True)
        if "api_key" in fields:
            fields["api_key_cipher"] = encrypt(fields.pop("api_key"))
        await llm_endpoint_repo.update(db, code, fields)
        await audit_principal_action(
            db,
            principal,
            action="llm.update",
            target_type="llm_endpoint",
            target_id=code,
            tenant_id=principal.tenant_id,
        )
        await db.commit()
        return _business_redirect("/admin/llm", notice="updated")
    finally:
        await db.close()


@app.post("/admin/llm/{code}/toggle")
async def llm_toggle(code: str, request: Request):
    """切换 LLM 接口启用状态。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, _, _ = values
    try:
        form = await request.form()
        if not await _operation_authorized(
            request,
            db,
            principal,
            page_code="llm",
            component_code="llm_editor",
            form=form,
        ):
            return _not_found()
        row = await llm_endpoint_repo.update(
            db,
            code,
            {"enabled": 1 if _as_bool(form.get("enabled")) else 0},
        )
        if row is None:
            return _business_redirect("/admin/llm", error="missing")
        await audit_principal_action(
            db,
            principal,
            action="llm.toggle",
            target_type="llm_endpoint",
            target_id=code,
            tenant_id=principal.tenant_id,
            detail={"enabled": bool(row.enabled)},
        )
        await db.commit()
        return _business_redirect("/admin/llm", notice="toggled")
    finally:
        await db.close()


@app.post("/admin/llm/{code}/delete")
async def llm_delete(code: str, request: Request):
    """删除 LLM 接口配置。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, _, _ = values
    try:
        form = await request.form()
        if not await _operation_authorized(
            request,
            db,
            principal,
            page_code="llm",
            component_code="llm_editor",
            form=form,
        ):
            return _not_found()
        if not await llm_endpoint_repo.delete(db, code):
            return _business_redirect("/admin/llm", error="missing")
        await audit_principal_action(
            db,
            principal,
            action="llm.delete",
            target_type="llm_endpoint",
            target_id=code,
            tenant_id=principal.tenant_id,
        )
        await db.commit()
        return _business_redirect("/admin/llm", notice="deleted")
    finally:
        await db.close()


@app.get("/admin/audit")
async def audit_get(request: Request):
    """按当前租户和固定筛选条件查询工具审计。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, pref, csrf = values
    try:
        if not await can_access_page(db, principal, "audit"):
            return _not_found()
        raw_run_id = request.query_params.get("run_id", "").strip()
        raw_ok = request.query_params.get("ok", "").strip().lower()
        source = request.query_params.get("source", "").strip() or None
        if source not in (None, "builtin", "skill", "mcp"):
            source = None
        ok = True if raw_ok == "true" else False if raw_ok == "false" else None
        try:
            run_id = int(raw_run_id) if raw_run_id else None
        except ValueError:
            run_id = None
        rows, total = await tool_audit_repo.list(
            db,
            tenant_id=_tenant_id(principal),
            run_id=run_id,
            tool_name=request.query_params.get("tool_name", "").strip() or None,
            source=source,
            ok=ok,
            limit=100,
        )
        flash, _ = _flash_from_query(request)
        return audit_page(
            principal=principal,
            csrf=csrf,
            pages=await visible_pages(db, principal),
            rows=rows,
            total=total,
            theme=pref.theme,
            color_mode=pref.color_mode,
            density=pref.density,
            flash=flash,
        )
    finally:
        await db.close()


def _provider_form_values(form) -> dict:
    """解析管理端 OAuth Provider 表单中的非密文字段。"""
    return {
        "display_name": str(form.get("display_name") or ""),
        "issuer": str(form.get("issuer") or ""),
        "discovery_url": str(form.get("discovery_url") or "") or None,
        "authorization_endpoint": (
            str(form.get("authorization_endpoint") or "") or None
        ),
        "token_endpoint": str(form.get("token_endpoint") or "") or None,
        "jwks_uri": str(form.get("jwks_uri") or "") or None,
        "client_id": str(form.get("client_id") or ""),
        "token_auth_method": str(
            form.get("token_auth_method") or "client_secret_basic"
        ),
        "scopes": _csv(form.get("scopes")),
        "username_claim": str(
            form.get("username_claim") or "preferred_username"
        ),
        "role_claim": str(form.get("role_claim") or "groups"),
        "role_claim_required": _as_bool(
            form.get("role_claim_required"), True
        ),
        "role_sync_max_age_seconds": int(
            str(form.get("role_sync_max_age_seconds") or "1800")
        ),
        "auto_create": _as_bool(form.get("auto_create"), True),
        "client_login_enabled": _as_bool(
            form.get("client_login_enabled"), True
        ),
        "admin_login_enabled": _as_bool(
            form.get("admin_login_enabled"), True
        ),
        "enabled": _as_bool(form.get("enabled"), True),
    }


@app.get("/admin/oauth-providers")
async def oauth_providers_get(request: Request):
    """渲染客户端和管理端共用的 OAuth Provider 配置页。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, pref, csrf = values
    try:
        if not await can_access_page(db, principal, "oauth_providers"):
            return _not_found()
        flash, _ = _flash_from_query(request)
        return oauth_providers_page(
            principal=principal,
            csrf=csrf,
            pages=await visible_pages(db, principal),
            providers=await oauth_provider_repo.list(
                db, _tenant_id(principal)
            ),
            theme=pref.theme,
            color_mode=pref.color_mode,
            density=pref.density,
            can_manage=await _can_edit_resource(
                db,
                principal,
                page_code="oauth_providers",
                component_code="provider_editor",
            ),
            flash=flash,
        )
    finally:
        await db.close()


@app.post("/admin/oauth-providers")
async def oauth_providers_post(request: Request):
    """动态新增客户端或管理端 OIDC Provider。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, _, _ = values
    try:
        form = await request.form()
        if not await _operation_authorized(
            request,
            db,
            principal,
            page_code="oauth_providers",
            component_code="provider_editor",
            form=form,
        ):
            return _not_found()
        try:
            fields = _provider_form_values(form)
            payload = OAuthProviderCreate(
                code=str(form.get("code") or ""),
                client_secret=str(form.get("client_secret") or "") or None,
                **fields,
            )
        except (TypeError, ValueError, ValidationError):
            return _business_redirect("/admin/oauth-providers", error="invalid")
        tenant_id = _tenant_id(principal)
        if await oauth_provider_repo.get(db, payload.code, tenant_id):
            return _business_redirect(
                "/admin/oauth-providers", error="duplicate"
            )
        stored = payload.model_dump(
            exclude={"client_secret", "scopes", "code"}
        )
        stored["scopes"] = " ".join(payload.scopes)
        for key in (
            "auto_create",
            "role_claim_required",
            "client_login_enabled",
            "admin_login_enabled",
            "enabled",
        ):
            stored[key] = int(stored[key])
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
        return _business_redirect(
            "/admin/oauth-providers", notice="created"
        )
    finally:
        await db.close()


@app.post("/admin/oauth-providers/{code}/update")
async def oauth_providers_update(code: str, request: Request):
    """更新 Provider 地址、claims、登录用途和 client secret。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, _, _ = values
    try:
        form = await request.form()
        if not await _operation_authorized(
            request,
            db,
            principal,
            page_code="oauth_providers",
            component_code="provider_editor",
            form=form,
        ):
            return _not_found()
        tenant_id = _tenant_id(principal)
        row = await oauth_provider_repo.get(db, code, tenant_id)
        if row is None:
            return _business_redirect(
                "/admin/oauth-providers", error="missing"
            )
        try:
            payload = OAuthProviderUpdate(**_provider_form_values(form))
        except (TypeError, ValueError, ValidationError):
            return _business_redirect("/admin/oauth-providers", error="invalid")
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
        if _as_bool(form.get("clear_client_secret")):
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
        return _business_redirect(
            "/admin/oauth-providers", notice="updated"
        )
    finally:
        await db.close()


@app.post("/admin/oauth-providers/{code}/toggle")
async def oauth_providers_toggle(code: str, request: Request):
    """切换 Provider 总开关，不修改客户端/管理员用途开关。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, _, _ = values
    try:
        form = await request.form()
        if not await _operation_authorized(
            request,
            db,
            principal,
            page_code="oauth_providers",
            component_code="provider_editor",
            form=form,
        ):
            return _not_found()
        tenant_id = _tenant_id(principal)
        row = await oauth_provider_repo.get(db, code, tenant_id)
        if row is None:
            return _business_redirect(
                "/admin/oauth-providers", error="missing"
            )
        await oauth_provider_repo.update(
            db,
            row,
            enabled=1 if _as_bool(form.get("enabled")) else 0,
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
        return _business_redirect(
            "/admin/oauth-providers", notice="toggled"
        )
    finally:
        await db.close()


@app.post("/admin/oauth-providers/{code}/delete")
async def oauth_providers_delete(code: str, request: Request):
    """删除尚未绑定身份或管理员角色的 OAuth Provider。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, _, _ = values
    try:
        form = await request.form()
        if not await _operation_authorized(
            request,
            db,
            principal,
            page_code="oauth_providers",
            component_code="provider_editor",
            form=form,
        ):
            return _not_found()
        tenant_id = _tenant_id(principal)
        row = await oauth_provider_repo.get(db, code, tenant_id)
        if row is None:
            return _business_redirect(
                "/admin/oauth-providers", error="missing"
            )
        try:
            await oauth_provider_repo.delete(db, row)
        except sa.exc.IntegrityError:
            await db.rollback()
            return _business_redirect(
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
        return _business_redirect(
            "/admin/oauth-providers", notice="deleted"
        )
    finally:
        await db.close()


@app.get("/admin/access/pages")
async def access_pages_get(request: Request):
    """页面权限配置页。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, pref, csrf = values
    try:
        if not await can_access_page(db, principal, "access_pages"):
            return _not_found()
        definitions = list(
            await db.scalars(
                sa.select(AdminPageDefinition).order_by(AdminPageDefinition.nav_order)
            )
        )
        statements = sa.select(AdminPagePermission)
        if not principal.is_platform_super_admin:
            statements = statements.where(
                sa.or_(
                    AdminPagePermission.tenant_id.is_(None),
                    AdminPagePermission.tenant_id == principal.tenant_id,
                )
            )
        rows = list(await db.scalars(statements))
        bindings: dict[str, list[AdminPagePermission]] = {}
        for row in rows:
            if row.tenant_id is None or row.tenant_id == principal.tenant_id:
                bindings.setdefault(row.page_code, []).append(row)
        if not principal.is_platform_super_admin:
            # 租户管理员只编辑当前租户的覆盖值，平台 owner 则看到全部作用域。
            for page_code in {row.page_code for row in rows}:
                tenant_rows = [
                    row
                    for row in rows
                    if row.page_code == page_code and row.tenant_id is not None
                ]
                if tenant_rows:
                    bindings[page_code] = tenant_rows
        return access_pages_page(
            principal=principal,
            csrf=csrf,
            pages=await visible_pages(db, principal),
            definitions=definitions,
            bindings=bindings,
            theme=pref.theme,
            color_mode=pref.color_mode,
            density=pref.density,
        )
    finally:
        await db.close()


def _parse_permissions(raw: str) -> list[tuple[str, str]]:
    """解析 `object.action` 逗号列表并拒绝未知权限。"""
    result: list[tuple[str, str]] = []
    for chunk in raw.replace("\n", ",").split(","):
        value = chunk.strip()
        if not value:
            continue
        if "." not in value:
            raise ValueError("invalid_permission")
        object_name, action = value.rsplit(".", 1)
        item = (object_name.strip(), action.strip())
        if item not in KNOWN_PERMISSIONS:
            raise ValueError("unknown_permission")
        result.append(item)
    return sorted(set(result))


async def _may_bind_permissions(
    db, principal: AdminPrincipal, permissions: list[tuple[str, str]]
) -> bool:
    """租户管理员只能绑定自己拥有的普通权限。"""
    if principal.is_platform_super_admin:
        return True
    for object_name, action in permissions:
        if (object_name, action) not in KNOWN_PERMISSIONS:
            return False
        if object_name in {"cross_tenant", "oauth_provider", "platform_admin"}:
            return False
        if not await casbin_service.can(
            db,
            user_id=principal.user_id or "",
            tenant_id=principal.tenant_id or "default",
            object_name=object_name,
            action=action,
        ):
            return False
    return True


def _target_binding_tenant(principal: AdminPrincipal, form) -> str | None:
    """解析并限制页面/组件权限写入的目标租户。"""
    raw = str(form.get("tenant_id") or "").strip()
    if principal.is_platform_super_admin:
        if not raw:
            return None
        if not TENANT_ID_RE.fullmatch(raw):
            raise ValueError("invalid_tenant")
        return raw
    if not principal.tenant_id:
        raise PermissionError("tenant_required")
    if raw and raw != principal.tenant_id:
        raise PermissionError("cross_tenant_forbidden")
    return principal.tenant_id


@app.post("/admin/access/pages/{page_code}/permissions")
async def access_page_permissions_post(page_code: str, request: Request):
    """保存页面权限绑定。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, _, _ = values
    try:
        if not _origin_ok(request):
            return _not_found()
        if not await can_access_page(db, principal, "access_pages"):
            return _not_found()
        form = await request.form()
        if not await _require_csrf(db, request, principal, form):
            return _html_error(
                title_text="Request rejected",
                message="CSRF validation failed.",
                status_code=403,
            )
        definition = await db.get(AdminPageDefinition, page_code)
        if definition is None:
            return _not_found()
        permissions = _parse_permissions(str(form.get("permissions") or ""))
        if page_code in CRITICAL_PAGES and not permissions:
            return _html_error(
                title_text="Invalid policy",
                message="Critical system pages cannot be made inaccessible.",
                status_code=400,
            )
        if not await _may_bind_permissions(db, principal, permissions):
            return _html_error(
                title_text="Forbidden",
                message="You cannot bind permissions outside your own scope.",
                status_code=403,
            )
        try:
            tenant_id = _target_binding_tenant(principal, form)
        except ValueError:
            return _html_error(
                title_text="Invalid policy",
                message="The requested tenant scope is invalid.",
                status_code=400,
            )
        except PermissionError:
            return _html_error(
                title_text="Forbidden",
                message="You cannot change another tenant's UI policy.",
                status_code=403,
            )
        await db.execute(
            sa.delete(AdminPagePermission).where(
                AdminPagePermission.page_code == page_code,
                (
                    AdminPagePermission.tenant_id.is_(None)
                    if tenant_id is None
                    else AdminPagePermission.tenant_id == tenant_id
                ),
            )
        )
        for object_name, action in permissions:
            db.add(
                AdminPagePermission(
                    tenant_id=tenant_id,
                    binding_scope=permission_binding_scope(tenant_id),
                    page_code=page_code,
                    object_name=object_name,
                    action_name=action,
                )
            )
        await audit_principal_action(
            db,
            principal,
            action="admin_ui.page_permissions.update",
            target_type="admin_page",
            target_id=page_code,
            tenant_id=tenant_id,
            detail={
                "scope": permission_binding_scope(tenant_id),
                "permissions": [
                    f"{object_name}.{action}"
                    for object_name, action in permissions
                ],
            },
        )
        await db.commit()
        return RedirectResponse(
            "/admin/access/pages?notice=saved", status_code=303
        )
    finally:
        await db.close()


@app.get("/admin/access/components")
async def access_components_get(request: Request):
    """组件权限配置页。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, pref, csrf = values
    try:
        if not await can_access_page(db, principal, "access_components"):
            return _not_found()
        components = list(
            await db.scalars(
                sa.select(AdminComponentDefinition).order_by(
                    AdminComponentDefinition.page_code,
                    AdminComponentDefinition.sort_order,
                )
            )
        )
        statements = sa.select(AdminComponentPermission)
        if not principal.is_platform_super_admin:
            statements = statements.where(
                sa.or_(
                    AdminComponentPermission.tenant_id.is_(None),
                    AdminComponentPermission.tenant_id == principal.tenant_id,
                )
            )
        rows = list(await db.scalars(statements))
        bindings: dict[tuple[str, str], list[AdminComponentPermission]] = {}
        for row in rows:
            key = (row.page_code, row.component_code)
            bindings.setdefault(key, []).append(row)
        if not principal.is_platform_super_admin:
            for key in set(bindings):
                tenant_rows = [row for row in bindings[key] if row.tenant_id]
                if tenant_rows:
                    bindings[key] = tenant_rows
        return access_components_page(
            principal=principal,
            csrf=csrf,
            pages=await visible_pages(db, principal),
            components=components,
            bindings=bindings,
            theme=pref.theme,
            color_mode=pref.color_mode,
            density=pref.density,
        )
    finally:
        await db.close()


@app.post(
    "/admin/access/components/{page_code}/{component_code}/permissions"
)
async def access_component_permissions_post(
    page_code: str, component_code: str, request: Request
):
    """保存组件权限绑定。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, _, _ = values
    try:
        if not _origin_ok(request) or not await can_access_page(
            db, principal, "access_components"
        ):
            return _not_found()
        form = await request.form()
        if not await _require_csrf(db, request, principal, form):
            return _html_error(
                title_text="Request rejected",
                message="CSRF validation failed.",
                status_code=403,
            )
        component = await db.get(
            AdminComponentDefinition, (page_code, component_code)
        )
        if component is None:
            return _not_found()
        permissions = _parse_permissions(str(form.get("permissions") or ""))
        if not await _may_bind_permissions(db, principal, permissions):
            return _html_error(
                title_text="Forbidden",
                message="You cannot bind permissions outside your own scope.",
                status_code=403,
            )
        try:
            tenant_id = _target_binding_tenant(principal, form)
        except ValueError:
            return _html_error(
                title_text="Invalid policy",
                message="The requested tenant scope is invalid.",
                status_code=400,
            )
        except PermissionError:
            return _html_error(
                title_text="Forbidden",
                message="You cannot change another tenant's UI policy.",
                status_code=403,
            )
        await db.execute(
            sa.delete(AdminComponentPermission).where(
                AdminComponentPermission.page_code == page_code,
                AdminComponentPermission.component_code == component_code,
                (
                    AdminComponentPermission.tenant_id.is_(None)
                    if tenant_id is None
                    else AdminComponentPermission.tenant_id == tenant_id
                ),
            )
        )
        for object_name, action in permissions:
            db.add(
                AdminComponentPermission(
                    tenant_id=tenant_id,
                    binding_scope=permission_binding_scope(tenant_id),
                    page_code=page_code,
                    component_code=component_code,
                    object_name=object_name,
                    action_name=action,
                )
            )
        await audit_principal_action(
            db,
            principal,
            action="admin_ui.component_permissions.update",
            target_type="admin_component",
            target_id=f"{page_code}/{component_code}",
            tenant_id=tenant_id,
            detail={
                "scope": permission_binding_scope(tenant_id),
                "permissions": [
                    f"{object_name}.{action}"
                    for object_name, action in permissions
                ],
            },
        )
        await db.commit()
        return RedirectResponse(
            "/admin/access/components?notice=saved", status_code=303
        )
    finally:
        await db.close()


@app.get("/admin/access/roles")
async def access_roles_get(request: Request):
    """Casbin policy 只读页面。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, pref, csrf = values
    try:
        if not await can_access_page(db, principal, "access_roles"):
            return _not_found()
        return roles_page(
            principal=principal,
            csrf=csrf,
            pages=await visible_pages(db, principal),
            rules=await casbin_service.list_policies(
                db,
                tenant_id=(
                    None
                    if principal.is_platform_super_admin
                    else principal.tenant_id
                ),
            ),
            theme=pref.theme,
            color_mode=pref.color_mode,
            density=pref.density,
        )
    finally:
        await db.close()


@app.get("/admin/access/mappings")
async def access_mappings_get(request: Request):
    """OAuth 角色映射页。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, pref, csrf = values
    try:
        if not await can_access_page(db, principal, "access_mappings"):
            return _not_found()
        mapping_query = sa.select(OAuthRoleMapping).join(
            OAuthProvider, OAuthProvider.id == OAuthRoleMapping.provider_id
        )
        provider_query = sa.select(OAuthProvider).where(
            OAuthProvider.admin_login_enabled == 1
        )
        if not principal.is_platform_super_admin:
            mapping_query = mapping_query.where(
                OAuthProvider.tenant_id == principal.tenant_id
            )
            provider_query = provider_query.where(
                OAuthProvider.tenant_id == principal.tenant_id
            )
        return mappings_page(
            principal=principal,
            csrf=csrf,
            pages=await visible_pages(db, principal),
            mappings=list(
                await db.scalars(
                    mapping_query.order_by(OAuthRoleMapping.id)
                )
            ),
            providers=list(
                await db.scalars(provider_query.order_by(OAuthProvider.code))
            ),
            theme=pref.theme,
            color_mode=pref.color_mode,
            density=pref.density,
        )
    finally:
        await db.close()


@app.post("/admin/access/mappings")
async def access_mappings_post(request: Request):
    """新增 OAuth 外部角色映射。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, _, _ = values
    try:
        if not _origin_ok(request) or not await can_access_page(
            db, principal, "access_mappings"
        ):
            return _not_found()
        form = await request.form()
        if not await _require_csrf(db, request, principal, form):
            return _not_found()
        try:
            provider_id = int(str(form.get("provider_id") or ""))
        except ValueError:
            return _not_found()
        provider = await db.get(OAuthProvider, provider_id)
        if provider is None or (
            not principal.is_platform_super_admin
            and provider.tenant_id != principal.tenant_id
        ) or provider.admin_login_enabled != 1:
            return _not_found()
        claim_name = str(form.get("claim_name") or "groups").strip()
        external_value = str(form.get("external_value") or "").strip()
        role_code = str(form.get("role_code") or "").strip()
        if not external_value or not role_code:
            return RedirectResponse(
                "/admin/access/mappings?error=missing_fields", status_code=303
            )
        role_exists = await db.scalar(
            sa.select(CasbinRule.id).where(
                CasbinRule.ptype == "p",
                CasbinRule.v0 == f"role:{role_code}",
                CasbinRule.v1.in_(
                    (
                        f"tenant:{provider.tenant_id}",
                        "tenant:*",
                        "*",
                    )
                ),
            )
        )
        if role_exists is None:
            return RedirectResponse(
                "/admin/access/mappings?error=unknown_role", status_code=303
            )
        if await db.scalar(
            sa.select(OAuthRoleMapping.id).where(
                OAuthRoleMapping.provider_id == provider_id,
                OAuthRoleMapping.claim_name == claim_name,
                OAuthRoleMapping.external_value == external_value,
            )
        ):
            return RedirectResponse(
                "/admin/access/mappings?error=already_exists", status_code=303
            )
        db.add(
            OAuthRoleMapping(
                provider_id=provider_id,
                claim_name=claim_name,
                external_value=external_value,
                role_code=role_code,
            )
        )
        await audit_principal_action(
            db,
            principal,
            action="casbin.oauth_mapping.create",
            target_type="oauth_role_mapping",
            target_id=f"{provider_id}:{claim_name}:{external_value}",
            tenant_id=provider.tenant_id,
            detail={"role_code": role_code},
        )
        await db.commit()
        return RedirectResponse(
            "/admin/access/mappings?notice=saved", status_code=303
        )
    finally:
        await db.close()


@app.get("/admin/platform-admins")
async def platform_admins_get(request: Request):
    """owner 专属平台超级管理员页。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, pref, csrf = values
    try:
        if not principal.is_platform_super_admin or not principal.is_owner:
            return _not_found()
        return platform_admins_page(
            principal=principal,
            csrf=csrf,
            pages=await visible_pages(db, principal),
            accounts=list(
                await db.scalars(
                    sa.select(SuperAdminAccount).order_by(SuperAdminAccount.id)
                )
            ),
            theme=pref.theme,
            color_mode=pref.color_mode,
            density=pref.density,
        )
    finally:
        await db.close()


@app.post("/admin/platform-admins")
async def platform_admins_post(request: Request):
    """owner 创建平台超级管理员并发送随机密码。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, _, _ = values
    try:
        if (
            not _origin_ok(request)
            or not principal.is_platform_super_admin
            or not principal.is_owner
        ):
            return _not_found()
        form = await request.form()
        if not await _require_csrf(db, request, principal, form):
            return _not_found()
        raw_username = str(form.get("username") or "").strip()
        raw_email = str(form.get("email") or "").strip()
        raw_phone = str(form.get("phone") or "").strip()
        username = normalize_super_admin_username(raw_username) or ""
        email = normalize_email(raw_email) if raw_email else None
        phone = normalize_phone(raw_phone) if raw_phone else None
        if (
            not username
            or not (email or phone)
            or (raw_email and email is None)
            or (raw_phone and phone is None)
        ):
            return RedirectResponse(
                "/admin/platform-admins?error=invalid_fields", status_code=303
            )
        try:
            account, _ = await create_super_admin_account(
                db,
                username=username,
                email=email,
                phone=phone,
                created_by_id=principal.principal_id,
                password_sender=notify_password,
            )
        except LoginError as exc:
            code = (
                "username_exists"
                if str(exc) == "super_admin_exists"
                else "notification_failed"
            )
            return RedirectResponse(
                f"/admin/platform-admins?error={code}", status_code=303
            )
        except ValueError:
            return RedirectResponse(
                "/admin/platform-admins?error=invalid_fields", status_code=303
            )
        await audit_principal_action(
            db,
            principal,
            action="platform_admin.account.create",
            target_type="super_admin_account",
            target_id=str(account.id),
            detail={"target_username": account.username},
        )
        await db.commit()
        return RedirectResponse(
            "/admin/platform-admins?notice=created", status_code=303
        )
    finally:
        await db.close()


@app.post("/admin/platform-admins/{account_id}/delete")
async def platform_admins_delete(account_id: int, request: Request):
    """owner 删除平台超级管理员。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, _, _ = values
    try:
        if (
            not _origin_ok(request)
            or not principal.is_platform_super_admin
            or not principal.is_owner
        ):
            return _not_found()
        form = await request.form()
        if not await _require_csrf(db, request, principal, form):
            return _not_found()
        account = await db.get(SuperAdminAccount, account_id)
        if account is None:
            return _not_found()
        if account.owner_slot == "superAdmin" or account.id == principal.principal_id:
            return RedirectResponse(
                "/admin/platform-admins?error=owner_protected", status_code=303
            )
        await audit_principal_action(
            db,
            principal,
            action="platform_admin.account.delete",
            target_type="super_admin_account",
            target_id=str(account.id),
            detail={"target_username": account.username},
        )
        await revoke_super_admin_sessions(db, account.id)
        await db.delete(account)
        await db.commit()
        return RedirectResponse(
            "/admin/platform-admins?notice=deleted", status_code=303
        )
    finally:
        await db.close()


@app.post("/admin/super-admin/forgot-password")
async def forgot_password(request: Request):
    """向数据库已登记渠道发送随机新密码，响应不泄露账号状态。"""
    if not _origin_ok(request):
        return _login_redirect("invalid_origin")
    form = await request.form()
    username = str(form.get("username") or "").strip()
    async with AdminSessionLocal() as db:
        outcome = "accepted"
        reason = "account_missing"
        account = await db.scalar(
            sa.select(SuperAdminAccount).where(
                SuperAdminAccount.username == username,
                SuperAdminAccount.status == "active",
            )
        )
        if account is not None and (account.email or account.phone):
            try:
                await reset_super_admin_password(
                    db,
                    account,
                    reason="forgot_password",
                    requested_ip_hash=hashlib.sha256(
                        (request.client.host if request.client else "").encode()
                    ).hexdigest(),
                    password_sender=notify_password,
                )
                reason = "password_resent"
            except LoginError as exc:
                reason = str(exc)
                if reason == "reset_rate_limited":
                    outcome = "rate_limited"
        await record_admin_audit(
            db,
            actor_kind="anonymous",
            actor_id=None,
            actor_username=username,
            action="platform_admin.password.forgot",
            target_type="super_admin_account",
            target_id=username,
            outcome=outcome,
            detail={"reason": reason},
        )
        await db.commit()
    return RedirectResponse(
        "/admin/login?notice=If+the+account+exists%2C+a+new+password+was+sent",
        status_code=303,
    )


@app.post("/admin/super-admin/reset-my-password")
async def reset_my_password(request: Request):
    """平台超级管理员重置自己的随机密码。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, _, _ = values
    try:
        if not principal.is_platform_super_admin or not _origin_ok(request):
            return _not_found()
        form = await request.form()
        if not await _require_csrf(db, request, principal, form):
            return _not_found()
        account = await db.get(SuperAdminAccount, principal.principal_id)
        if account is None:
            return _not_found()
        try:
            _, delivery = await reset_super_admin_password(
                db,
                account,
                reason="manual_reset",
                password_sender=notify_password,
            )
        except LoginError as exc:
            await audit_principal_action(
                db,
                principal,
                action="platform_admin.password.reset_self",
                target_type="super_admin_account",
                target_id=str(account.id),
                outcome="denied",
                detail={"code": str(exc)},
            )
            await db.commit()
            return RedirectResponse(
                "/admin/preferences?error=reset_rate_limited", status_code=303
            )
        response = RedirectResponse(
            "/admin/login?notice=password_resent"
            if delivery.delivered
            else "/admin/preferences?error=notification_failed",
            status_code=303,
        )
        await audit_principal_action(
            db,
            principal,
            action="platform_admin.password.reset_self",
            target_type="super_admin_account",
            target_id=str(account.id),
            outcome="success" if delivery.delivered else "failed",
        )
        await db.commit()
        if delivery.delivered:
            clear_super_admin_cookies(response)
        return response
    finally:
        await db.close()


@app.get("/admin/preferences")
async def preferences_get(request: Request):
    """主题与外观偏好页。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, pref, csrf = values
    try:
        if not await can_access_page(db, principal, "preferences"):
            return _not_found()
        return preferences_page(
            principal=principal,
            csrf=csrf,
            pages=await visible_pages(db, principal),
            theme=pref.theme,
            color_mode=pref.color_mode,
            density=pref.density,
        )
    finally:
        await db.close()


@app.post("/admin/preferences")
async def preferences_post(request: Request):
    """保存主题、色温和密度。"""
    values = await _with_auth(request)
    if values is None:
        return _unauthorized()
    db, principal, pref, _ = values
    try:
        if not _origin_ok(request) or not await can_access_page(
            db, principal, "preferences"
        ):
            return _not_found()
        form = await request.form()
        if not await _require_csrf(db, request, principal, form):
            return _not_found()
        theme = str(form.get("theme") or "fluent")
        color_mode = str(form.get("color_mode") or "system")
        density = str(form.get("density") or "comfortable")
        if (
            theme not in ALLOWED_THEMES
            or color_mode not in ALLOWED_COLOR_MODES
            or density not in ALLOWED_DENSITIES
        ):
            return _not_found()
        pref.theme = theme
        pref.color_mode = color_mode
        pref.density = density
        pref.version = (pref.version or 1) + 1
        await audit_principal_action(
            db,
            principal,
            action="admin_ui.preferences.update",
            target_type="admin_ui_preference",
            target_id=f"{pref.principal_kind}:{pref.principal_id}",
            tenant_id=principal.tenant_id,
            detail={
                "theme": theme,
                "color_mode": color_mode,
                "density": density,
            },
        )
        await db.commit()
        return RedirectResponse("/admin/preferences?notice=saved", status_code=303)
    finally:
        await db.close()
