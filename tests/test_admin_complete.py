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
async def test_workflow_save_rejects_missing_or_disabled_agent_reference(db):
    """Workflow saves must not keep broken Agent links after schema/config edits."""
    tag = uuid.uuid4().hex[:8]
    disabled_code = f"disabled-{tag}"
    workflow_code = f"wf-link-{tag}"
    await agent_repo.create(
        db,
        code=disabled_code,
        tenant_id="default",
        name="Disabled Agent",
        system_prompt="disabled",
        model="test-model",
        base_url="",
        api_key_cipher="",
        tools=[],
        skills=[],
        enabled=0,
    )
    client, csrf = await _logged_in_client()
    created = await client.post(
        "/admin/workflows",
        data={
            "csrf_token": csrf,
            "code": workflow_code,
            "name": "Agent Link Flow",
            "description": "",
            "agent_code": disabled_code,
            "enabled": "1",
        },
    )
    assert created.status_code == 303
    assert "error=invalid" in created.headers["location"]

    await workflow_repo.create(
        db,
        code=workflow_code,
        name="Agent Link Flow",
        description="",
        definition=_workflow_definition(disabled_code),
        enabled=1,
    )
    missing_definition = _workflow_definition(f"missing-{tag}")
    updated = await client.post(
        f"/admin/workflows/{workflow_code}/update",
        data={
            "csrf_token": csrf,
            "name": "Agent Link Flow",
            "description": "",
            "enabled": "1",
            "definition": json.dumps(missing_definition),
            "next": f"/admin/workflows/{workflow_code}",
        },
    )
    assert updated.status_code == 303
    assert "error=invalid" in updated.headers["location"]
    row = await workflow_repo.get_by_code(db, workflow_code)
    assert row is not None
    assert row.definition["nodes"][0]["agentCode"] == disabled_code
    await client.aclose()


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
async def test_draft_skill_is_never_left_enabled(db):
    """Draft and enabled are mutually exclusive for prompt injection."""
    client, csrf = await _logged_in_client()
    code = f"draft-{uuid.uuid4().hex[:8]}"
    response = await client.post(
        "/admin/skills",
        data={
            "csrf_token": csrf,
            "code": code,
            "description": "Draft skill",
            "allowed_tools": "",
            "body": "Not active yet.",
            "status": "draft",
            "enabled": "1",
        },
    )
    assert response.status_code == 303
    row = await db.scalar(sa.select(Skill).where(Skill.code == code))
    assert row is not None
    assert row.status == "draft"
    assert not row.enabled
    await client.aclose()


def test_workflow_read_only_users_can_open_the_graph():
    """Read-only users still need an entry into the canvas viewer."""
    from services.admin_web.accounts import AdminPrincipal
    from services.admin_web.views.workflows import workflows_page

    row = type(
        "WorkflowRow",
        (),
        {
            "code": "view-flow",
            "name": "View Flow",
            "enabled": 1,
            "version": 1,
        },
    )()
    markup = workflows_page(
        principal=AdminPrincipal(
            kind="platform_super_admin",
            principal_id=1,
            user_id=None,
            tenant_id="default",
            username="reader",
            session_id=1,
            is_owner=False,
        ),
        csrf="csrf",
        pages=[],
        rows=[row],
        agents=[],
        theme="fluent",
        color_mode="system",
        density="comfortable",
        can_write=False,
    ).to_html()
    assert 'href="/admin/workflows/view-flow"' in markup
    assert ">View graph</a>" in markup


