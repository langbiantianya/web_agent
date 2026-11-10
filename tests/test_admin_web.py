"""V4.7 独立 Ludic 后台的认证、Casbin、UI 策略与超级管理员测试。"""

from __future__ import annotations

import json
import logging
import string
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.models.admin_control import (
    AdminAuditEvent,
    AdminPagePermission,
    SuperAdminAccount,
    permission_binding_scope,
)
from app.models.admin_user import AdminUser
from app.models.base import utcnow
from app.models.oauth import OAuthProvider
from app.models.user import AppUser
from services.admin_web.accounts import (
    AdminPrincipal,
    create_admin_session,
    create_super_admin_account,
    create_super_admin_session,
    ensure_owner_super_admin,
    reset_super_admin_password,
    resolve_admin_session,
    resolve_super_admin_session,
    rotate_expired_super_admins,
    verify_session_csrf,
)
from services.admin_web.audit import record_admin_audit
from services.admin_web.db import AdminSessionLocal
from services.admin_web.notifications import DeliveryResult
from services.admin_web.rbac import casbin_service
from services.admin_web.registry import (
    PAGE_SPECS,
    can_access_page,
    can_show_component,
    seed_registry,
)
from services.admin_web.security import (
    admin_session_cookie_name,
    generate_password,
    hash_password,
    super_csrf_cookie_name,
    super_session_cookie_name,
    verify_password,
)


def _password_sender(store: list[str], *, delivered: bool = True):
    """构造记录明文密码的测试通知器；返回值不会进入数据库。"""

    async def send(*, username: str, email, phone, password: str):
        """记录测试密码并返回固定渠道结果。"""
        store.append(password)
        status = "sent" if delivered else "failed"
        return DeliveryResult(email_status=status, sms_status="skipped")

    return send


def _platform_principal(
    *, principal_id: int = 1, owner: bool = True
) -> AdminPrincipal:
    """构造平台超级管理员主体。"""
    return AdminPrincipal(
        kind="platform_super_admin",
        principal_id=principal_id,
        user_id=None,
        tenant_id=None,
        username="superAdmin" if owner else "opsAdmin",
        session_id=1,
        is_owner=owner,
    )


async def _seed_and_create_oauth_admin(
    *,
    roles: list[str],
    tenant_id: str = "default",
) -> tuple[AdminPrincipal, OAuthProvider]:
    """建立可登录的 OAuth 管理员及其 Casbin 角色。"""
    async with AdminSessionLocal() as db:
        await seed_registry(db)
        tag = uuid.uuid4().hex[:10]
        provider = OAuthProvider(
            tenant_id=tenant_id,
            code=f"idp-{tag}",
            display_name="Test IdP",
            issuer=f"https://idp-{tag}.example",
            client_id=f"client-{tag}",
            role_sync_max_age_seconds=1800,
            enabled=1,
        )
        user = AppUser(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            username=f"admin-{tag}",
            status="active",
        )
        db.add_all([provider, user])
        await db.flush()
        admin = AdminUser(
            user_id=user.id,
            provider_id=provider.id,
            tenant_id=tenant_id,
            status="active",
            enabled=1,
            last_role_sync_at=utcnow(),
            version=1,
        )
        db.add(admin)
        await db.flush()
        await casbin_service.replace_user_roles(
            db,
            user_id=user.id,
            tenant_id=tenant_id,
            role_codes=roles,
        )
        await db.commit()
        principal = AdminPrincipal(
            kind="oauth_admin",
            principal_id=admin.id,
            user_id=user.id,
            tenant_id=tenant_id,
            username=user.username,
            session_id=1,
        )
        return principal, provider


def test_admin_login_slides_password_panel_over_oauth_view():
    """账号密码登录默认隐藏，并由独立入口切换到密码重置界面。"""
    from pathlib import Path

    from services.admin_web.views import login_page

    rendered = login_page(providers=[]).to_html()

    def opening_tag(element_id: str) -> str:
        marker = f'id="{element_id}"'
        position = rendered.index(marker)
        start = rendered.rfind("<", 0, position)
        end = rendered.index(">", position)
        return rendered[start : end + 1]

    assert " hidden" in opening_tag("super-admin-login-panel")
    assert " hidden" in opening_tag("super-admin-password-reset-view")
    assert 'class="login-methods login-stage"' in rendered
    assert 'class="login-card primary-login-card"' in rendered
    assert 'class="login-card super-admin-login-card"' in rendered
    assert 'id="toggle-super-admin-login"' in rendered
    assert 'class="btn btn-oauth login-disclosure-button"' in rendered
    assert 'data-admin-login-toggle="super-admin-login-panel"' in rendered
    assert ">Back to OAuth</button>" in rendered
    assert 'data-admin-view-show="super-admin-password-reset-view"' in rendered
    assert 'action="/admin/login/super-admin"' in rendered
    assert rendered.count('action="/admin/super-admin/forgot-password"') == 1

    script = (
        Path(__file__).parents[1]
        / "services"
        / "admin_web"
        / "static"
        / "admin-ui.js"
    ).read_text(encoding="utf-8")
    assert "function bindLoginControls()" in script
    assert 'panel.classList.add("is-active");' in script
    assert 'primaryCard?.classList.add("is-replaced");' in script
    assert 'panel.classList.remove("is-active");' in script
    assert "panel.hidden = true;" in script
    assert "showLoginView(showView, hideView)" in script

def test_eight_character_password_contract_and_argon2id():
    """密码固定 8 位、字符集受限且只保存 Argon2id 摘要。"""
    alphabet = set(string.ascii_letters + string.digits + "_@")
    samples = [generate_password() for _ in range(500)]
    assert all(len(value) == 8 for value in samples)
    assert all(set(value) <= alphabet for value in samples)
    assert all(any(ch.isupper() for ch in value) for value in samples)
    assert all(any(ch.islower() for ch in value) for value in samples)
    assert all(any(ch.isdigit() for ch in value) for value in samples)
    assert all(any(ch in "_@" for ch in value) for value in samples)

    digest = hash_password(samples[0])
    assert digest.startswith("$argon2id$")
    assert digest != samples[0]
    assert verify_password(samples[0], digest)
    assert not verify_password(samples[0] + "x", digest)


async def test_first_install_owner_is_generated_once_without_notification():
    """首次安装生成 owner 密码；已有 owner 时后续启动不再生成。"""
    async with AdminSessionLocal() as db:
        password = await ensure_owner_super_admin(db)
        assert password is not None
        assert len(password) == 8
        owner = await db.scalar(
            sa.select(SuperAdminAccount).where(
                SuperAdminAccount.owner_slot == "superAdmin"
            )
        )
        assert owner is not None
        assert owner.username == "superAdmin"
        assert owner.email is None and owner.phone is None
        assert owner.status == "active"
        assert verify_password(password, owner.password_hash)
        assert await ensure_owner_super_admin(db) is None
        await db.delete(owner)
        await db.commit()


async def test_admin_lifespan_logs_initial_password_only_when_created(
    monkeypatch,
    caplog,
):
    """启动生命周期只记录首次创建 owner 的明文密码。"""
    import services.admin_web.app as admin_web_app

    monkeypatch.setattr(
        admin_web_app,
        "ensure_owner_super_admin",
        AsyncMock(return_value="Abc123@x"),
    )
    with caplog.at_level(logging.WARNING):
        async with admin_web_app.app.router.lifespan_context(
            admin_web_app.app
        ):
            pass
    assert "superAdmin" in caplog.text
    assert "Abc123@x" in caplog.text
    assert "仅本次日志" in caplog.text


