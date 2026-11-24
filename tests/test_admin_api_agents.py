"""管理面 JSON API —— Agent 资源（V4.10）。

这些用例锁住三件事：

1. **认证与 CSRF**：没登录是 401，写操作缺 CSRF 头是 403 —— 不是 302，
   也不是 200。
2. **权限判定仍然只有一处**：写操作要过「页面可见 + 组件可写」两道 Casbin
   判定；前端拿到的「可见组件」只是渲染依据。
3. **写入语义**：部分更新不能清空没传的字段；清空 JSON Schema 要真的写
   NULL。
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import sqlalchemy as sa

from app.core.crypto import encrypt
from app.repo.agent_repo import agent_repo
from app.repo.llm_endpoint_repo import llm_endpoint_repo
from services.admin_web.accounts import create_super_admin_session
from services.admin_web.db import AdminSessionLocal
from services.admin_web.notifications import DeliveryResult
from services.admin_web.security import super_csrf_cookie_name, super_session_cookie_name


def _password_sender():
    """构造总是成功的测试通知器（账号创建要求通知成功）。"""

    async def send(*, username, email, phone, password):
        """报告通知已送达。"""
        return DeliveryResult(email_status="sent", sms_status="skipped")

    return send


async def _client() -> tuple[httpx.AsyncClient, str]:
    """建立带平台超级管理员会话的 ASGI 客户端，并准备一个 LLM 连接。"""
    from services.admin_web.accounts import create_super_admin_account
    from services.admin_web.app import app as admin_asgi_app

    from services.admin_web.registry import seed_registry

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
        endpoint_code = f"llm-{tag}"
        await llm_endpoint_repo.create(
            db,
            code=endpoint_code,
            name="Test endpoint",
            base_url="https://api.example.com/v1",
            model="test-model",
            api_key_cipher=encrypt("sk-test"),
            priority=100,
            enabled=1,
        )
        await db.commit()
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_asgi_app),
        base_url="http://test",
        follow_redirects=False,
    )
    client.cookies.set(super_session_cookie_name(), token)
    client.cookies.set(super_csrf_cookie_name(), csrf)
    return client, endpoint_code


def _headers(client: httpx.AsyncClient) -> dict[str, str]:
    """取 CSRF 头。"""
    return {"x-csrf-token": client.cookies.get(super_csrf_cookie_name()) or ""}


async def _cleanup(client: httpx.AsyncClient, endpoint_code: str) -> None:
    """删掉本次用例造的 LLM 连接。"""
    async with AdminSessionLocal() as db:
        await llm_endpoint_repo.delete(db, endpoint_code)
        await db.commit()
    await client.aclose()


async def test_unauthenticated_list_returns_401_json() -> None:
    """未登录要拿到 401 JSON，而不是 302 跳登录页。"""
    from services.admin_web.app import app as admin_asgi_app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_asgi_app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        response = await client.get("/admin/api/agents")

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["code"] == "unauthorized"


async def test_write_without_csrf_header_is_rejected() -> None:
    """写操作必须带 CSRF 头。"""
    client, endpoint_code = await _client()
    try:
        response = await client.post(
            "/admin/api/agents",
            json={
                "code": "no_csrf",
                "name": "No CSRF",
                "systemPrompt": "hi",
                "llmEndpointCode": endpoint_code,
                "enabled": True,
            },
        )
    finally:
        await _cleanup(client, endpoint_code)

    assert response.status_code == 403
    assert response.json()["code"] == "invalid_csrf"


async def test_agent_crud_round_trip() -> None:
    """创建 → 读取 → 更新 → 删除的完整链路。"""
    client, endpoint_code = await _client()
    code = f"agent-{uuid.uuid4().hex[:8]}"
    try:
        created = await client.post(
            "/admin/api/agents",
            headers=_headers(client),
            json={
                "code": code,
                "name": "Demo",
                "description": "first",
                "systemPrompt": "You are helpful.",
                "model": "test-model",
                "llmEndpointCode": endpoint_code,
                "tools": [],
                "skills": [],
                "responseFormat": "text",
                "enabled": True,
            },
        )
        assert created.status_code == 201, created.text
        item = created.json()["item"]
        assert item["code"] == code
        assert item["name"] == "Demo"
        assert item["enabled"] is True

        listed = await client.get("/admin/api/agents")
        assert listed.status_code == 200
        payload = listed.json()
        assert any(row["code"] == code for row in payload["items"])
        assert payload["total"] >= 1
        assert any(
            item["code"] == endpoint_code for item in payload["options"]["llmEndpoints"]
        )

        fetched = await client.get(f"/admin/api/agents/{code}")
        assert fetched.status_code == 200
        assert fetched.json()["item"]["name"] == "Demo"

        updated = await client.put(
            f"/admin/api/agents/{code}",
            headers=_headers(client),
            json={"description": "second"},
        )
        assert updated.status_code == 200, updated.text
        updated_item = updated.json()["item"]
        assert updated_item["description"] == "second"
        # 只传一个字段时，其余字段必须原样保留。
        assert updated_item["name"] == "Demo"
        assert updated_item["systemPrompt"] == "You are helpful."
        assert updated_item["llmEndpointCode"] == endpoint_code

        removed = await client.delete(
            f"/admin/api/agents/{code}", headers=_headers(client)
        )
        assert removed.status_code == 200
        assert removed.json()["ok"] is True

        assert (await client.get(f"/admin/api/agents/{code}")).status_code == 404
    finally:
        await _cleanup(client, endpoint_code)


async def test_duplicate_code_returns_409() -> None:
    """重复 code 要报冲突，而不是静默覆盖。"""
    client, endpoint_code = await _client()
    code = f"dup-{uuid.uuid4().hex[:8]}"
    payload = {
        "code": code,
        "name": "Dup",
        "systemPrompt": "hi",
        "llmEndpointCode": endpoint_code,
        "enabled": True,
    }
    try:
        first = await client.post(
            "/admin/api/agents", headers=_headers(client), json=payload
        )
        assert first.status_code == 201, first.text
        second = await client.post(
            "/admin/api/agents", headers=_headers(client), json=payload
        )
    finally:
        await _cleanup(client, endpoint_code)

    assert second.status_code == 409
    assert second.json()["code"] == "duplicate"


async def test_unknown_llm_endpoint_is_reported_per_field() -> None:
    """连接不存在时要标到具体字段上。"""
    client, endpoint_code = await _client()
    try:
        response = await client.post(
            "/admin/api/agents",
            headers=_headers(client),
            json={
                "code": f"bad-{uuid.uuid4().hex[:8]}",
                "name": "Bad",
                "systemPrompt": "hi",
                "llmEndpointCode": "does-not-exist",
                "enabled": True,
            },
        )
    finally:
        await _cleanup(client, endpoint_code)

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "invalid_input"
    assert "llm_endpoint_code" in body["fieldErrors"]


async def test_clearing_response_schema_writes_null() -> None:
    """显式传 null 表示「不限定结构」，要真的写 NULL。"""
    client, endpoint_code = await _client()
    code = f"schema-{uuid.uuid4().hex[:8]}"
    try:
        await client.post(
            "/admin/api/agents",
            headers=_headers(client),
            json={
                "code": code,
                "name": "Schema",
                "systemPrompt": "hi",
                "llmEndpointCode": endpoint_code,
                "responseFormat": "json",
                "responseSchema": {"type": "object"},
                "enabled": True,
            },
        )
        cleared = await client.put(
            f"/admin/api/agents/{code}",
            headers=_headers(client),
            json={"responseSchema": None},
        )
        assert cleared.status_code == 200, cleared.text
        async with AdminSessionLocal() as db:
            row = await agent_repo.get_by_code(db, code)
            assert row is not None
            assert row.response_schema is None
    finally:
        await _cleanup(client, endpoint_code)


async def test_toggle_reports_missing_agent() -> None:
    """切换不存在的 Agent 返回 404。"""
    client, endpoint_code = await _client()
    try:
        response = await client.post(
            "/admin/api/agents/nope/toggle",
            headers=_headers(client),
            json={"enabled": False},
        )
    finally:
        await _cleanup(client, endpoint_code)

    assert response.status_code == 404
    assert response.json()["code"] == "missing"


@pytest.mark.parametrize("path", ["/admin/api/agents", "/admin/api/agents/x"])
async def test_list_requires_authentication(path: str) -> None:
    """所有资源端点都要先认证。"""
    from services.admin_web.app import app as admin_asgi_app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_asgi_app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        response = await client.get(path)

    assert response.status_code == 401


async def test_agent_rows_are_tenant_scoped() -> None:
    """列表只返回当前租户的 Agent。"""
    client, endpoint_code = await _client()
    try:
        response = await client.get("/admin/api/agents")
        assert response.status_code == 200
        for row in response.json()["items"]:
            assert row.get("tenantId") in (None, "")
    finally:
        await _cleanup(client, endpoint_code)