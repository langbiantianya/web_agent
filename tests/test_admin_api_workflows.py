"""管理面 JSON API —— 工作流资源（V4.10）。

锁住两件事：**图定义只有画布这一条写路径**（管理面 API 只改名称与
描述，不改图），以及新建时初始 Agent 必须真实存在且已启用。
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
from app.repo.workflow_repo import workflow_repo


async def test_workflow_crud_round_trip() -> None:
    """创建 → 读取 → 启停 → 删除。"""
    client = await _client()
    code = f"wf-{uuid.uuid4().hex[:8]}"
    agent_code = f"agent-{uuid.uuid4().hex[:8]}"
    try:
        async with AdminSessionLocal() as db:
            await agent_repo.create(
                db,
                code=agent_code,
                tenant_id=None,
                name="入口 Agent",
                description="",
                system_prompt="hi",
                model="m",
                llm_endpoint_code="",
                base_url="",
                api_key_cipher="",
                tools=[],
                skills=[],
                enabled=1,
            )
            await db.commit()

        created = await client.post(
            "/admin/api/workflows",
            headers=_headers(client),
            json={
                "code": code,
                "name": "演示工作流",
                "description": "用于测试",
                "agentCode": agent_code,
                "enabled": True,
            },
        )
        assert created.status_code == 201, created.text
        item = created.json()["item"]
        assert item["code"] == code
        # 新建只生成单节点骨架，图交给画布。
        assert item["nodeCount"] == 1

        fetched = await client.get(f"/admin/api/workflows/{code}")
        assert fetched.status_code == 200
        definition = fetched.json()["item"]["definition"]
        assert definition["nodes"][0]["agentCode"] == agent_code

        toggled = await client.post(
            f"/admin/api/workflows/{code}/toggle",
            headers=_headers(client),
            json={"enabled": False},
        )
        assert toggled.status_code == 200
        assert toggled.json()["item"]["enabled"] is False

        removed = await client.delete(
            f"/admin/api/workflows/{code}", headers=_headers(client)
        )
        assert removed.status_code == 200
        assert removed.json()["ok"] is True
    finally:
        async with AdminSessionLocal() as db:
            await workflow_repo.delete(db, code)
            await agent_repo.delete(db, agent_code)
            await db.commit()
        await client.aclose()


async def test_workflow_update_does_not_touch_definition() -> None:
    """改名称不能动图定义。"""
    client = await _client()
    code = f"wf-{uuid.uuid4().hex[:8]}"
    agent_code = f"agent-{uuid.uuid4().hex[:8]}"
    try:
        async with AdminSessionLocal() as db:
            await agent_repo.create(
                db,
                code=agent_code,
                tenant_id=None,
                name="入口 Agent",
                description="",
                system_prompt="hi",
                model="m",
                llm_endpoint_code="",
                base_url="",
                api_key_cipher="",
                tools=[],
                skills=[],
                enabled=1,
            )
            await db.commit()
        await client.post(
            "/admin/api/workflows",
            headers=_headers(client),
            json={
                "code": code,
                "name": "原名",
                "agentCode": agent_code,
                "enabled": True,
            },
        )
        before = (await client.get(f"/admin/api/workflows/{code}")).json()["item"]

        updated = await client.put(
            f"/admin/api/workflows/{code}",
            headers=_headers(client),
            json={"name": "新名", "definition": {"nodes": []}},
        )
        assert updated.status_code == 200, updated.text
        after = (await client.get(f"/admin/api/workflows/{code}")).json()["item"]
        assert after["name"] == "新名"
        # 传入的 definition 被忽略：图仍然由画布独占写路径。
        assert after["definition"] == before["definition"]
    finally:
        async with AdminSessionLocal() as db:
            await workflow_repo.delete(db, code)
            await agent_repo.delete(db, agent_code)
            await db.commit()
        await client.aclose()


async def test_workflow_create_requires_existing_agent() -> None:
    """初始 Agent 必须真实存在。"""
    client = await _client()
    try:
        response = await client.post(
            "/admin/api/workflows",
            headers=_headers(client),
            json={
                "code": f"wf-{uuid.uuid4().hex[:8]}",
                "name": "坏工作流",
                "agentCode": "does-not-exist",
                "enabled": True,
            },
        )
    finally:
        await client.aclose()

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "invalid_input"
    assert "agentCode" in body["fieldErrors"]


async def test_workflow_write_requires_csrf_header() -> None:
    """写操作缺 CSRF 头一律 403。"""
    client = await _client()
    try:
        response = await client.post(
            "/admin/api/workflows",
            json={
                "code": f"wf-{uuid.uuid4().hex[:8]}",
                "name": "无令牌",
                "agentCode": "x",
            },
        )
    finally:
        await client.aclose()

    assert response.status_code == 403
    assert response.json()["code"] == "invalid_csrf"


async def test_workflow_list_requires_authentication() -> None:
    """未登录返回 401 JSON。"""
    from services.admin_web.app import app as admin_asgi_app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_asgi_app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        response = await client.get("/admin/api/workflows")

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"
