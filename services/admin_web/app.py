"""独立 Ludic admin-web ASGI 应用。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import secrets
from contextlib import asynccontextmanager
from datetime import timedelta
from urllib.parse import quote, parse_qsl, urlencode, urlsplit, urlunsplit

import sqlalchemy as sa
from ludic.web import LudicApp, Request
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
from app.core.oidc import (
    OIDCError,
    exchange_code,
    resolve_provider_endpoints,
    verify_id_token,
)
from app.core.user_profile import normalize_email, normalize_phone
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
from app.models.oauth import OAuthLoginAttempt, OAuthProvider
from app.models.run import Run
from services.admin_web.accounts import (
    AdminPrincipal,
    LoginError,
    authenticate_super_admin,
    create_admin_session,
    create_super_admin_account,
    create_super_admin_session,
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
    dashboard,
    error_page,
    login_page,
    mappings_page,
    platform_admins_page,
    preferences_page,
    roles_page,
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
    """初始化 registry/Casbin，并启动密码轮换任务。"""
    async with AdminSessionLocal() as db:
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
        provider_query = sa.select(OAuthProvider)
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
        ):
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
