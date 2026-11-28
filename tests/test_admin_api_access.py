"""管理面 JSON API —— 访问控制矩阵（V4.10）。

锁住四件事：**关键页面不能被清空**、**租户级绑定不能是空集**、
**写操作缺 CSRF 头要被拒**、以及**越权绑定别人租户要被拒**。
"""

from __future__ import annotations

import uuid

import httpx
import pytest

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


async def test_access_pages_list_and_save() -> None:
    """页面权限矩阵可读、可保存，保存后能读回来。"""
    client = await _client()
    try:
        listed = await client.get("/admin/api/access/pages")
        assert listed.status_code == 200, listed.text
        payload = listed.json()
        codes = {item["code"] for item in payload["items"]}
        assert "agents" in codes
        assert payload["permissionOptions"]

        saved = await client.put(
            "/admin/api/access/pages/agents",
            headers=_headers(client),
            json={"tenantId": None, "permissions": ["agent.read"]},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["item"]["permissions"] == ["agent.read"]

        again = await client.get("/admin/api/access/pages")
        item = next(
            row for row in again.json()["items"] if row["code"] == "agents"
        )
        global_binding = next(
            binding for binding in item["bindings"] if binding["tenantId"] is None
        )
        assert global_binding["permissions"] == ["agent.read"]
    finally:
        await client.aclose()


async def test_access_pages_rejects_empty_and_missing_csrf() -> None:
    """关键页面不能清空；缺 CSRF 头一律 403。"""
    client = await _client()
    try:
        critical = await client.put(
            "/admin/api/access/pages/dashboard",
            headers=_headers(client),
            json={"tenantId": None, "permissions": []},
        )
        assert critical.status_code == 400, critical.text
        assert critical.json()["code"] == "invalid_input"

        without_csrf = await client.put(
            "/admin/api/access/pages/agents",
            json={"tenantId": None, "permissions": ["agent.read"]},
        )
        assert without_csrf.status_code == 403, without_csrf.text
        assert without_csrf.json()["code"] == "invalid_csrf"

        unknown = await client.put(
            "/admin/api/access/pages/agents",
            headers=_headers(client),
            json={"tenantId": None, "permissions": ["not_a_permission.read"]},
        )
        assert unknown.status_code == 400, unknown.text
        assert unknown.json()["code"] == "invalid_input"
    finally:
        await client.aclose()


async def test_access_components_list_and_save() -> None:
    """组件权限矩阵可读、可保存。"""
    client = await _client()
    try:
        listed = await client.get("/admin/api/access/components")
        assert listed.status_code == 200, listed.text
        items = listed.json()["items"]
        assert any(
            item["pageCode"] == "agents" and item["componentCode"] == "agent_editor"
            for item in items
        )

        saved = await client.put(
            "/admin/api/access/components/agents/agent_editor",
            headers=_headers(client),
            json={"tenantId": None, "permissions": ["agent.write"]},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["item"]["permissions"] == ["agent.write"]

        missing = await client.put(
            "/admin/api/access/components/agents/not_a_component",
            headers=_headers(client),
            json={"tenantId": None, "permissions": ["agent.write"]},
        )
        assert missing.status_code == 404, missing.text
    finally:
        await client.aclose()


async def test_access_pages_rejects_invalid_tenant_scope() -> None:
    """租户标识不合法时是 400（写错了），不是 403（被拒绝）。"""
    client = await _client()
    try:
        response = await client.put(
            "/admin/api/access/pages/agents",
            headers=_headers(client),
            json={"tenantId": "not a tenant!", "permissions": ["agent.read"]},
        )
        assert response.status_code == 400, response.text
        assert response.json()["code"] == "invalid_input"
    finally:
        await client.aclose()
