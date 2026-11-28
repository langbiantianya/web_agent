"""管理面 JSON API —— 角色与 OAuth 映射（V4.10）。

锁住四件事：**重复角色是 409**、**循环继承要挡住**、**被引用的角色不能删**、
以及**同供应商下的重复映射是 409**。
"""

from __future__ import annotations

import uuid

import httpx

from app.models.oauth import OAuthProvider
from services.admin_web.accounts import create_super_admin_session
from services.admin_web.db import AdminSessionLocal
from services.admin_web.notifications import DeliveryResult
from services.admin_web.registry import seed_registry
from services.admin_web.security import super_csrf_cookie_name, super_session_cookie_name


def _password_sender():
    """构造总是成功的测试通知器。"""

    async def send(*, username, email, phone, password):
        """报告通知已送达。"""
        return DeliveryResult(email_status="sent", sms_status="skipped")

    return send


async def _client() -> httpx.AsyncClient:
    """建立带平台超级管理员会话的 ASGI 客户端。"""
    from services.admin_web.accounts import create_super_admin_account
    from services.admin_web.app import app as admin_asgi_app

    tag = uuid.uuid4().hex[:8]
    async with AdminSessionLocal() as db:
        await seed_registry(db)
        account, _ = await create_super_admin_account(
            db,
            username=f"api_{tag}",
            email=f"api-{tag}@example.com",
            phone=None,
            owner=False,
            password_sender=_password_sender(),
        )
        token, csrf = await create_super_admin_session(db, account)
        await db.commit()
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_asgi_app),
        base_url="http://test",
        follow_redirects=False,
    )
    client.cookies.set(super_session_cookie_name(), token)
    client.cookies.set(super_csrf_cookie_name(), csrf)
    return client


def _headers(client: httpx.AsyncClient) -> dict[str, str]:
    """取 CSRF 头。"""
    return {"x-csrf-token": client.cookies.get(super_csrf_cookie_name()) or ""}


