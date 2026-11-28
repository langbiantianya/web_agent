"""Workflow 画布的 island 片段页（V4.10 阶段 D）。

锁住三件事：

1. ``/admin/workflows/{code}/canvas`` 返回的是**不带外壳**的片段页——
   导航、顶栏由 SvelteKit 那一层负责，画布自己带齐样式与脚本；
2. 画布的**写路径没变**：保存表单仍然指向 ``/admin/workflows/{code}/update``，
   但落点回到片段页（否则 iframe 里会跳进整页）；
3. 整页版本 ``/admin/workflows/{code}`` 仍然是**回退路径**：SvelteKit
   容器关掉后管理端照样能用，所以它必须继续带外壳。
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from app.repo.agent_repo import agent_repo
from app.repo.workflow_repo import workflow_repo
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
            username=f"canvas_{tag}",
            email=f"canvas-{tag}@example.com",
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


async def _seed_workflow(client: httpx.AsyncClient) -> tuple[str, str]:
    """建一个 Agent 与一个工作流，返回它们的 code。"""
    code = f"wf-{uuid.uuid4().hex[:8]}"
    agent_code = f"agent-{uuid.uuid4().hex[:8]}"
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
        headers={"x-csrf-token": client.cookies.get(super_csrf_cookie_name()) or ""},
        json={
            "code": code,
            "name": "画布演示",
            "description": "",
            "agentCode": agent_code,
            "enabled": True,
        },
    )
    assert created.status_code == 201, created.text
    return code, agent_code


@pytest.mark.asyncio
async def test_canvas_fragment_has_no_shell_but_keeps_island() -> None:
    """片段页：没有导航外壳，但画布 DOM、脚本与 CSRF 都在。"""
    client = await _client()
    code, agent_code = await _seed_workflow(client)
    try:
        response = await client.get(f"/admin/workflows/{code}/canvas")
        assert response.status_code == 200, response.text
        text = response.text

        # 画布本体
        assert 'id="workflow-editor-canvas"' in text
        assert 'id="workflow-editor-inspector"' in text
        # 画布脚本与样式
        assert "/admin/static/workflow-editor.js" in text
        assert "/admin/static/workflow-editor-core.js" in text
        assert "/admin/static/workflow-editor.css" in text
        assert "/admin/static/admin.css" in text
        # 写路径没变，落点回到片段页
        assert f'action="/admin/workflows/{code}/update"' in text
        assert f'value="/admin/workflows/{code}/canvas"' in text
        assert 'name="csrf_token"' in text

        # **没有**外壳：导航与顶栏归 SvelteKit 那一层
        assert 'class="sidebar"' not in text
        assert "nav-link" not in text

        # 要能被同源的 SvelteKit 外壳用 iframe 承载：默认的 DENY 会让
        # 浏览器直接拒绝渲染这一帧（表现为 iframe 里「拒绝连接」）。
        assert response.headers.get("x-frame-options") == "SAMEORIGIN"
        assert "frame-ancestors 'self'" in response.headers.get(
            "content-security-policy", ""
        )
    finally:
        async with AdminSessionLocal() as db:
            await workflow_repo.delete(db, code)
            await agent_repo.delete(db, agent_code)
            await db.commit()
        await client.aclose()


@pytest.mark.asyncio
async def test_full_canvas_page_still_renders_shell() -> None:
    """整页画布是回退路径：外壳必须还在。"""
    client = await _client()
    code, agent_code = await _seed_workflow(client)
    try:
        response = await client.get(f"/admin/workflows/{code}")
        assert response.status_code == 200, response.text
        text = response.text
        assert 'class="sidebar"' in text
        assert 'id="workflow-editor-canvas"' in text
        # 整页的落点是整页自己
        assert f'value="/admin/workflows/{code}"' in text
        # 整页**不**允许被嵌入：它是完整的后台页面
        assert response.headers.get("x-frame-options") == "DENY"
    finally:
        async with AdminSessionLocal() as db:
            await workflow_repo.delete(db, code)
            await agent_repo.delete(db, agent_code)
            await db.commit()
        await client.aclose()


@pytest.mark.asyncio
async def test_canvas_fragment_requires_login() -> None:
    """未登录拿不到画布片段。"""
    from services.admin_web.app import app as admin_asgi_app

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_asgi_app),
        base_url="http://test",
        follow_redirects=False,
    )
    try:
        response = await client.get("/admin/workflows/whatever/canvas")
        assert response.status_code in (302, 303, 404)
    finally:
        await client.aclose()