async def test_super_admin_is_created_only_after_notification_and_has_no_plaintext():
    """账号激活要求通知成功；数据库不出现通知中的明文密码。"""
    passwords: list[str] = []
    tag = uuid.uuid4().hex[:8]
    async with AdminSessionLocal() as db:
        account, delivery = await create_super_admin_account(
            db,
            username=f"ops_{tag}",
            email=f"ops-{tag}@example.com",
            phone=None,
            password_sender=_password_sender(passwords),
        )
        assert delivery.delivered
        assert account.status == "active"
        assert account.password_hash.startswith("$argon2id$")
        assert passwords[0] not in account.password_hash
        assert verify_password(passwords[0], account.password_hash)

    failed_passwords: list[str] = []
    async with AdminSessionLocal() as db:
        with pytest.raises(Exception, match="notification_failed"):
            await create_super_admin_account(
                db,
                username=f"failed_{tag}",
                email=f"failed-{tag}@example.com",
                phone=None,
                password_sender=_password_sender(
                    failed_passwords, delivered=False
                ),
            )
        assert await db.scalar(
            sa.select(SuperAdminAccount.id).where(
                SuperAdminAccount.username == f"failed_{tag}"
            )
        ) is None


async def test_platform_super_admin_bypasses_casbin_for_pages_and_components():
    """平台超级管理员不需要 g/p role 即可看到全部注册页面和组件。"""
    async with AdminSessionLocal() as db:
        await seed_registry(db)
        principal = _platform_principal()
        for page in PAGE_SPECS:
            assert await can_access_page(db, principal, page.code)
            for component in page.components:
                assert await can_show_component(
                    db, principal, page.code, component.code
                )


async def test_oauth_page_and_component_permissions_are_filtered():
    """OAuth 管理员只获得 Casbin allow 的页面和组件。"""
    principal, _ = await _seed_and_create_oauth_admin(roles=["viewer"])
    async with AdminSessionLocal() as db:
        assert await can_access_page(db, principal, "dashboard")
        assert await can_show_component(
            db, principal, "dashboard", "metrics"
        )
        assert not await can_show_component(
            db, principal, "dashboard", "quick_actions"
        )
        assert not await can_access_page(db, principal, "access_pages")
        assert not await can_access_page(db, principal, "platform_admins")


async def test_oauth_role_hierarchy_matches_design_matrix():
    """tenant_admin 继承 operator/viewer，但没有平台 owner 能力。"""
    principal, _ = await _seed_and_create_oauth_admin(
        roles=["tenant_admin"], tenant_id="tenant-matrix"
    )
    async with AdminSessionLocal() as db:
        assert await casbin_service.can(
            db,
            user_id=principal.user_id or "",
            tenant_id=principal.tenant_id or "",
            object_name="admin",
            action="read",
        )
        assert await casbin_service.can(
            db,
            user_id=principal.user_id or "",
            tenant_id=principal.tenant_id or "",
            object_name="workflow",
            action="write",
        )
        assert await can_access_page(db, principal, "access_pages")
        assert not await can_access_page(db, principal, "platform_admins")


async def test_tenant_ui_override_replaces_global_permission_binding():
    """租户覆盖命中时完全替代全局页面绑定。"""
    principal, _ = await _seed_and_create_oauth_admin(
        roles=["viewer"], tenant_id="tenant-ui"
    )
    async with AdminSessionLocal() as db:
        db.add(
            AdminPagePermission(
                tenant_id="tenant-ui",
                binding_scope=permission_binding_scope("tenant-ui"),
                page_code="preferences",
                object_name="workflow",
                action_name="read",
            )
        )
        await db.commit()
        assert await can_access_page(db, principal, "preferences")
        db.add(
            AdminPagePermission(
                tenant_id="tenant-ui",
                binding_scope=permission_binding_scope("tenant-ui"),
                page_code="dashboard",
                object_name="admin",
                action_name="write",
            )
        )
        await db.commit()
        assert not await can_access_page(db, principal, "dashboard")


async def test_global_permission_binding_unique_constraint_is_enforced():
    """全局绑定的 NULL tenant 通过非空 scope key 参与唯一约束。"""
    async with AdminSessionLocal() as db:
        await seed_registry(db)
        duplicate = AdminPagePermission(
            tenant_id=None,
            binding_scope="global",
            page_code="dashboard",
            object_name="admin",
            action_name="read",
        )
        db.add(duplicate)
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()


async def test_intentionally_empty_ui_policy_is_not_reseeded_on_restart():
    """人工清空非关键页面权限后，下一次启动不能恢复默认绑定。"""
    async with AdminSessionLocal() as db:
        await seed_registry(db)
        await db.execute(
            sa.delete(AdminPagePermission).where(
                AdminPagePermission.page_code == "preferences",
                AdminPagePermission.tenant_id.is_(None),
            )
        )
        await db.commit()
        await seed_registry(db)
        assert await db.scalar(
            sa.select(AdminPagePermission.id).where(
                AdminPagePermission.page_code == "preferences",
                AdminPagePermission.tenant_id.is_(None),
            )
        ) is None


async def test_oauth_admin_session_requires_current_and_fresh_role_sync():
    """角色版本变化或同步过期后，旧 Session 立即失效。"""
    principal, provider = await _seed_and_create_oauth_admin(roles=["viewer"])
    async with AdminSessionLocal() as db:
        token, _ = await create_admin_session(
            db, admin_user_id=principal.principal_id, role_sync_version=1
        )
        await db.commit()
        assert await resolve_admin_session(db, token) is not None

        admin = await db.get(AdminUser, principal.principal_id)
        admin.version += 1
        await db.commit()
        assert await resolve_admin_session(db, token) is None

        token, _ = await create_admin_session(
            db, admin_user_id=admin.id, role_sync_version=admin.version
        )
        await db.commit()
        admin.last_role_sync_at = utcnow() - timedelta(hours=2)
        await db.commit()
        assert await resolve_admin_session(db, token) is None

        admin.last_role_sync_at = utcnow()
        token, _ = await create_admin_session(
            db, admin_user_id=admin.id, role_sync_version=admin.version
        )
        await db.commit()
        provider = await db.get(OAuthProvider, provider.id)
        provider.enabled = 0
        await db.commit()
        assert await resolve_admin_session(db, token) is None


async def test_csrf_requires_database_session_hash():
    """仅伪造一对相同 Cookie/表单值仍不能通过 CSRF。"""
    passwords: list[str] = []
    tag = uuid.uuid4().hex[:8]
    async with AdminSessionLocal() as db:
        account, _ = await create_super_admin_account(
            db,
            username=f"csrf_{tag}",
            email=f"csrf-{tag}@example.com",
            phone=None,
            password_sender=_password_sender(passwords),
        )
        token, csrf = await create_super_admin_session(db, account)
        await db.commit()
        principal = await resolve_super_admin_session(db, token)
        assert principal is not None
        assert await verify_session_csrf(db, principal, csrf)
        assert not await verify_session_csrf(db, principal, "forged-token")


async def test_admin_audit_drops_passwords_and_contact_details():
    """审计只保留操作元数据，不保存密码、CSRF 或联系方式。"""
    async with AdminSessionLocal() as db:
        row = await record_admin_audit(
            db,
            actor_kind="platform_super_admin",
            actor_id=1,
            actor_username="superAdmin",
            action="platform_admin.test",
            target_type="super_admin_account",
            target_id="2",
            detail={
                "password": "Secret_1",
                "csrf_token": "csrf",
                "email": "private@example.com",
                "phone": "+8613800138000",
                "role": "ops",
            },
        )
        await db.commit()
        stored = await db.get(AdminAuditEvent, row.id)
        assert stored is not None
        assert stored.detail == {"role": "ops"}


