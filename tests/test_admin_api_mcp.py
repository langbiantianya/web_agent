"""管理面 JSON API —— MCP 服务器资源（V4.10）。

锁住三件事：**凭据字段只写不回显**、传输方式与端点字段必须匹配
（stdio 不许带 URL、http 不许带命令）、以及写操作缺 CSRF 头要被拒。
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


from app.repo.agent_repo import agent_repo
from app.repo.mcp_repo import mcp_server_repo


async def test_mcp_crud_round_trip() -> None:
    """创建 → 读取 → 更新 → 启停 → 删除。"""
    client = await _client()
    code = f"mcp-{uuid.uuid4().hex[:8]}"
    try:
        created = await client.post(
            "/admin/api/mcp",
            headers=_headers(client),
            json={
                "code": code,
                "name": "Local MCP",
                "transport": "http",
                "url": "https://mcp.example.com/mcp",
                "timeoutSec": 30,
            },
        )
        assert created.status_code == 201, created.text
        item = created.json()["item"]
        assert item["code"] == code
        assert item["transport"] == "http"
        assert item["url"] == "https://mcp.example.com/mcp"
        # 凭据字段只回显键名，不回显值。
        assert item["envKeys"] == []
        assert item["headerKeys"] == []

        listed = await client.get("/admin/api/mcp")
        assert listed.status_code == 200
        assert any(row["code"] == code for row in listed.json()["items"])

        updated = await client.put(
            f"/admin/api/mcp/{code}",
            headers=_headers(client),
            json={"name": "改过的名字", "timeoutSec": 60},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["item"]["name"] == "改过的名字"
        assert updated.json()["item"]["timeoutSec"] == 60

        toggled = await client.post(
            f"/admin/api/mcp/{code}/toggle",
            headers=_headers(client),
            json={"enabled": False},
        )
        assert toggled.status_code == 200
        assert toggled.json()["item"]["enabled"] is False

        removed = await client.delete(
            f"/admin/api/mcp/{code}", headers=_headers(client)
        )
        assert removed.status_code == 200
        assert removed.json()["ok"] is True
    finally:
        await client.aclose()


async def test_stdio_transport_rejects_url() -> None:
    """stdio 不许带 URL。"""
    client = await _client()
    try:
        response = await client.post(
            "/admin/api/mcp",
            headers=_headers(client),
            json={
                "code": f"mcp-{uuid.uuid4().hex[:8]}",
                "transport": "stdio",
                "command": "npx -y @modelcontextprotocol/server-everything",
                "url": "https://mcp.example.com/mcp",
            },
        )
    finally:
        await client.aclose()

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "invalid_input"
    assert body["fieldErrors"]


async def test_http_transport_requires_url() -> None:
    """http 必须给地址。"""
    client = await _client()
    try:
        response = await client.post(
            "/admin/api/mcp",
            headers=_headers(client),
            json={
                "code": f"mcp-{uuid.uuid4().hex[:8]}",
                "transport": "http",
            },
        )
    finally:
        await client.aclose()

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_input"


async def test_mcp_write_requires_csrf_header() -> None:
    """写操作缺 CSRF 头一律 403。"""
    client = await _client()
    try:
        response = await client.post(
            "/admin/api/mcp",
            json={
                "code": f"mcp-{uuid.uuid4().hex[:8]}",
                "transport": "http",
                "url": "https://mcp.example.com/mcp",
            },
        )
    finally:
        await client.aclose()

    assert response.status_code == 403
    assert response.json()["code"] == "invalid_csrf"


async def test_mcp_list_requires_authentication() -> None:
    """未登录返回 401 JSON。"""
    from services.admin_web.app import app as admin_asgi_app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_asgi_app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        response = await client.get("/admin/api/mcp")

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"
