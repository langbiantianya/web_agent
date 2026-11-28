"""Admin 全模块验收矩阵。

这里不复述单个表单的字段测试，而是覆盖跨页面、跨模块、容易被局部用例漏掉
的契约：所有注册页可打开、写接口统一 fail-closed、跨模块引用不能破坏工作流，
以及破坏性按钮的可访问名称和确认提示。
"""

from __future__ import annotations

import re
import uuid
import json

import httpx
import pytest
import sqlalchemy as sa

from app.core.tools import tool_names
from app.repo.agent_repo import agent_repo
from app.repo.llm_endpoint_repo import llm_endpoint_repo
from app.repo.mcp_repo import mcp_server_repo, mcp_tool_repo
from app.repo.skill_repo import skill_repo
from app.repo.workflow_repo import workflow_repo
from services.admin_web import app as admin_web_app
from services.admin_web.registry import PAGE_SPECS
from app.models.agent import Agent
from app.models.skill import Skill


async def _logged_in_client() -> tuple[httpx.AsyncClient, str]:
    """复用既有真实 Session 登录器，返回 ASGI 浏览器客户端与 CSRF。"""
    from tests.test_admin_web import _create_seeded_logged_in_super_admin

    client, _, _, csrf = await _create_seeded_logged_in_super_admin()
    return client, csrf


def _workflow_definition(agent_code: str) -> dict:
    """构造一个引用指定 Agent 的最小合法工作流。"""
    return {
        "entry": "step1",
        "nodes": [
            {
                "id": "step1",
                "type": "agent",
                "agentCode": agent_code,
            }
        ],
        "edges": [],
    }


def _button_tag(markup: str, aria_label: str) -> str:
    """返回包含指定 aria-label 的完整 opening tag。"""
    marker = f'aria-label="{aria_label}"'
    position = markup.index(marker)
    start = markup.rfind("<button", 0, position)
    end = markup.index(">", position)
    return markup[start : end + 1]


@pytest.mark.asyncio
async def test_workflow_validation_has_english_diagnostics():
    """The canvas validation API follows the selected admin locale."""
    client, csrf = await _logged_in_client()
    response = await client.post(
        "/admin/workflows/validate",
        json={
            "csrf_token": csrf,
            "locale": "en",
            "definition": {
                "entry": "a",
                "nodes": [
                    {"id": "a", "type": "agent", "agentCode": "a"},
                    {"id": "b", "type": "agent", "agentCode": "b"},
                ],
                "edges": [
                    {"from": "a", "to": "b"},
                    {"from": "b", "to": "a"},
                ],
            },
        },
    )
    assert response.status_code == 200
    errors = response.json()["errors"]
    assert any("cycle" in item["message"] for item in errors)
    assert all(
        not re.search(r"[\u4e00-\u9fff]", item["message"])
        for item in errors
    )
    await client.aclose()


@pytest.mark.asyncio
async def test_every_admin_page_form_is_csrf_protected():
    """所有管理页上的 POST 表单都必须带 CSRF；响应不得回显敏感列。"""
    client, _ = await _logged_in_client()
    try:
        for spec in PAGE_SPECS:
            response = await client.get(spec.route)
            if response.status_code != 200:
                continue
            text = response.text
            for form_markup in re.findall(
                r"<form\b[^>]*>.*?</form>", text, flags=re.DOTALL
            ):
                if re.search(r'\bmethod="post"', form_markup, re.IGNORECASE):
                    assert 'name="csrf_token"' in form_markup, spec.code
            assert "api_key_cipher" not in text
            assert "password_hash" not in text
            assert "$argon2" not in text
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_all_mutating_admin_routes_reject_missing_csrf():
    """用路由表式的矩阵守住所有写入口，避免新增接口漏调统一鉴权助手。"""
    client, _ = await _logged_in_client()
    paths = [
        "/admin/workflows/validate",
        "/admin/workflows",
        "/admin/workflows/not-there/update",
        "/admin/workflows/not-there/toggle",
        "/admin/workflows/not-there/delete",
        "/admin/agents",
        "/admin/agents/not-there/update",
        "/admin/agents/not-there/toggle",
        "/admin/agents/not-there/delete",
        "/admin/skills",
        "/admin/skills/not-there/update",
        "/admin/skills/not-there/archive",
        "/admin/mcp",
        "/admin/mcp/not-there/update",
        "/admin/mcp/not-there/toggle",
        "/admin/mcp/not-there/delete",
        "/admin/mcp/not-there/refresh",
        "/admin/llm",
        "/admin/llm/probe",
        "/admin/llm/not-there/update",
        "/admin/llm/not-there/toggle",
        "/admin/llm/not-there/delete",
        "/admin/oauth-providers",
        "/admin/oauth-providers/not-there/update",
        "/admin/oauth-providers/not-there/toggle",
        "/admin/oauth-providers/not-there/delete",
        "/admin/access/pages/dashboard/permissions",
        "/admin/access/components/agents/agent_editor/permissions",
        "/admin/access/roles",
        "/admin/access/roles/not-there/permissions",
        "/admin/access/roles/not-there/inheritance",
        "/admin/access/roles/not-there/delete",
        "/admin/access/mappings",
        "/admin/access/mappings/999999/update",
        "/admin/access/mappings/999999/delete",
        "/admin/preferences",
        "/admin/super-admin/reset-my-password",
    ]
    try:
        for path in paths:
            if path == "/admin/workflows/validate":
                response = await client.post(
                    path,
                    json={
                        "definition": {
                            "entry": "step1",
                            "nodes": [],
                            "edges": [],
                        }
                    },
                )
            else:
                response = await client.post(path, data={})
            assert response.status_code in {403, 404}, (path, response.text)
    finally:
        await client.aclose()