async def test_password_reset_rotates_hash_and_revokes_old_session():
    """随机重置成功后旧密码与旧 Session 同时失效。"""
    initial_passwords: list[str] = []
    reset_passwords: list[str] = []
    tag = uuid.uuid4().hex[:8]
    async with AdminSessionLocal() as db:
        account, _ = await create_super_admin_account(
            db,
            username=f"reset_{tag}",
            email=f"reset-{tag}@example.com",
            phone=None,
            password_sender=_password_sender(initial_passwords),
        )
        token, _ = await create_super_admin_session(db, account)
        await db.commit()

        reset, delivery = await reset_super_admin_password(
            db,
            account,
            reason="manual_reset",
            password_sender=_password_sender(reset_passwords),
        )
        assert delivery.delivered
        assert reset.status == "activated"
        assert account.password_version == 2
        assert verify_password(reset_passwords[0], account.password_hash)
        assert not verify_password(initial_passwords[0], account.password_hash)
        assert await resolve_super_admin_session(db, token) is None


async def test_scheduled_rotation_runs_and_updates_expired_accounts():
    """到期轮换会生成新密码、发送通知并撤销旧 Session。"""
    initial_passwords: list[str] = []
    rotated_passwords: list[str] = []
    tag = uuid.uuid4().hex[:8]
    async with AdminSessionLocal() as db:
        account, _ = await create_super_admin_account(
            db,
            username=f"rotate_{tag}",
            email=f"rotate-{tag}@example.com",
            phone=None,
            password_sender=_password_sender(initial_passwords),
        )
        account.password_expires_at = utcnow() - timedelta(seconds=1)
        await db.commit()
        count = await rotate_expired_super_admins(
            db,
            password_sender=_password_sender(rotated_passwords),
        )
        assert count == 1
        await db.refresh(account)
        assert account.password_version == 2
        assert verify_password(rotated_passwords[0], account.password_hash)


async def _create_logged_in_super_admin(
    *, owner: bool
) -> tuple[httpx.AsyncClient, int, list[str]]:
    """创建平台账号和带独立 Cookie/CSRF 的 ASGI 客户端。"""
    from services.admin_web.app import app as admin_asgi_app

    passwords: list[str] = []
    tag = uuid.uuid4().hex[:8]
    async with AdminSessionLocal() as db:
        account, _ = await create_super_admin_account(
            db,
            username="superAdmin" if owner else f"ops_{tag}",
            email=f"{'owner' if owner else 'ops'}-{tag}@example.com",
            phone=None,
            owner=owner,
            password_sender=_password_sender(passwords),
        )
        token, csrf = await create_super_admin_session(db, account)
        account_id = account.id
        await db.commit()
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_asgi_app),
        base_url="http://test",
        follow_redirects=False,
    )
    client.cookies.set(super_session_cookie_name(), token)
    client.cookies.set(super_csrf_cookie_name(), csrf)
    return client, account_id, passwords


async def _create_seeded_logged_in_super_admin() -> tuple[
    httpx.AsyncClient, int, list[str], str
]:
    """建立 registry 已同步且持有合法平台 Session 的后台客户端。"""
    async with AdminSessionLocal() as db:
        await seed_registry(db)
    # owner 用户名是全局单例；业务页验证不需要 owner 专属操作，使用独立平台账号
    # 可避免测试顺序依赖，同时仍然验证“平台超级管理员不进入 Casbin”的路径。
    client, account_id, passwords = await _create_logged_in_super_admin(owner=False)
    csrf = client.cookies.get(super_csrf_cookie_name()) or ""
    return client, account_id, passwords, csrf


async def test_only_owner_can_create_and_delete_platform_admins(monkeypatch):
    """owner 专属路由对非 owner 返回 404，且 owner 不能删除自己。"""
    import services.admin_web.app as admin_web_app

    created_passwords: list[str] = []

    async def fake_notify(*, username, email, phone, password):
        """模拟 owner 创建账号时的成功通知。"""
        created_passwords.append(password)
        return DeliveryResult(email_status="sent", sms_status="skipped")

    monkeypatch.setattr(admin_web_app, "notify_password", fake_notify)
    owner_client, owner_id, _ = await _create_logged_in_super_admin(owner=True)
    non_owner_client, _, _ = await _create_logged_in_super_admin(owner=False)
    try:
        csrf = owner_client.cookies.get(super_csrf_cookie_name())
        tag = uuid.uuid4().hex[:8]
        created = await owner_client.post(
            "/admin/platform-admins",
            data={
                "csrf_token": csrf,
                "username": f"managed_{tag}",
                "email": f"managed-{tag}@example.com",
                "phone": "",
            },
        )
        assert created.status_code == 303
        assert len(created_passwords) == 1
        async with AdminSessionLocal() as db:
            audit = await db.scalar(
                sa.select(AdminAuditEvent)
                .where(
                    AdminAuditEvent.action
                    == "platform_admin.account.create"
                )
                .order_by(AdminAuditEvent.id.desc())
            )
            assert audit is not None
            assert audit.actor_username == "superAdmin"

        forbidden = await non_owner_client.post(
            "/admin/platform-admins",
            data={
                "csrf_token": non_owner_client.cookies.get(
                    super_csrf_cookie_name()
                ),
                "username": f"forbidden_{tag}",
                "email": f"forbidden-{tag}@example.com",
                "phone": "",
            },
        )
        assert forbidden.status_code == 404

        self_delete = await owner_client.post(
            f"/admin/platform-admins/{owner_id}/delete",
            data={"csrf_token": csrf},
        )
        assert self_delete.status_code == 303
        assert "owner_protected" in self_delete.headers["location"]
    finally:
        await owner_client.aclose()
        await non_owner_client.aclose()


async def test_business_pages_are_registered_for_platform_super_admin():
    """六类业务管理页都通过独立 admin-web 的页面权限注册表访问。"""
    client, _, _, _ = await _create_seeded_logged_in_super_admin()
    paths = [
        "/admin/workflows",
        "/admin/agents",
        "/admin/skills",
        "/admin/mcp",
        "/admin/llm",
        "/admin/audit",
    ]
    try:
        for path in paths:
            response = await client.get(path)
            assert response.status_code == 200, (path, response.text)
            assert "Admin navigation" in response.text
    finally:
        await client.aclose()


async def test_llm_page_documents_openai_compatible_presets():
    """LLM 页说明走 OpenAI 兼容协议，并给出 DeepSeek/阿里百炼的快捷预设。"""
    client, _, _, _ = await _create_seeded_logged_in_super_admin()
    try:
        page = await client.get("/admin/llm")
        assert page.status_code == 200
        assert "OpenAI-compatible" in page.text
        # 预设下拉框由 admin-ui.js 绑定，页面只负责渲染 data-* 与挂钩属性。
        assert 'data-llm-preset="picker"' in page.text
        for preset in (
            "https://api.deepseek.com/v1",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        ):
            assert preset in page.text
        # 模型框是「下拉候选 + 自由输入」：input 挂 datalist，按钮带探测挂钩。
        assert 'name="model"' in page.text
        assert 'list="llm-model-new"' in page.text
        assert 'data-llm-fetch="llm-model-new"' in page.text
        assert 'data-llm-test="llm-model-new"' in page.text
        script = await client.get("/admin/static/admin-ui.js")
        assert script.status_code == 200
        assert 'select[data-llm-preset]' in script.text
        assert "form.elements.namedItem" in script.text
        assert '"/admin/llm/probe"' in script.text
    finally:
        await client.aclose()


