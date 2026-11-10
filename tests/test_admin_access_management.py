"""后台角色、权限矩阵和 OAuth 映射的可运营性测试。"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from app.models.admin_control import CasbinRule, OAuthRoleMapping
from app.models.oauth import OAuthProvider
from services.admin_web.db import AdminSessionLocal
from services.admin_web.rbac import casbin_service
from tests.test_admin_complete import _logged_in_client

@pytest.mark.asyncio
async def test_role_page_is_an_operational_permission_matrix():
    """角色页必须提供矩阵、继承和受保护的删除入口。"""
    client, _ = await _logged_in_client()
    try:
        response = await client.get("/admin/access/roles")
        assert response.status_code == 200
        text = response.text
        assert "Roles &amp; permissions" in text
        assert 'class="permission-matrix"' in text
        assert "Edit permissions" in text
        assert "Role inheritance" in text
        assert "Save permissions" in text
        assert 'value="tenant_admin"' in text
        assert "Built-in role required by authentication defaults." in text
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_create_update_and_enforce_custom_role_permissions():
    """新角色可创建、原子改权，并立即进入 Casbin 判定。"""
    client, csrf = await _logged_in_client()
    role_code = f"release_{uuid.uuid4().hex[:8]}"
    user_id = f"role-user-{uuid.uuid4().hex[:8]}"
    try:
        created = await client.post(
            "/admin/access/roles",
            data={
                "csrf_token": csrf,
                "role_code": role_code,
                "domain": "tenant:*",
                "permissions": ["admin.read", "workflow.read"],
            },
        )
        assert created.status_code == 303
        assert "notice=created" in created.headers["location"]
        async with AdminSessionLocal() as db:
            rules = list(
                await db.scalars(
                    sa.select(CasbinRule).where(
                        CasbinRule.ptype == "p",
                        CasbinRule.v0 == f"role:{role_code}",
                        CasbinRule.v1 == "tenant:*",
                    )
                )
            )
            assert {(rule.v2, rule.v3) for rule in rules} == {
                ("admin", "read"),
                ("workflow", "read"),
            }
            await casbin_service.replace_user_roles(
                db,
                user_id=user_id,
                tenant_id="default",
                role_codes=[role_code],
            )
            await db.commit()
            assert await casbin_service.can(
                db,
                user_id=user_id,
                tenant_id="default",
                object_name="workflow",
                action="read",
            )

        updated = await client.post(
            f"/admin/access/roles/{role_code}/permissions",
            data={
                "csrf_token": csrf,
                "domain": "tenant:*",
                "permissions": ["admin.read", "agent.write"],
            },
        )
        assert updated.status_code == 303
        async with AdminSessionLocal() as db:
            assert await casbin_service.can(
                db,
                user_id=user_id,
                tenant_id="default",
                object_name="agent",
                action="write",
            )
            assert not await casbin_service.can(
                db,
                user_id=user_id,
                tenant_id="default",
                object_name="workflow",
                action="read",
            )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_role_inheritance_applies_and_cycles_are_rejected():
    """角色继承生效；反向建立继承链形成的环必须被拒绝。"""
    client, csrf = await _logged_in_client()
    tag = uuid.uuid4().hex[:8]
    parent = f"parent_{tag}"
    child = f"child_{tag}"
    user_id = f"inherit-user-{tag}"
    try:
        for code, permission in ((parent, "audit.read"), (child, "agent.read")):
            response = await client.post(
                "/admin/access/roles",
                data={
                    "csrf_token": csrf,
                    "role_code": code,
                    "domain": "tenant:*",
                    "permissions": [permission],
                },
            )
            assert response.status_code == 303
        inherited = await client.post(
            f"/admin/access/roles/{child}/inheritance",
            data={
                "csrf_token": csrf,
                "domain": "tenant:*",
                "parents": [parent],
            },
        )
        assert inherited.status_code == 303
        async with AdminSessionLocal() as db:
            await casbin_service.replace_user_roles(
                db,
                user_id=user_id,
                tenant_id="default",
                role_codes=[child],
            )
            await db.commit()
            assert await casbin_service.can(
                db,
                user_id=user_id,
                tenant_id="default",
                object_name="audit",
                action="read",
            )

        cyclic = await client.post(
            f"/admin/access/roles/{parent}/inheritance",
            data={
                "csrf_token": csrf,
                "domain": "tenant:*",
                "parents": [child],
            },
        )
        assert cyclic.status_code == 303
        assert "error=invalid" in cyclic.headers["location"]
        async with AdminSessionLocal() as db:
            assert await db.scalar(
                sa.select(CasbinRule.id).where(
                    CasbinRule.ptype == "g",
                    CasbinRule.v0 == f"role:{parent}",
                    CasbinRule.v1 == f"role:{child}",
                )
            ) is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_role_delete_protects_builtin_and_assigned_roles():
    """内置、已被用户继承或映射的角色不能删除；未引用角色可以删除。"""
    client, csrf = await _logged_in_client()
    tag = uuid.uuid4().hex[:8]
    assigned = f"assigned_{tag}"
    disposable = f"disposable_{tag}"
    try:
        for code in (assigned, disposable):
            created = await client.post(
                "/admin/access/roles",
                data={
                    "csrf_token": csrf,
                    "role_code": code,
                    "domain": "tenant:*",
                    "permissions": ["admin.read"],
                },
            )
            assert created.status_code == 303
        async with AdminSessionLocal() as db:
            db.add(
                CasbinRule(
                    ptype="g",
                    v0=f"user:protected-{tag}",
                    v1=f"role:{assigned}",
                    v2="tenant:default",
                )
            )
            await db.commit()

        builtin = await client.post(
            "/admin/access/roles/tenant_admin/delete",
            data={"csrf_token": csrf, "domain": "tenant:*"},
        )
        assert builtin.status_code == 303
        assert "error=in_use" in builtin.headers["location"]
        protected = await client.post(
            f"/admin/access/roles/{assigned}/delete",
            data={"csrf_token": csrf, "domain": "tenant:*"},
        )
        assert protected.status_code == 303
        assert "error=in_use" in protected.headers["location"]
        deleted = await client.post(
            f"/admin/access/roles/{disposable}/delete",
            data={"csrf_token": csrf, "domain": "tenant:*"},
        )
        assert deleted.status_code == 303
        assert "notice=deleted" in deleted.headers["location"]
        async with AdminSessionLocal() as db:
            assert await db.scalar(
                sa.select(CasbinRule.id).where(
                    CasbinRule.ptype == "p",
                    CasbinRule.v0 == f"role:{disposable}",
                )
            ) is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_oauth_mapping_can_be_updated_disabled_and_deleted():
    """映射目标角色、启停状态和删除都能在管理页完成。"""
    client, csrf = await _logged_in_client()
    tag = uuid.uuid4().hex[:8]
    async with AdminSessionLocal() as db:
        provider = OAuthProvider(
            tenant_id="default",
            code=f"mapping-{tag}",
            display_name="Mapping Test IdP",
            issuer=f"https://mapping-{tag}.example",
            client_id=f"mapping-client-{tag}",
            admin_login_enabled=1,
            enabled=1,
        )
        db.add(provider)
        await db.commit()
        await db.refresh(provider)
        provider_id = provider.id
    try:
        created = await client.post(
            "/admin/access/mappings",
            data={
                "csrf_token": csrf,
                "provider_id": str(provider_id),
                "claim_name": "groups",
                "external_value": f"release-{tag}",
                "role_code": "viewer",
            },
        )
        assert created.status_code == 303
        async with AdminSessionLocal() as db:
            mapping = await db.scalar(
                sa.select(OAuthRoleMapping).where(
                    OAuthRoleMapping.provider_id == provider_id
                )
            )
            assert mapping is not None
            mapping_id = mapping.id
        updated = await client.post(
            f"/admin/access/mappings/{mapping_id}/update",
            data={
                "csrf_token": csrf,
                "role_code": "operator",
            },
        )
        assert updated.status_code == 303
        async with AdminSessionLocal() as db:
            mapping = await db.get(OAuthRoleMapping, mapping_id)
            assert mapping.role_code == "operator"
            assert mapping.enabled == 0
        deleted = await client.post(
            f"/admin/access/mappings/{mapping_id}/delete",
            data={"csrf_token": csrf},
        )
        assert deleted.status_code == 303
        async with AdminSessionLocal() as db:
            assert await db.get(OAuthRoleMapping, mapping_id) is None
    finally:
        await client.aclose()
