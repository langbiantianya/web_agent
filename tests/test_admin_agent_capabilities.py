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
async def test_admin_agent_binds_visible_skill_and_mcp_tools(db):
    """Agent 页面按来源展示能力，并保存 Skill + 内置/MCP 工具组合。"""
    tag = uuid.uuid4().hex[:8]
    llm_code = f"llm-{tag}"
    skill_code = f"skill-{tag}"
    server_code = f"tools-{tag}"
    agent_code = f"agent-{tag}"
    await llm_endpoint_repo.create(
        db,
        code=llm_code,
        name="Capability LLM",
        base_url="https://llm.example/v1",
        model="capability-model",
        api_key_cipher="cipher",
        priority=10,
        enabled=1,
    )
    await skill_repo.create(
        db,
        code=skill_code,
        name=skill_code,
        description="Capability skill",
        allowed_tools="calc, mcp__tools__search",
        body="Use the search tool when needed.",
        status="published",
        enabled=1,
        content_hash="hash",
    )
    server = await mcp_server_repo.create(
        db,
        code=server_code,
        tenant_id="default",
        name="Capability tools",
        transport="http",
        url="https://mcp.example/mcp",
        enabled=1,
        auth_type="none",
    )
    await mcp_tool_repo.replace_for_server(
        db,
        server.id,
        [
            {
                "qualified_name": f"mcp__{server_code}__search",
                "tool_name": "search",
                "description": "Search capability records.",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
    )
    client, csrf = await _logged_in_client()
    try:
        page = await client.get("/admin/agents")
        assert page.status_code == 200
        assert "Capability tools" in page.text
        assert f"mcp__{server_code}__search" in page.text
        assert "calc" in page.text
        assert "not granted to this agent" in page.text
        assert 'name="skills"' in page.text
        assert 'name="tools"' in page.text

        created = await client.post(
            "/admin/agents",
            data={
                "csrf_token": csrf,
                "code": agent_code,
                "name": "Capability agent",
                "llm_endpoint_code": llm_code,
                "model": "",
                "skills": [skill_code],
                "tools": ["calc", f"mcp__{server_code}__search"],
                "system_prompt": "Use only checked capabilities.",
                "enabled": "1",
            },
        )
        assert created.status_code == 303
        stored = await db.scalar(
            sa.select(Agent).where(Agent.code == agent_code)
        )
        assert stored is not None
        assert stored.skills == [skill_code]
        assert stored.tools == [
            "calc",
            f"mcp__{server_code}__search",
        ]

        skills_page = await client.get("/admin/skills")
        assert agent_code in skills_page.text
        mcp_page = await client.get("/admin/mcp")
        assert agent_code in mcp_page.text
        assert "search" in mcp_page.text

        archived = await client.post(
            f"/admin/skills/{skill_code}/archive",
            data={"csrf_token": csrf},
        )
        assert "error=in_use" in archived.headers["location"]
        disabled_mcp = await client.post(
            f"/admin/mcp/{server_code}/toggle",
            data={"csrf_token": csrf, "enabled": "0"},
        )
        assert "error=in_use" in disabled_mcp.headers["location"]
        deleted_mcp = await client.post(
            f"/admin/mcp/{server_code}/delete",
            data={"csrf_token": csrf},
        )
        assert "error=in_use" in deleted_mcp.headers["location"]

        renamed = await client.post(
            f"/admin/mcp/{server_code}/update",
            data={
                "csrf_token": csrf,
                "name": "Renamed tools",
                "timeout_sec": "45",
                "url": "https://mcp.example/mcp",
                "command": "",
            },
        )
        assert "notice=updated" in renamed.headers["location"]
    finally:
        await client.aclose()


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


@pytest.mark.asyncio
async def test_admin_can_edit_historically_disabled_bound_skill(db):
    """已停用的历史绑定仍可维护内容，避免管理页面永久锁死。"""
    tag = uuid.uuid4().hex[:8]
    skill_code = f"disabled-{tag}"
    await skill_repo.create(
        db,
        code=skill_code,
        name=skill_code,
        description="Historical skill",
        allowed_tools="",
        body="Old body",
        status="published",
        enabled=0,
        content_hash="hash",
    )
    await agent_repo.create(
        db,
        code=f"agent-{tag}",
        tenant_id="default",
        name="Historical binding",
        system_prompt="Use the historical skill.",
        model="m",
        base_url="",
        api_key_cipher="",
        tools=[],
        skills=[skill_code],
        enabled=1,
    )
    client, csrf = await _logged_in_client()
    try:
        updated = await client.post(
            f"/admin/skills/{skill_code}/update",
            data={
                "csrf_token": csrf,
                "description": "Maintained skill",
                "allowed_tools": "",
                "body": "Maintained body",
                "status": "published",
                "enabled": "0",
            },
        )
        assert updated.status_code == 303
        assert "notice=updated" in updated.headers["location"]
        row = await skill_repo.get_by_code(db, skill_code, "default")
        assert row is not None
        assert row.body == "Maintained body"
    finally:
        await client.aclose()


def test_capability_groups_are_named_and_empty_skills_are_hidden() -> None:
    """能力分组应有明确标题；没有 Skill 时不能留下空框。"""
    from services.admin_web.views.agent_capabilities import capability_selector

    markup = capability_selector(
        skills=[],
        mcp_servers=[],
        mcp_tools=[],
    ).to_html()
    assert "Published Skills" not in markup
    assert "Built-in tools" in markup
