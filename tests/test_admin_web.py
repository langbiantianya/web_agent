"""V4.7 独立 Ludic 后台的认证、Casbin、UI 策略与超级管理员测试。"""

from __future__ import annotations

import string
import uuid
from datetime import timedelta

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
