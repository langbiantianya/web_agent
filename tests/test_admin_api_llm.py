"""管理面 JSON API —— LLM 端点资源（V4.10）。

锁住三件事：认证与 CSRF、**明文 Key 不回显**、以及「仍被 Agent 引用时不能
停用/删除」这条业务约束。
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from app.core.crypto import decrypt, encrypt
from app.repo.agent_repo import agent_repo
from app.repo.llm_endpoint_repo import llm_endpoint_repo
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


async def test_llm_crud_round_trip_and_key_is_never_echoed() -> None:
    """创建 → 读取 → 更新 → 删除；响应里绝不出现明文 Key。"""
    client = await _client()
    code = f"llm-{uuid.uuid4().hex[:8]}"
    try:
        created = await client.post(
            "/admin/api/llm",
            headers=_headers(client),
            json={
                "code": code,
                "name": "Test",
                "baseUrl": "https://api.example.com/v1",
                "model": "test-model",
                "apiKey": "sk-secret-value",
                "priority": 10,
                "enabled": True,
            },
        )
        assert created.status_code == 201, created.text
        item = created.json()["item"]
        assert item["code"] == code
        assert item["hasApiKey"] is True
        assert "sk-secret-value" not in created.text

        listed = await client.get("/admin/api/llm")
        assert listed.status_code == 200
        assert "sk-secret-value" not in listed.text
        assert any(row["code"] == code for row in listed.json()["items"])

        updated = await client.put(
            f"/admin/api/llm/{code}",
            headers=_headers(client),
            json={"model": "new-model"},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["item"]["model"] == "new-model"
        # 只传一个字段，其余保持原样；Key 也没被清空。
        assert updated.json()["item"]["baseUrl"] == "https://api.example.com/v1"
        assert updated.json()["item"]["hasApiKey"] is True

        async with AdminSessionLocal() as db:
            row = await llm_endpoint_repo.get_by_code(db, code)
            assert row is not None
            assert decrypt(row.api_key_cipher) == "sk-secret-value"

        removed = await client.delete(
            f"/admin/api/llm/{code}", headers=_headers(client)
        )
        assert removed.status_code == 200
        assert removed.json()["ok"] is True
    finally:
        await client.aclose()


async def test_invalid_base_url_is_reported_per_field() -> None:
    """地址不合法时要标到具体字段上。"""
    client = await _client()
    try:
        response = await client.post(
            "/admin/api/llm",
            headers=_headers(client),
            json={
                "code": f"bad-{uuid.uuid4().hex[:8]}",
                "baseUrl": "ftp://example.com",
                "enabled": True,
            },
        )
    finally:
        await client.aclose()

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "invalid_input"
    assert "baseUrl" in body["fieldErrors"]


async def test_endpoint_in_use_cannot_be_disabled_or_deleted() -> None:
    """仍被 Agent 引用的连接不能停用、也不能删除。"""
    client = await _client()
    code = f"llm-{uuid.uuid4().hex[:8]}"
    agent_code = f"agent-{uuid.uuid4().hex[:8]}"
    try:
        async with AdminSessionLocal() as db:
            await llm_endpoint_repo.create(
                db,
                code=code,
                name="Bound",
                base_url="https://api.example.com/v1",
                model="m",
                api_key_cipher=encrypt("sk-x"),
                priority=10,
                enabled=1,
            )
            await agent_repo.create(
                db,
                code=agent_code,
                tenant_id=None,
                name="Bound agent",
                description="",
                system_prompt="hi",
                model="m",
                llm_endpoint_code=code,
                base_url="",
                api_key_cipher="",
                tools=[],
                skills=[],
                enabled=1,
            )
            await db.commit()

        disabled = await client.post(
            f"/admin/api/llm/{code}/toggle",
            headers=_headers(client),
            json={"enabled": False},
        )
        assert disabled.status_code == 409
        assert disabled.json()["code"] == "endpoint_in_use"

        removed = await client.delete(
            f"/admin/api/llm/{code}", headers=_headers(client)
        )
        assert removed.status_code == 409
        assert removed.json()["code"] == "endpoint_in_use"
    finally:
        async with AdminSessionLocal() as db:
            await agent_repo.delete(db, agent_code)
            await llm_endpoint_repo.delete(db, code)
            await db.commit()
        await client.aclose()


async def test_llm_write_requires_csrf_header() -> None:
    """写操作缺 CSRF 头一律 403。"""
    client = await _client()
    try:
        response = await client.post(
            "/admin/api/llm",
            json={
                "code": f"nocsrf-{uuid.uuid4().hex[:8]}",
                "baseUrl": "https://api.example.com/v1",
                "enabled": True,
            },
        )
    finally:
        await client.aclose()

    assert response.status_code == 403
    assert response.json()["code"] == "invalid_csrf"


async def test_llm_list_requires_authentication() -> None:
    """未登录返回 401 JSON。"""
    from services.admin_web.app import app as admin_asgi_app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_asgi_app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        response = await client.get("/admin/api/llm")

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


@pytest.mark.parametrize("path", ["/admin/api/llm", "/admin/api/llm/x"])
async def test_llm_endpoints_all_require_authentication(path: str) -> None:
    """每个资源端点都要先认证。"""
    from services.admin_web.app import app as admin_asgi_app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_asgi_app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        response = await client.get(path)

    assert response.status_code == 401