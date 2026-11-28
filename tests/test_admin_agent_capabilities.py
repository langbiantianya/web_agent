"""管理端 Agent、Skill 与 MCP 能力联动测试。"""

from __future__ import annotations

import uuid

import httpx
import pytest
import sqlalchemy as sa

from app.core.mcp.catalog import McpCatalogError, refresh_catalog
from app.core.mcp.manager import mcp_manager
from app.models.agent import Agent
from app.repo.agent_repo import agent_repo
from app.repo.llm_endpoint_repo import llm_endpoint_repo
from app.repo.mcp_repo import mcp_server_repo, mcp_tool_repo
from app.repo.skill_repo import skill_repo
from services.admin_web import app as admin_web_app
from services.admin_web.accounts import (
    create_super_admin_account,
    create_super_admin_session,
)
from services.admin_web.db import AdminSessionLocal
from services.admin_web.notifications import DeliveryResult
from services.admin_web.registry import seed_registry
from services.admin_web.security import (
    super_csrf_cookie_name,
    super_session_cookie_name,
)


async def _password_sender(*, username, email, phone, password):
    """后台测试账号不发送真实通知。"""
    return DeliveryResult(email_status="sent", sms_status="skipped")


async def _logged_in_client() -> tuple[httpx.AsyncClient, str]:
    """建立平台超级管理员会话。"""
    tag = uuid.uuid4().hex[:10]
    async with AdminSessionLocal() as db:
        await seed_registry(db)
        account, _ = await create_super_admin_account(
            db,
            username=f"cap_admin_{tag}",
            email=f"cap-{tag}@example.com",
            phone=None,
            password_sender=_password_sender,
        )
        token, csrf = await create_super_admin_session(db, account)
        await db.commit()
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_web_app.app),
        base_url="http://test",
        follow_redirects=False,
    )
    client.cookies.set(super_session_cookie_name(), token)
    client.cookies.set(super_csrf_cookie_name(), csrf)
    return client, csrf


@pytest.mark.asyncio
async def test_catalog_refresh_cannot_remove_agent_bound_tool(db, monkeypatch):
    """MCP 刷新若会移除已绑定工具，应当在替换快照前失败。"""
    tag = uuid.uuid4().hex[:8]
    server_code = f"refresh-{tag}"
    tool_name = f"mcp__{server_code}__lookup"
    server = await mcp_server_repo.create(
        db,
        code=server_code,
        tenant_id="default",
        name=server_code,
        transport="http",
        url="https://refresh.example/mcp",
        enabled=1,
        auth_type="none",
    )
    await mcp_tool_repo.replace_for_server(
        db,
        server.id,
        [
            {
                "qualified_name": tool_name,
                "tool_name": "lookup",
                "description": "Bound tool",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
    )
    await agent_repo.create(
        db,
        code=f"agent-{tag}",
        tenant_id="default",
        name="Bound agent",
        system_prompt="Use lookup.",
        model="m",
        base_url="",
        api_key_cipher="",
        tools=[tool_name],
        skills=[],
        enabled=1,
    )

    async def _empty_tools(_server):
        return []

    monkeypatch.setattr(mcp_manager, "list_tools", _empty_tools)
    with pytest.raises(McpCatalogError, match="仍被 Agent 使用"):
        await refresh_catalog(db, server)

    remaining = await mcp_tool_repo.list_by_server(db, server.id)
    assert [tool.qualified_name for tool in remaining] == [tool_name]