async def test_llm_probe_lists_models_and_runs_minimal_chat(monkeypatch):
    """「获取模型」与「测试连接」共用一个探测接口，且不会把密钥打进结果。"""
    from app.models.admin_control import AdminAuditEvent
    from app.models.llm_endpoint import LlmEndpoint
    from services.admin_web.llm_probe import LlmProbeError, ProbeResult
    # 探测接口的处理器在 ``llm_write`` 里（写操作拆分出来的模块），
    # 桩要打在**真正持有处理器**的模块上，否则打补丁等于没打。
    from services.admin_web.routes import llm_write as llm_routes

    client, _, _, csrf = await _create_seeded_logged_in_super_admin()
    tag = uuid.uuid4().hex[:8]
    code = f"probe-{tag}"
    seen: dict[str, str] = {}

    async def fake_models(base_url: str, api_key: str) -> list[str]:
        seen["models"] = f"{base_url}|{api_key}"
        return ["deepseek-chat", "deepseek-reasoner"]

    async def fake_chat(base_url: str, api_key: str, model: str) -> ProbeResult:
        seen["chat"] = f"{api_key}|{model}"
        return ProbeResult(latency_ms=12, model=model, reply="pong")

    monkeypatch.setattr(llm_routes, "fetch_models", fake_models)
    monkeypatch.setattr(llm_routes, "probe_chat", fake_chat)
    try:
        created = await client.post(
            "/admin/llm",
            data={
                "csrf_token": csrf,
                "code": code,
                "name": "Probe endpoint",
                "base_url": "https://probe.example.com/v1",
                "model": "deepseek-chat",
                "api_key": "probe-secret",
                "priority": "90",
            },
        )
        assert created.status_code == 303, created.text
        async with AdminSessionLocal() as db:
            stored = await db.scalar(
                sa.select(LlmEndpoint).where(LlmEndpoint.code == code)
            )
            assert stored is not None and stored.model == "deepseek-chat"

        # 1) 只列模型：编辑态留空 API Key ⇒ 用库里已存密钥。
        listed = await client.post(
            "/admin/llm/probe",
            data={"csrf_token": csrf, "mode": "models", "code": code},
        )
        assert listed.status_code == 200, listed.text
        assert listed.json()["models"] == ["deepseek-chat", "deepseek-reasoner"]
        assert seen["models"] == "https://probe.example.com/v1|probe-secret"
        assert "probe-secret" not in listed.text

        # 2) 测试连接：拉列表 + 一次最小对话，模型用表单里选的那个。
        tested = await client.post(
            "/admin/llm/probe",
            data={
                "csrf_token": csrf,
                "mode": "test",
                "code": code,
                "model": "deepseek-reasoner",
            },
        )
        assert tested.status_code == 200, tested.text
        body = tested.json()
        assert body["ok"] is True
        assert body["model"] == "deepseek-reasoner"
        assert body["latency_ms"] == 12
        assert body["models"] == ["deepseek-chat", "deepseek-reasoner"]
        assert seen["chat"] == "probe-secret|deepseek-reasoner"

        # 3) 失败也走 JSON，并把原因带出来；审计里同样只留短原因。
        async def failing_models(base_url: str, api_key: str) -> list[str]:
            raise LlmProbeError("GET /models failed: HTTP 401")

        monkeypatch.setattr(llm_routes, "fetch_models", failing_models)
        failed = await client.post(
            "/admin/llm/probe",
            data={"csrf_token": csrf, "mode": "models", "code": code},
        )
        assert failed.status_code == 400
        assert failed.json() == {
            "ok": False,
            "error": "GET /models failed: HTTP 401",
            "models": [],
        }

        # 4) 402/429 只说明额度/限流不够：地址与密钥已通过列表验证，
        #    结果单独标 limited，并把已拿到的模型列表带回去。
        monkeypatch.setattr(llm_routes, "fetch_models", fake_models)

        async def limited_chat(
            base_url: str, api_key: str, model: str
        ) -> ProbeResult:
            raise LlmProbeError(
                "POST /chat/completions failed: HTTP 429 quota", status_code=429
            )

        monkeypatch.setattr(llm_routes, "probe_chat", limited_chat)
        limited = await client.post(
            "/admin/llm/probe",
            data={"csrf_token": csrf, "mode": "test", "code": code},
        )
        assert limited.status_code == 400
        limited_body = limited.json()
        assert limited_body["limited"] is True
        assert limited_body["models"] == ["deepseek-chat", "deepseek-reasoner"]
        assert "429" in limited_body["error"]

        async with AdminSessionLocal() as db:
            outcomes = set(
                await db.scalars(
                    sa.select(AdminAuditEvent.outcome).where(
                        AdminAuditEvent.action == "llm.probe",
                        AdminAuditEvent.target_id == code,
                    )
                )
            )
            assert {"success", "denied"} <= outcomes
    finally:
        await client.aclose()


async def test_admin_appearance_exposes_three_persisted_cloud_themes():
    """外观页提供三套可辨识主题，并持久化主题与明暗模式。"""
    client, _, _, csrf = await _create_seeded_logged_in_super_admin()
    try:
        page = await client.get("/admin/preferences")
        assert page.status_code == 200
        assert 'role="radiogroup"' in page.text
        # 静态资源 URL 必须就是 shell 导出的那份（版本号随脚本一起换），
        # 别把版本号抄进测试——否则每次改前端都要改测试，还容易漏。
        from services.admin_web.views.shell import ADMIN_STYLESHEET, ADMIN_UI_SCRIPT

        assert ADMIN_STYLESHEET in page.text
        assert ADMIN_UI_SCRIPT in page.text
        assert 'data-admin-locale="zh-CN"' in page.text
        assert 'data-admin-locale="en"' in page.text
        assert "brand-mark" not in page.text
        assert "nav-icon" not in page.text
        assert "nav-group-title" in page.text
        assert "Access control" in page.text
        script = await client.get("/admin/static/admin-ui.js")
        assert script.status_code == 200
        assert "web_agent.admin.appearance.v1" in script.text
        assert "web_agent.admin.locale" in script.text
        assert page.text.count('class="theme-preview"') == 3
        assert page.text.count("theme-option theme-option-") == 3
        for theme in ("fluent", "cloudscape", "material"):
            assert f'value="{theme}"' in page.text

        for theme in ("fluent", "cloudscape", "material"):
            for color_mode in ("light", "dark", "system"):
                saved = await client.post(
                    "/admin/preferences",
                    data={
                        "csrf_token": csrf,
                        "theme": theme,
                        "color_mode": color_mode,
                        "density": "compact",
                    },
                )
                assert saved.status_code == 303
                stored = await client.get("/admin/preferences")
                assert stored.status_code == 200
                assert f'data-theme="{theme}"' in stored.text
                assert f'data-color-mode="{color_mode}"' in stored.text
                assert 'data-density="compact"' in stored.text
    finally:
        await client.aclose()