@pytest.mark.asyncio
async def test_every_admin_page_requires_login():
    """注册表里的每个管理页都必须先认证，不能出现漏保护页面。"""
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_web_app.app),
        base_url="http://test",
        follow_redirects=False,
    )
    try:
        for spec in PAGE_SPECS:
            response = await client.get(spec.route)
            assert response.status_code == 303, (spec.code, response.text)
            assert response.headers["location"] == "/admin/login"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_every_registered_page_renders_a_complete_shell():
    """登录后逐页验证状态码、主内容、导航和无内部异常泄漏。"""
    client, _ = await _logged_in_client()
    try:
        for spec in PAGE_SPECS:
            response = await client.get(spec.route)
            expected = 404 if spec.code == "platform_admins" else 200
            assert response.status_code == expected, (spec.code, response.text)
            text = response.text
            assert "<main" in text
            assert "<h1" in text
            if expected == 404:
                assert 'class="error-shell"' in text
                assert "HTTP 404" in text
                assert 'aria-label="Admin navigation"' not in text
            else:
                assert 'id="main-content"' in text
                assert 'aria-label="Admin navigation"' in text
            assert "Traceback" not in text
            assert "Internal Server Error" not in text
            assert "sqlalchemy." not in text.lower()
    finally:
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


@pytest.mark.asyncio
async def test_agent_delete_is_blocked_when_workflow_references_it(db):
    """删除 Agent 不能留下一个必将在运行期失败的工作流。"""
    tag = uuid.uuid4().hex[:8]
    endpoint_code = f"full-llm-{tag}"
    agent_code = f"full-agent-{tag}"
    workflow_code = f"full-workflow-{tag}"
    await llm_endpoint_repo.create(
        db,
        code=endpoint_code,
        name="Full audit LLM",
        base_url="https://llm.example/v1",
        model="audit-model",
        api_key_cipher="cipher",
        priority=10,
        enabled=1,
    )
    await agent_repo.create(
        db,
        code=agent_code,
        tenant_id="default",
        name="Full audit agent",
        system_prompt="Audit this agent.",
        model="audit-model",
        llm_endpoint_code=endpoint_code,
        base_url="",
        api_key_cipher="",
        tools=[],
        skills=[],
        enabled=1,
    )
    await workflow_repo.create(
        db,
        code=workflow_code,
        name="Full audit workflow",
        description="References the agent.",
        definition=_workflow_definition(agent_code),
        enabled=1,
    )
    client, csrf = await _logged_in_client()
    try:
        page = await client.get("/admin/agents")
        assert workflow_code in page.text
        assert " disabled" in _button_tag(
            page.text, "Delete Agent Full audit agent"
        )

        response = await client.post(
            f"/admin/agents/{agent_code}/delete",
            data={"csrf_token": csrf},
        )
        assert response.status_code == 303
        assert "error=agent_in_use" in response.headers["location"]
        assert await agent_repo.get_by_code(db, agent_code, "default") is not None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_in_use_resources_show_references_and_lock_dangerous_actions(db):
    """LLM、Skill、MCP 的引用要在列表页可见，危险按钮应直接锁定。"""
    tag = uuid.uuid4().hex[:8]
    endpoint_code = f"locked-llm-{tag}"
    agent_code = f"locked-agent-{tag}"
    skill_code = f"locked-skill-{tag}"
    server_code = f"locked-mcp-{tag}"
    tool_name = f"mcp__{server_code}__lookup"
    await llm_endpoint_repo.create(
        db,
        code=endpoint_code,
        name="Locked LLM",
        base_url="https://locked.example/v1",
        model="locked-model",
        api_key_cipher="cipher",
        priority=10,
        enabled=1,
    )
    await skill_repo.create(
        db,
        code=skill_code,
        name="Locked Skill",
        description="Locked by an Agent.",
        body="Body",
        status="published",
        enabled=1,
        content_hash="hash",
    )
    server = await mcp_server_repo.create(
        db,
        code=server_code,
        tenant_id="default",
        name="Locked MCP",
        transport="http",
        url="https://locked-mcp.example/mcp",
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
                "description": "Locked tool",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
    )
    await agent_repo.create(
        db,
        code=agent_code,
        tenant_id="default",
        name="Locked Agent",
        system_prompt="Use locked resources.",
        model="locked-model",
        llm_endpoint_code=endpoint_code,
        base_url="",
        api_key_cipher="",
        tools=[tool_name],
        skills=[skill_code],
        enabled=1,
    )
    client, _ = await _logged_in_client()
    try:
        llm_page = await client.get("/admin/llm")
        assert agent_code in llm_page.text
        assert " disabled" in _button_tag(
            llm_page.text, "Delete LLM endpoint Locked LLM"
        )

        skill_page = await client.get("/admin/skills")
        assert agent_code in skill_page.text
        assert " disabled" in _button_tag(
            skill_page.text, "Archive Skill Locked Skill"
        )

        mcp_page = await client.get("/admin/mcp")
        assert agent_code in mcp_page.text
        assert " disabled" in _button_tag(
            mcp_page.text, "Delete MCP server Locked MCP"
        )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_destructive_actions_have_confirmation_and_context(db):
    """危险表单不能只有裸“删除/归档”，至少要确认并提供读屏上下文。"""
    tag = uuid.uuid4().hex[:8]
    endpoint_code = f"confirm-llm-{tag}"
    agent_code = f"confirm-agent-{tag}"
    skill_code = f"confirm-skill-{tag}"
    server_code = f"confirm-mcp-{tag}"
    await llm_endpoint_repo.create(
        db,
        code=endpoint_code,
        name="Confirm LLM",
        base_url="https://llm-confirm.example/v1",
        model="confirm-model",
        api_key_cipher="cipher",
        priority=10,
        enabled=1,
    )
    await agent_repo.create(
        db,
        code=agent_code,
        tenant_id="default",
        name="Confirm Agent",
        system_prompt="Confirm actions.",
        model="confirm-model",
        llm_endpoint_code=endpoint_code,
        base_url="",
        api_key_cipher="",
        tools=[],
        skills=[],
        enabled=1,
    )
    await skill_repo.create(
        db,
        code=skill_code,
        name="Confirm Skill",
        description="Confirm archive.",
        body="Body",
        status="published",
        enabled=1,
        content_hash="hash",
    )
    await mcp_server_repo.create(
        db,
        code=server_code,
        tenant_id="default",
        name="Confirm MCP",
        transport="http",
        url="https://mcp-confirm.example/mcp",
        enabled=1,
        auth_type="none",
    )
    client, _ = await _logged_in_client()
    try:
        expectations = {
            "/admin/agents": (f"Delete Agent Confirm Agent", f"Disable Agent Confirm Agent"),
            "/admin/llm": (f"Delete LLM endpoint Confirm LLM", f"Disable LLM endpoint Confirm LLM"),
            "/admin/skills": (f"Archive Skill Confirm Skill", None),
            "/admin/mcp": (f"Delete MCP server Confirm MCP", f"Disable MCP server Confirm MCP"),
        }
        for path, (delete_label, toggle_label) in expectations.items():
            page = await client.get(path)
            assert page.status_code == 200
            assert f'aria-label="{delete_label}"' in page.text, path
            # 删除走**页面级确认对话框**（UX-01）：行内按钮只是触发器，
            # 真正提交在对话框里的表单上。触发器必须带对象名与影响摘要。
            assert "data-confirm-trigger=" in page.text, path
            assert "data-confirm-dialog=" in page.text, path
            assert "data-confirm-title=" in page.text, path
            if toggle_label:
                assert f'aria-label="{toggle_label}"' in page.text, path
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_invalid_audit_filters_are_ignored_without_500():
    """URL 里的脏筛选值要被宽容归一，不能让审计页 500。"""
    client, _ = await _logged_in_client()
    try:
        response = await client.get(
            "/admin/audit?run_id=NaN&source=unknown&ok=maybe"
        )
        assert response.status_code == 200
        assert "Traceback" not in response.text
        assert response.text.count('<option value="">Any</option>') == 2
    finally:
        await client.aclose()


def test_capability_selector_covers_all_builtin_tools():
    """Agent 页面必须能选择工具注册表中的每个内置工具。"""
    from services.admin_web.views.agent_capabilities import capability_selector

    markup = capability_selector(
        skills=[],
        mcp_servers=[],
        mcp_tools=[],
    ).to_html()
    for name in tool_names():
        assert f'value="{name}"' in markup