async def test_role_crud_round_trip() -> None:
    """创建 → 列出 → 改权限 → 改继承 → 删除。"""
    client = await _client()
    code = f"role_{uuid.uuid4().hex[:8]}"
    parent = f"parent_{uuid.uuid4().hex[:8]}"
    try:
        for role_code in (parent, code):
            created = await client.post(
                "/admin/api/access/roles",
                headers=_headers(client),
                json={
                    "code": role_code,
                    "domain": "tenant:*",
                    "permissions": ["agent.read"],
                },
            )
            assert created.status_code == 201, created.text

        listed = await client.get("/admin/api/access/roles")
        assert listed.status_code == 200, listed.text
        assert any(row["code"] == code for row in listed.json()["items"])
        assert "tenant:*" in listed.json()["domains"]

        updated = await client.put(
            f"/admin/api/access/roles/{code}",
            headers=_headers(client),
            json={"domain": "tenant:*", "permissions": ["agent.read", "agent.write"]},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["item"]["permissions"] == ["agent.read", "agent.write"]

        inherited = await client.put(
            f"/admin/api/access/roles/{code}/inheritance",
            headers=_headers(client),
            json={"domain": "tenant:*", "parents": [parent]},
        )
        assert inherited.status_code == 200, inherited.text
        assert inherited.json()["item"]["parents"] == [parent]

        removed = await client.delete(
            f"/admin/api/access/roles/{code}?domain=tenant:*",
            headers=_headers(client),
        )
        assert removed.status_code == 200, removed.text
    finally:
        await client.aclose()


async def test_role_rejects_duplicate_and_cycle() -> None:
    """重复角色是 409；循环继承要挡住。"""
    client = await _client()
    code = f"role_{uuid.uuid4().hex[:8]}"
    try:
        payload = {
            "code": code,
            "domain": "tenant:*",
            "permissions": ["agent.read"],
        }
        first = await client.post(
            "/admin/api/access/roles", headers=_headers(client), json=payload
        )
        assert first.status_code == 201, first.text

        duplicate = await client.post(
            "/admin/api/access/roles", headers=_headers(client), json=payload
        )
        assert duplicate.status_code == 409, duplicate.text
        assert duplicate.json()["code"] == "duplicate"

        await client.put(
            f"/admin/api/access/roles/{code}/inheritance",
            headers=_headers(client),
            json={"domain": "tenant:*", "parents": [code]},
        )
        cycle = await client.put(
            f"/admin/api/access/roles/{code}/inheritance",
            headers=_headers(client),
            json={"domain": "tenant:*", "parents": [code]},
        )
        assert cycle.status_code == 400, cycle.text
        assert cycle.json()["code"] == "invalid_input"
    finally:
        await client.aclose()


async def test_role_delete_reports_in_use() -> None:
    """被子角色引用的角色不能删，返回 409 in_use。"""
    client = await _client()
    code = f"role_{uuid.uuid4().hex[:8]}"
    child = f"child_{uuid.uuid4().hex[:8]}"
    try:
        for role_code in (code, child):
            created = await client.post(
                "/admin/api/access/roles",
                headers=_headers(client),
                json={
                    "code": role_code,
                    "domain": "tenant:*",
                    "permissions": ["agent.read"],
                },
            )
            assert created.status_code == 201, created.text
        await client.put(
            f"/admin/api/access/roles/{child}/inheritance",
            headers=_headers(client),
            json={"domain": "tenant:*", "parents": [code]},
        )
        removed = await client.delete(
            f"/admin/api/access/roles/{code}?domain=tenant:*",
            headers=_headers(client),
        )
        assert removed.status_code == 409, removed.text
        assert removed.json()["code"] == "in_use"
    finally:
        await client.aclose()


async def test_mapping_crud_round_trip() -> None:
    """映射的新增、重复检测、改角色与删除。"""
    client = await _client()
    tag = uuid.uuid4().hex[:8]
    role_code = f"role_{tag}"
    async with AdminSessionLocal() as db:
        provider = OAuthProvider(
            tenant_id="default",
            code=f"idp-{tag}",
            display_name="API Mapping IdP",
            issuer=f"https://api-mapping-{tag}.example",
            client_id=f"api-client-{tag}",
            admin_login_enabled=1,
            enabled=1,
        )
        db.add(provider)
        await db.commit()
        await db.refresh(provider)
        provider_id = provider.id
    try:
        created_role = await client.post(
            "/admin/api/access/roles",
            headers=_headers(client),
            json={
                "code": role_code,
                "domain": "tenant:default",
                "permissions": ["agent.read"],
            },
        )
        assert created_role.status_code == 201, created_role.text

        listed = await client.get("/admin/api/access/mappings")
        assert listed.status_code == 200, listed.text
        assert any(
            row["id"] == provider_id for row in listed.json()["providers"]
        )

        created = await client.post(
            "/admin/api/access/mappings",
            headers=_headers(client),
            json={
                "providerId": provider_id,
                "claimName": "groups",
                "externalValue": "ops",
                "roleCode": role_code,
            },
        )
        assert created.status_code == 201, created.text
        mapping_id = created.json()["item"]["id"]

        duplicate = await client.post(
            "/admin/api/access/mappings",
            headers=_headers(client),
            json={
                "providerId": provider_id,
                "claimName": "groups",
                "externalValue": "ops",
                "roleCode": role_code,
            },
        )
        assert duplicate.status_code == 409, duplicate.text

        updated = await client.put(
            f"/admin/api/access/mappings/{mapping_id}",
            headers=_headers(client),
            json={"roleCode": role_code, "enabled": False},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["item"]["enabled"] is False

        removed = await client.delete(
            f"/admin/api/access/mappings/{mapping_id}", headers=_headers(client)
        )
        assert removed.status_code == 200, removed.text
    finally:
        await client.aclose()