async def test_admin_web_dynamically_scopes_providers_for_client_and_admin():
    """Provider 在后台配置后分别驱动客户端和管理端登录入口。"""
    from app.main import create_app

    client, _, _, csrf = await _create_seeded_logged_in_super_admin()
    tag = uuid.uuid4().hex[:8]
    client_code = f"client-{tag}"
    admin_code = f"admin-{tag}"
    client_name = f"Client IdP {tag}"
    admin_name = f"Admin IdP {tag}"
    base_form = {
        "csrf_token": csrf,
        "issuer": "https://id.example.com",
        "client_id": "configured-client",
        "client_secret": "provider-secret-value",
        "token_auth_method": "client_secret_basic",
        "scopes": "openid profile email",
        "username_claim": "preferred_username",
        "role_claim": "groups",
        "role_claim_required": "1",
        "role_sync_max_age_seconds": "1800",
        "auto_create": "1",
        "enabled": "1",
    }
    try:
        client_only = await client.post(
            "/admin/oauth-providers",
            data={
                **base_form,
                "code": client_code,
                "display_name": client_name,
                "client_login_enabled": "1",
                "admin_login_enabled": "0",
            },
        )
        assert client_only.status_code == 303, client_only.text
        admin_only = await client.post(
            "/admin/oauth-providers",
            data={
                **base_form,
                "code": admin_code,
                "display_name": admin_name,
                "client_login_enabled": "0",
                "admin_login_enabled": "1",
            },
        )
        assert admin_only.status_code == 303, admin_only.text

        async with AdminSessionLocal() as db:
            from app.models.oauth import OAuthProvider

            client_row = await db.scalar(
                sa.select(OAuthProvider).where(OAuthProvider.code == client_code)
            )
            admin_row = await db.scalar(
                sa.select(OAuthProvider).where(OAuthProvider.code == admin_code)
            )
            assert client_row is not None and admin_row is not None
            assert client_row.client_login_enabled == 1
            assert client_row.admin_login_enabled == 0
            assert admin_row.client_login_enabled == 0
            assert admin_row.admin_login_enabled == 1
            assert client_row.client_secret_cipher

        admin_page = await client.get("/admin/oauth-providers")
        assert admin_page.status_code == 200
        assert client_name in admin_page.text
        assert admin_name in admin_page.text
        assert "provider-secret-value" not in admin_page.text

        from services.admin_web.app import app as admin_asgi_app

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=admin_asgi_app),
            base_url="http://test",
            follow_redirects=False,
        ) as anonymous_admin:
            login_page = await anonymous_admin.get("/admin/login")
        assert admin_name in login_page.text
        assert client_name not in login_page.text

        main_app = create_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main_app),
            base_url="http://test",
        ) as main_client:
            providers = await main_client.get("/api/auth/providers")
        assert providers.status_code == 200
        names = {item["display_name"] for item in providers.json()}
        assert client_name in names
        assert admin_name not in names

        updated = await client.post(
            f"/admin/oauth-providers/{client_code}/update",
            data={
                **base_form,
                "display_name": f"{client_name} Updated",
                "client_secret": "",
                "client_login_enabled": "1",
                "admin_login_enabled": "1",
                "clear_client_secret": "1",
            },
        )
        assert updated.status_code == 303, updated.text
        async with AdminSessionLocal() as db:
            from app.models.oauth import OAuthProvider

            updated_row = await db.scalar(
                sa.select(OAuthProvider).where(OAuthProvider.code == client_code)
            )
            assert updated_row is not None
            assert updated_row.admin_login_enabled == 1
            assert updated_row.client_secret_cipher is None
    finally:
        await client.aclose()


async def test_admin_web_clears_provider_endpoint_fields_and_survives_null_scopes():
    """清空 endpoint 必须真正落库，scope 传 null 不能让更新接口 500。"""
    client, _, _, csrf = await _create_seeded_logged_in_super_admin()
    tag = uuid.uuid4().hex[:8]
    code = f"clear-{tag}"
    base_form = {
        "csrf_token": csrf,
        "issuer": "https://id.example.com",
        "client_id": "configured-client",
        "client_secret": "provider-secret-value",
        "token_auth_method": "client_secret_basic",
        "scopes": "openid profile email",
        "username_claim": "preferred_username",
        "role_claim": "groups",
        "role_claim_required": "1",
        "role_sync_max_age_seconds": "1800",
        "auto_create": "1",
        "enabled": "1",
        "client_login_enabled": "1",
        "admin_login_enabled": "1",
    }
    try:
        created = await client.post(
            "/admin/oauth-providers",
            data={
                **base_form,
                "code": code,
                "display_name": "Clearable IdP",
                "discovery_url": "https://id.example.com/.well-known/openid-configuration",
                "authorization_endpoint": "https://id.example.com/authorize",
            },
        )
        assert created.status_code == 303, created.text
        async with AdminSessionLocal() as db:
            row = await db.scalar(
                sa.select(OAuthProvider).where(OAuthProvider.code == code)
            )
            assert row is not None
            assert row.discovery_url
            assert row.authorization_endpoint

        # 表单里清空 endpoint 后必须真正置空，而不是保留旧值。
        updated = await client.post(
            f"/admin/oauth-providers/{code}/update",
            data={
                **base_form,
                "code": code,
                "display_name": "Clearable IdP",
                "discovery_url": "",
                "authorization_endpoint": "",
                "token_endpoint": "",
                "jwks_uri": "",
            },
        )
        assert updated.status_code == 303, updated.text
        async with AdminSessionLocal() as db:
            row = await db.scalar(
                sa.select(OAuthProvider).where(OAuthProvider.code == code)
            )
            assert row is not None
            assert row.discovery_url is None
            assert row.authorization_endpoint is None
            assert row.token_endpoint is None
            assert row.jwks_uri is None
    finally:
        await client.aclose()


async def test_oauth_provider_api_update_accepts_null_scopes():
    """管理 API 显式传 scopes=null 时只清空 scope，不抛 TypeError。"""
    from app.repo.oauth_provider_repo import oauth_provider_repo

    async with AdminSessionLocal() as db:
        await seed_registry(db)
        tag = uuid.uuid4().hex[:8]
        code = f"api-null-{tag}"
        row = await oauth_provider_repo.create(
            db,
            tenant_id="default",
            code=code,
            client_secret=None,
            display_name="API Null Scopes",
            issuer="https://id.example.com",
            client_id="api-client",
            scopes="openid profile",
            username_claim="preferred_username",
            role_claim="groups",
            role_sync_max_age_seconds=1800,
            auto_create=1,
            role_claim_required=1,
            client_login_enabled=1,
            admin_login_enabled=1,
            enabled=1,
        )
        assert row is not None
        try:
            # 直接调用管理 API 路由，确保 null scope 不会变成 500。
            from app.api.routes.oauth_provider import update_provider
            from app.schemas.oauth_provider import OAuthProviderUpdate

            updated = await update_provider(
                code,
                OAuthProviderUpdate(scopes=None),
                db=db,
                admin=AdminPrincipal(
                    kind="platform_super_admin",
                    principal_id=1,
                    user_id=None,
                    tenant_id="default",
                    username="superAdmin",
                    session_id=1,
                ),
            )
            assert updated.scopes == []
            await db.refresh(row)
            assert row.scopes == ""
        finally:
            await db.rollback()


