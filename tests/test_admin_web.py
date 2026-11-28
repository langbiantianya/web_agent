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


