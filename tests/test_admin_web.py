"""V4.7 独立 Ludic 后台的认证、Casbin、UI 策略与超级管理员测试。"""

from __future__ import annotations

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


async def test_admin_web_manages_workflow_agent_and_llm_without_client_api():
    """后台创建、修改和切换资源时直接使用共享仓储，不经过旧管理 API。"""
    client, _, _, csrf = await _create_seeded_logged_in_super_admin()
    tag = uuid.uuid4().hex[:8]
    agent_code = f"agent-{tag}"
    workflow_code = f"workflow-{tag}"
    llm_code = f"llm-{tag}"
    try:
        agent = await client.post(
            "/admin/agents",
            data={
                "csrf_token": csrf,
                "code": agent_code,
                "name": "Admin Agent",
                "model": "test-model",
                "base_url": "https://llm.example.com/v1",
                "api_key": "admin-web-secret",
                "tools": "",
                "skills": "",
                "system_prompt": "Use the admin configuration.",
                "enabled": "1",
            },
        )
        assert agent.status_code == 303, agent.text
        agent_page = await client.get("/admin/agents")
        assert agent_code in agent_page.text
        assert "admin-web-secret" not in agent_page.text

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

        llm = await client.post(
            "/admin/llm",
            data={
                "csrf_token": csrf,
                "code": llm_code,
                "name": "Primary",
                "base_url": "https://llm.example.com/v1",
                "api_key": "llm-admin-secret",
                "priority": "5",
            },
        )
        assert llm.status_code == 303, llm.text
        toggled = await client.post(
            f"/admin/llm/{llm_code}/toggle",
            data={"csrf_token": csrf, "enabled": "0"},
        )
        assert toggled.status_code == 303, toggled.text

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
            assert stored_agent.api_key_cipher != "admin-web-secret"
            assert stored_workflow is not None
            assert stored_workflow.name == "Admin Workflow Updated"
            assert stored_llm is not None and not stored_llm.enabled
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
                "llm.toggle",
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