async def test_admin_web_manages_workflow_agent_and_llm_without_client_api():
    """后台创建、修改和切换资源时直接使用共享仓储，不经过旧管理 API。"""
    client, _, _, csrf = await _create_seeded_logged_in_super_admin()
    tag = uuid.uuid4().hex[:8]
    agent_code = f"agent-{tag}"
    workflow_code = f"workflow-{tag}"
    llm_code = f"llm-{tag}"
    try:
        llm = await client.post(
            "/admin/llm",
            data={
                "csrf_token": csrf,
                "code": llm_code,
                "name": "Primary",
                "base_url": "https://llm.example.com/v1",
                "model": "test-model",
                "api_key": "llm-admin-secret",
                "priority": "5",
            },
        )
        assert llm.status_code == 303, llm.text

        agent = await client.post(
            "/admin/agents",
            data={
                "csrf_token": csrf,
                "code": agent_code,
                "name": "Admin Agent",
                "llm_endpoint_code": llm_code,
                "model": "",
                "tools": "",
                "skills": "",
                "system_prompt": "Use the admin configuration.",
                "enabled": "1",
            },
        )
        assert agent.status_code == 303, agent.text
        agent_page = await client.get("/admin/agents")
        assert agent_code in agent_page.text
        assert "llm-admin-secret" not in agent_page.text

        workflow = await client.post(
            "/admin/workflows",
            data={
                "csrf_token": csrf,
                "code": workflow_code,
                "name": "Admin Workflow",
                "description": "created by admin web",
                "definition": (
                    '{"entry":"node","nodes":[{"id":"node",'
                    f'"agentCode":"{agent_code}"}}],"edges":[]}}'
                ),
                "enabled": "1",
            },
        )
        assert workflow.status_code == 303, workflow.text
        updated = await client.post(
            f"/admin/workflows/{workflow_code}/update",
            data={
                "csrf_token": csrf,
                "name": "Admin Workflow Updated",
                "description": "updated",
                "definition": (
                    '{"entry":"node","nodes":[{"id":"node",'
                    f'"agentCode":"{agent_code}"}}],"edges":[]}}'
                ),
                "enabled": "1",
            },
        )
        assert updated.status_code == 303, updated.text

        async with AdminSessionLocal() as db:
            from app.models.agent import Agent
            from app.models.llm_endpoint import LlmEndpoint
            from app.models.workflow import Workflow

            stored_agent = await db.scalar(
                sa.select(Agent).where(Agent.code == agent_code)
            )
            stored_workflow = await db.scalar(
                sa.select(Workflow).where(Workflow.code == workflow_code)
            )
            stored_llm = await db.scalar(
                sa.select(LlmEndpoint).where(LlmEndpoint.code == llm_code)
            )
            assert stored_agent is not None
            assert stored_agent.llm_endpoint_code == llm_code
            assert stored_agent.model == "test-model"
            assert stored_agent.base_url == ""
            assert stored_workflow is not None
            assert stored_workflow.name == "Admin Workflow Updated"
            assert stored_llm is not None and stored_llm.enabled
            audit_actions = set(
                await db.scalars(
                    sa.select(AdminAuditEvent.action).where(
                        AdminAuditEvent.target_id.in_(
                            [agent_code, workflow_code, llm_code]
                        )
                    )
                )
            )
            assert {
                "agent.create",
                "workflow.create",
                "workflow.update",
                "llm.create",
            } <= audit_actions
    finally:
        await client.aclose()


async def test_admin_web_manages_skill_mcp_and_audit_views():
    """Skill、MCP 与审计页通过共享仓储呈现，凭据不回显。"""
    client, _, _, csrf = await _create_seeded_logged_in_super_admin()
    tag = uuid.uuid4().hex[:8]
    skill_code = f"skill-{tag}"
    mcp_code = f"mcp-{tag}"
    try:
        skill = await client.post(
            "/admin/skills",
            data={
                "csrf_token": csrf,
                "code": skill_code,
                "description": "Admin managed skill",
                "body": "Follow these instructions.",
                "status": "draft",
                "enabled": "0",
            },
        )
        assert skill.status_code == 303, skill.text
        skill_update = await client.post(
            f"/admin/skills/{skill_code}/update",
            data={
                "csrf_token": csrf,
                "description": "Published admin skill",
                "body": "Updated instructions.",
                "status": "published",
                "enabled": "1",
            },
        )
        assert skill_update.status_code == 303, skill_update.text

        mcp = await client.post(
            "/admin/mcp",
            data={
                "csrf_token": csrf,
                "code": mcp_code,
                "name": "Remote catalog",
                "transport": "http",
                "url": "https://mcp.example.com/mcp",
                "command": "",
                "timeout_sec": "30",
            },
        )
        assert mcp.status_code == 303, mcp.text

        async with AdminSessionLocal() as db:
            from app.models.skill import Skill, SkillRevision
            from app.models.tool_call_audit import ToolCallAudit
            from app.models.mcp_server import McpServer

            stored_skill = await db.scalar(
                sa.select(Skill).where(Skill.code == skill_code)
            )
            stored_mcp = await db.scalar(
                sa.select(McpServer).where(McpServer.code == mcp_code)
            )
            assert stored_skill is not None and stored_skill.status == "published"
            revision_count = await db.scalar(
                sa.select(sa.func.count())
                .select_from(SkillRevision)
                .where(SkillRevision.skill_id == stored_skill.id)
            )
            assert revision_count == 1
            assert stored_mcp is not None
            db.add(
                ToolCallAudit(
                    tenant_id="default",
                    run_id=987654,
                    node_id="node-a",
                    tool_call_id="call-a",
                    tool_name="safe_tool",
                    source="builtin",
                    ok=1,
                    duration_ms=12,
                    expires_at=utcnow() + timedelta(days=1),
                )
            )
            await db.commit()

        audit = await client.get("/admin/audit?run_id=987654")
        assert audit.status_code == 200
        assert "safe_tool" in audit.text
        assert "987654" in audit.text
    finally:
        await client.aclose()


async def test_session_cookie_names_are_isolated():
    """OAuth 管理员与其他后台/客户端 Session Cookie 名不能重叠。"""
    assert admin_session_cookie_name() != super_session_cookie_name()
    assert admin_session_cookie_name() not in {"wa_session", "__Host-wa_session"}


def test_admin_engine_uses_postgresql_timezone_and_prepare_safety():
    """admin-web 的 PG 连接也固定 UTC，并兼容 PgBouncer 事务池。"""
    from services.admin_web.db import _engine_kwargs

    kwargs = _engine_kwargs(
        "postgresql+psycopg://user:pass@127.0.0.1:5432/admin"
    )
    assert kwargs["connect_args"] == {
        "prepare_threshold": None,
        "options": "-c timezone=UTC",
    }


async def test_admin_workflow_validate_endpoint_reports_graph_issues():
    """校验端点一次返回全部问题，并带出节点/边定位。"""
    client, _, _, csrf = await _create_seeded_logged_in_super_admin()
    try:
        payload = {
            "csrf_token": csrf,
            "definition": {
                "entry": "step1",
                "nodes": [
                    {"id": "step1", "type": "agent", "agentCode": "planner"},
                    {"id": "review", "type": "agent", "agentCode": "writer"},
                    {"id": "lonely", "type": "agent", "agentCode": "writer"},
                ],
                "edges": [
                    {"from": "step1", "to": "review", "when": 'upstream.nope == "x"'},
                ],
            },
        }
        response = await client.post("/admin/workflows/validate", json=payload)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is False
        messages = " ".join(item["message"] for item in body["errors"])
        assert "lonely" in messages
        assert "upstream.nope" in messages
        assert any(item.get("node") == "lonely" for item in body["errors"])
        assert any(
            item.get("edge") == "step1->review" for item in body["errors"]
        )
    finally:
        await client.aclose()


async def test_admin_workflow_validate_endpoint_locates_whole_cycle():
    """成环时端点给出环上**全部**节点与边，画布才能一次标红整个环。

    修复前这条 issue 在响应里既没有 ``node`` 也没有 ``edge``，画布拿不到任何
    定位，管理员只能自己对着句子里的「存在环」猜是哪一段出了问题。
    """
    client, _, _, csrf = await _create_seeded_logged_in_super_admin()
    try:
        response = await client.post(
            "/admin/workflows/validate",
            json={
                "csrf_token": csrf,
                "definition": {
                    "entry": "a",
                    "nodes": [
                        {"id": "a", "type": "agent", "agentCode": "planner"},
                        {"id": "b", "type": "agent", "agentCode": "writer"},
                        {"id": "c", "type": "agent", "agentCode": "writer"},
                    ],
                    "edges": [
                        {"from": "a", "to": "b"},
                        {"from": "b", "to": "c"},
                        {"from": "c", "to": "a"},
                    ],
                },
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is False
        cycle = next(i for i in body["errors"] if "存在环" in i["message"])
        assert cycle["nodes"] == ["a", "b", "c"]
        assert cycle["node"] == "a"
        assert set(cycle["edges"]) == {"a->b", "b->c", "c->a"}
        assert cycle["edge"] == "a->b"
    finally:
        await client.aclose()


async def test_admin_workflow_validate_endpoint_accepts_valid_definition():
    """合法定义返回 ok=true。"""
    client, _, _, csrf = await _create_seeded_logged_in_super_admin()
    try:
        response = await client.post(
            "/admin/workflows/validate",
            json={
                "csrf_token": csrf,
                "definition": {
                    "entry": "a",
                    "nodes": [
                        {"id": "a", "type": "agent", "agentCode": "planner"},
                        {"id": "b", "type": "agent", "agentCode": "writer"},
                    ],
                    "edges": [{"from": "a", "to": "b"}],
                },
            },
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True}
    finally:
        await client.aclose()


async def test_admin_workflow_validate_rejects_malformed_definition():
    """形状不对的负载必须拿到 400，不能让校验端点 500。"""
    client, _, _, csrf = await _create_seeded_logged_in_super_admin()
    try:
        for bad in (
            {"entry": "a", "nodes": {"a": {}}, "edges": []},
            {"entry": "a", "nodes": ["x"], "edges": []},
            {"entry": "a", "nodes": [None], "edges": []},
            {"entry": "a", "nodes": [{"id": "a"}], "edges": ["x"]},
        ):
            response = await client.post(
                "/admin/workflows/validate",
                json={"csrf_token": csrf, "definition": bad},
            )
            assert response.status_code == 400, (bad, response.text)
            assert response.json()["ok"] is False
    finally:
        await client.aclose()


async def test_admin_workflow_validate_reports_node_level_problems():
    """节点自身字段不合法（缺 agentCode）也要报出来，并定位到节点。"""
    client, _, _, csrf = await _create_seeded_logged_in_super_admin()
    try:
        response = await client.post(
            "/admin/workflows/validate",
            json={
                "csrf_token": csrf,
                "definition": {
                    "entry": "step1",
                    "nodes": [{"id": "step1", "type": "agent"}],
                    "edges": [],
                },
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is False
        assert any(
            item.get("node") == "step1" for item in body["errors"]
        ), body
    finally:
        await client.aclose()


async def test_admin_workflow_validate_requires_csrf():
    """缺 CSRF 时拒绝。"""
    client, _, _, _ = await _create_seeded_logged_in_super_admin()
    try:
        response = await client.post(
            "/admin/workflows/validate",
            json={
                "definition": {
                    "entry": "a",
                    "nodes": [{"id": "a", "type": "agent", "agentCode": "p"}],
                    "edges": [],
                }
            },
        )
        assert response.status_code == 403
        assert response.json()["ok"] is False
    finally:
        await client.aclose()


async def test_admin_workflow_validate_rejects_anonymous_client():
    """未登录时回 403 JSON，不能 303 跳登录页（fetch 跟随后拿到的是 HTML）。"""
    from services.admin_web.app import app as admin_asgi_app

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_asgi_app),
        base_url="http://test",
        follow_redirects=False,
    )
    try:
        response = await client.post(
            "/admin/workflows/validate",
            json={
                "definition": {
                    "entry": "a",
                    "nodes": [{"id": "a", "type": "agent", "agentCode": "p"}],
                    "edges": [],
                }
            },
        )
        assert response.status_code == 403, response.text
        body = response.json()
        assert body["ok"] is False
        assert body["errors"][0]["message"] == "forbidden"
    finally:
        await client.aclose()


async def test_admin_workflow_validate_normalizes_pydantic_messages():
    """pydantic 的英文内部文案要归一成中文，并带出字段定位。"""
    client, _, _, csrf = await _create_seeded_logged_in_super_admin()
    try:
        missing_code = await client.post(
            "/admin/workflows/validate",
            json={
                "csrf_token": csrf,
                "definition": {
                    "entry": "step1",
                    "nodes": [{"id": "step1", "type": "agent"}],
                    "edges": [],
                },
            },
        )
        assert missing_code.status_code == 200, missing_code.text
        errors = missing_code.json()["errors"]
        # 我们自己的中文消息保留，但要去掉 pydantic 加的英文前缀
        assert any(
            item["message"]
            == "节点 step1 是 agent 类型，必须指定 agentCode"
            for item in errors
        ), errors
        assert not any(
            "Value error" in item["message"] for item in errors
        ), errors
        assert any(item.get("node") == "step1" for item in errors), errors

        missing_id = await client.post(
            "/admin/workflows/validate",
            json={
                "csrf_token": csrf,
                "definition": {
                    "entry": "a",
                    "nodes": [{"type": "agent", "agentCode": "p"}],
                    "edges": [],
                },
            },
        )
        assert missing_id.status_code == 200, missing_id.text
        errors = missing_id.json()["errors"]
        assert any("缺少必填字段" in item["message"] for item in errors), errors

        extra_field = await client.post(
            "/admin/workflows/validate",
            json={
                "csrf_token": csrf,
                "definition": {
                    "entry": "a",
                    "nodes": [
                        {"id": "a", "type": "agent", "agentCode": "p", "x": 1}
                    ],
                    "edges": [],
                },
            },
        )
        assert extra_field.status_code == 200, extra_field.text
        errors = extra_field.json()["errors"]
        hits = [item for item in errors if "不支持的字段" in item["message"]]
        assert hits, errors
        assert hits[0].get("field") == "a.x", errors
    finally:
        await client.aclose()


async def test_admin_workflow_validate_deduplicates_model_level_graph_issue():
    """模型级条目与图级条目重复时只留带定位的那条。"""
    client, _, _, csrf = await _create_seeded_logged_in_super_admin()
    try:
        response = await client.post(
            "/admin/workflows/validate",
            json={
                "csrf_token": csrf,
                "definition": {
                    "entry": "step1",
                    "nodes": [
                        {"id": "step1", "type": "agent", "agentCode": "planner"},
                        {"id": "lonely", "type": "agent", "agentCode": "writer"},
                    ],
                    "edges": [],
                },
            },
        )
        assert response.status_code == 200, response.text
        errors = response.json()["errors"]
        orphans = [item for item in errors if "孤立节点" in item["message"]]
        assert len(orphans) == 1, errors
        assert orphans[0].get("node") == "lonely", errors
        assert "Value error" not in orphans[0]["message"], errors
    finally:
        await client.aclose()


async def test_admin_workflow_validate_normalizes_fractional_int_message():
    """整数字段收到小数时也不能漏出 pydantic 的英文文案。"""
    client, _, _, csrf = await _create_seeded_logged_in_super_admin()
    try:
        response = await client.post(
            "/admin/workflows/validate",
            json={
                "csrf_token": csrf,
                "definition": {
                    "entry": "a",
                    "nodes": [
                        {
                            "id": "a",
                            "type": "agent",
                            "agentCode": "p",
                            "requiredApprovals": 1.5,
                        }
                    ],
                    "edges": [],
                },
            },
        )
        assert response.status_code == 200, response.text
        errors = response.json()["errors"]
        hits = [
            item
            for item in errors
            if item.get("field") == "a.requiredApprovals"
        ]
        assert hits, errors
        assert hits[0]["message"] == "字段 a.requiredApprovals 必须是整数", errors
        assert hits[0].get("node") == "a", errors
        assert not any(
            "Input should be" in item["message"] for item in errors
        ), errors
    finally:
        await client.aclose()


def test_admin_workflow_validate_shape_messages_are_localized():
    """形状类负载的 pydantic 文案也不能是英文。"""
    from services.admin_web.app import _definition_errors

    for definition in (
        {"entry": "a", "nodes": {"a": {}}, "edges": []},
        {"entry": "a", "nodes": ["x"], "edges": []},
        {"entry": "a", "nodes": [None], "edges": []},
        {"entry": "a", "nodes": [], "edges": {"a": {}}},
    ):
        errors = _definition_errors(definition)
        messages = " ".join(item["message"] for item in errors)
        assert "Input should be a valid list" not in messages, errors
        assert "Input should be a valid dictionary" not in messages, errors


def test_admin_workflow_validate_unknown_error_type_falls_back_to_chinese():
    """未覆盖的 pydantic 错误类型也要给中文，不能把英文原文透出去。"""
    from services.admin_web.app import _pydantic_message

    assert (
        _pydantic_message("some_unknown_type", "Input should be ...", "a.x")
        == "字段 a.x 不合法"
    )
    assert (
        _pydantic_message("some_unknown_type", "Input should be ...", None)
        == "定义不合法"
    )


async def test_admin_workflow_editor_page_renders_canvas():
    """编排页返回画布容器，且不下发任何 client secret 之类的敏感值。"""
    from app.models.workflow import Workflow

    client, _, _, _ = await _create_seeded_logged_in_super_admin()
    tag = uuid.uuid4().hex[:8]
    code = f"wf-{tag}"
    async with AdminSessionLocal() as db:
        db.add(
            Workflow(
                code=code,
                name="Canvas Flow",
                description="",
                definition={
                    "entry": "step1",
                    "nodes": [
                        {
                            "id": "step1",
                            "type": "agent",
                            "label": "意图识别",
                            "agentCode": "planner",
                        }
                    ],
                    "edges": [],
                },
                enabled=1,
            )
        )
        await db.commit()
    try:
        response = await client.get(f"/admin/workflows/{code}")
        assert response.status_code == 200, response.text
        assert "workflow-editor-canvas" in response.text
        assert "workflow-editor-state" in response.text
        assert "意图识别" in response.text
        # 画布页不应出现 client secret / 密码一类的字样
        assert "client_secret" not in response.text
    finally:
        await client.aclose()


async def test_admin_workflow_update_persists_layout_and_label():
    """保存后 layout 坐标与节点 label 必须原样落库。"""
    from app.models.workflow import Workflow

    client, _, _, csrf = await _create_seeded_logged_in_super_admin()
    tag = uuid.uuid4().hex[:8]
    code = f"wf-{tag}"
    async with AdminSessionLocal() as db:
        db.add(
            Workflow(
                code=code,
                name="Layout Flow",
                description="",
                definition={
                    "entry": "step1",
                    "nodes": [
                        {
                            "id": "step1",
                            "type": "agent",
                            "label": "意图识别",
                            "agentCode": "planner",
                        }
                    ],
                    "edges": [],
                },
                enabled=1,
            )
        )
        await db.commit()
    definition = {
        "entry": "step1",
        "inputTemplate": "问：{{user_input}}",
        "nodes": [
            {
                "id": "step1",
                "type": "agent",
                "label": "意图识别",
                "agentCode": "planner",
            }
        ],
        "edges": [],
        "layout": {
            "nodes": {"step1": {"x": 320, "y": 640}},
            "viewport": {"x": 12, "y": 34, "scale": 1.5},
        },
    }
    try:
        updated = await client.post(
            f"/admin/workflows/{code}/update",
            data={
                "csrf_token": csrf,
                "name": "Layout Flow",
                "description": "",
                "enabled": "1",
                "definition": json.dumps(definition, ensure_ascii=False),
                "next": f"/admin/workflows/{code}",
            },
        )
        assert updated.status_code == 303, updated.text
        # 保存成功要回到编排页并带上可见反馈。
        assert updated.headers["location"] == f"/admin/workflows/{code}?notice=updated"
        async with AdminSessionLocal() as db:
            row = await db.scalar(
                sa.select(Workflow).where(Workflow.code == code)
            )
            assert row is not None
            assert row.definition["layout"]["nodes"]["step1"] == {
                "x": 320,
                "y": 640,
            }
            assert row.definition["layout"]["viewport"]["scale"] == 1.5
            assert row.definition["nodes"][0]["label"] == "意图识别"
    finally:
        await client.aclose()


async def test_admin_workflow_editor_page_missing_workflow_returns_404():
    """不存在的编排返回 404，不泄露存在性差异。"""
    client, _, _, _ = await _create_seeded_logged_in_super_admin()
    try:
        response = await client.get("/admin/workflows/does-not-exist")
        assert response.status_code == 404
    finally:
        await client.aclose()


async def test_admin_workflow_create_accepts_agent_code_and_redirects_to_editor():
    """创建表单不再收 JSON，改收初始 Agent code。"""
    from app.models.agent import Agent

    client, _, _, csrf = await _create_seeded_logged_in_super_admin()
    tag = uuid.uuid4().hex[:8]
    agent_code = f"ag-{tag}"
    code = f"wf-{tag}"
    async with AdminSessionLocal() as db:
        db.add(
            Agent(
                tenant_id="default",
                code=agent_code,
                name="Planner",
                model="test-model",
                base_url="https://llm.example.com/v1",
                api_key_cipher="",
                system_prompt="p",
                tools=[],
                skills=[],
                enabled=1,
            )
        )
        await db.commit()
    try:
        created = await client.post(
            "/admin/workflows",
            data={
                "csrf_token": csrf,
                "code": code,
                "name": "New Flow",
                "description": "",
                "agent_code": agent_code,
                "enabled": "1",
            },
        )
        assert created.status_code == 303, created.text
        # 建完直接进编排页，并带上可见反馈。
        assert created.headers["location"] == f"/admin/workflows/{code}?notice=created"
        async with AdminSessionLocal() as db:
            from app.models.workflow import Workflow

            row = await db.scalar(
                sa.select(Workflow).where(Workflow.code == code)
            )
            assert row is not None
            assert row.definition["nodes"][0]["agentCode"] == agent_code
            assert row.definition["nodes"][0]["id"] == "step1"
    finally:
        await client.aclose()


async def test_admin_workflow_editor_shows_save_feedback():
    """保存后编排页要看得见反馈：notice 查询值翻成 flash 文案。

    改造前保存成功后的 303 不带 notice=，编排页也不消费 flash，
    于是用户点完「保存」看不到任何提示。
    """
    client, _, _, csrf = await _create_seeded_logged_in_super_admin()
    tag = uuid.uuid4().hex[:8]
    code = f"wf-{tag}"
    try:
        created = await client.post(
            "/admin/workflows",
            data={
                "csrf_token": csrf,
                "code": code,
                "name": "Feedback Flow",
                "description": "",
                "definition": json.dumps(
                    {
                        "entry": "step1",
                        "nodes": [
                            {"id": "step1", "type": "agent", "agentCode": "a"},
                        ],
                        "edges": [],
                    }
                ),
                "enabled": "1",
            },
        )
        assert created.status_code == 303, created.text

        # 创建后带 notice=created 打开编排页 → 页面出现创建提示。
        page = await client.get(f"/admin/workflows/{code}?notice=created")
        assert page.status_code == 200, page.text
        assert "Resource created." in page.text

        # 保存后带 notice=updated 打开编排页 → 页面出现保存提示。
        page = await client.get(f"/admin/workflows/{code}?notice=updated")
        assert page.status_code == 200, page.text
        assert "Changes saved." in page.text

        # 任意文本不会被回显：白名单外的值不产生提示。
        page = await client.get(f"/admin/workflows/{code}?notice=<script>x</script>")
        assert page.status_code == 200, page.text
        assert "<script>x</script>" not in page.text
    finally:
        await client.aclose()
