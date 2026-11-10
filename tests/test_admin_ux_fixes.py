"""本轮 P0/P1 修复的验收用例。

覆盖 2026-11-10 UX 评审里 P0/P1 各项的**可观察行为**：

- UX-01：删除要先过确认对话框，并且可以撤销；
- UX-03：校验失败保留输入并逐字段标错；
- UX-05：在用的连接显示引用数量并锁定危险操作；
- UX-06：列表支持搜索 / 排序 / 分页，且状态可分享（落在 URL 上）；
- UX-07：探测状态是可被读屏播报的活动区域；
- UX-09：编辑表单进独立面板，可用 ``?resource=`` 深链接；
- UX-19/UX-20：能力选择器有搜索、折叠、计数与建议差异。
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from app.models.agent import Agent
from app.models.admin_deleted_resource import AdminDeletedResource
from app.repo.agent_repo import agent_repo
from app.repo.llm_endpoint_repo import llm_endpoint_repo
from app.repo.skill_repo import skill_repo
from services.admin_web.listing import parse_list_params


async def _logged_in_client():
    from tests.test_admin_web import _create_seeded_logged_in_super_admin

    client, _, _, csrf = await _create_seeded_logged_in_super_admin()
    return client, csrf


async def _seed_endpoint(db, code: str, name: str) -> None:
    await llm_endpoint_repo.create(
        db,
        code=code,
        name=name,
        base_url="https://ux.example/v1",
        model="ux-model",
        api_key_cipher="cipher",
        priority=10,
        enabled=1,
    )


@pytest.mark.asyncio
async def test_delete_requires_confirmation_dialog_and_can_be_undone(db):
    """UX-01：删除不是单击即生效，且误删能在撤销窗口内恢复。"""
    tag = uuid.uuid4().hex[:8]
    code = f"undo-llm-{tag}"
    await _seed_endpoint(db, code, "Undo LLM")
    client, csrf = await _logged_in_client()
    try:
        page = await client.get("/admin/llm")
        assert page.status_code == 200
        # 触发器只是打开对话框，不是提交按钮。
        assert "data-confirm-trigger=" in page.text
        assert f'data-confirm-title="Delete LLM endpoint Undo LLM"' in page.text
        assert "data-confirm-dialog=" in page.text

        deleted = await client.post(
            f"/admin/llm/{code}/delete",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert deleted.status_code == 303
        assert "notice=deleted" in deleted.headers["location"]
        assert "undo=" in deleted.headers["location"]
        assert await llm_endpoint_repo.get_by_code(db, code) is None

        entry = await db.scalar(
            sa.select(AdminDeletedResource).where(
                AdminDeletedResource.resource_type == "llm_endpoint",
                AdminDeletedResource.resource_code == code,
            )
        )
        assert entry is not None

        undone = await client.post(
            f"/admin/undo/{entry.id}",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert undone.status_code == 303
        assert await llm_endpoint_repo.get_by_code(db, code) is not None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_agent_with_decimal_temperature_can_be_deleted(db):
    """回归：``temperature`` 是 DECIMAL 列，快照必须能存进 JSON 列。

    之前直接把行字段塞进 JSON，删除 Agent 会抛
    ``TypeError: Object of type Decimal is not JSON serializable``，
    删除动作直接 500——比不做撤销更糟。
    """
    tag = uuid.uuid4().hex[:8]
    code = f"decimal-agent-{tag}"
    endpoint_code = f"decimal-llm-{tag}"
    await _seed_endpoint(db, endpoint_code, "Decimal LLM")
    await agent_repo.create(
        db,
        code=code,
        tenant_id="default",
        name="Decimal Agent",
        system_prompt="Prompt.",
        model="ux-model",
        llm_endpoint_code=endpoint_code,
        base_url="",
        api_key_cipher="",
        tools=[],
        skills=[],
        enabled=1,
    )
    client, csrf = await _logged_in_client()
    try:
        deleted = await client.post(
            f"/admin/agents/{code}/delete",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert deleted.status_code == 303, deleted.text
        assert await db.scalar(
            sa.select(Agent).where(Agent.code == code)
        ) is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_invalid_input_is_kept_and_marked_per_field(db):
    """UX-03：校验失败回到原表单，已填内容还在，字段被标错。"""
    tag = uuid.uuid4().hex[:8]
    code = f"invalid-agent-{tag}"
    endpoint_code = f"invalid-llm-{tag}"
    await _seed_endpoint(db, endpoint_code, "Invalid LLM")
    client, csrf = await _logged_in_client()
    try:
        created = await client.post(
            "/admin/agents",
            data={
                "csrf_token": csrf,
                "code": code,
                "name": "",
                "llm_endpoint_code": endpoint_code,
                "model": "",
                "system_prompt": "",
                "enabled": "1",
            },
            follow_redirects=False,
        )
        assert created.status_code == 303
        assert "invalid=" in created.headers["location"]

        page = await client.get(created.headers["location"])
        assert page.status_code == 200
        assert "field-error" in page.text
        assert 'aria-invalid="true"' in page.text
        # 已填的编码还在（没有被丢掉）。
        assert code in page.text
        assert await db.scalar(
            sa.select(Agent).where(Agent.code == code)
        ) is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_list_search_sort_and_pagination_are_shareable(db):
    """UX-06：搜索 / 排序 / 分页在服务端生效，状态落在 URL 上。"""
    tag = uuid.uuid4().hex[:8]
    code = f"page-skill-{tag}"
    await skill_repo.create(
        db,
        code=code,
        name=code,
        description="Paged skill.",
        body="Body",
        status="published",
        enabled=1,
        content_hash="hash",
    )
    client, _ = await _logged_in_client()
    try:
        page = await client.get(f"/admin/skills?q={code}")
        assert page.status_code == 200
        assert code in page.text
        assert "data-list-total=" in page.text

        empty = await client.get("/admin/skills?q=no-such-skill-xyz")
        assert empty.status_code == 200
        assert code not in empty.text

        sorted_page = await client.get("/admin/skills?sort=name&page=1&size=20")
        assert sorted_page.status_code == 200
        assert "Sort by" in sorted_page.text
        assert "list-pager" in sorted_page.text
    finally:
        await client.aclose()


def test_list_params_tolerate_dirty_query():
    """UX-06：URL 里的脏值不能让页面 500，也不该被当成有效值。"""
    class _FakeRequest:
        def __init__(self, params):
            self.query_params = params

    params = parse_list_params(
        _FakeRequest({"q": " a ", "sort": "unknown", "page": "abc", "size": "-5"}),
        allowed_sorts=("name",),
    )
    assert params.q == "a"
    assert params.sort == ""
    assert params.page == 1
    assert params.size == 20


@pytest.mark.asyncio
async def test_edit_form_lives_in_a_deep_linkable_panel(db):
    """UX-09：编辑表单在独立面板里，同一时刻只有一个，可用 ?resource= 深链接。"""
    tag = uuid.uuid4().hex[:8]
    code = f"panel-skill-{tag}"
    await skill_repo.create(
        db,
        code=code,
        name=code,
        description="Panel skill.",
        body="Body",
        status="published",
        enabled=1,
        content_hash="hash",
    )
    client, _ = await _logged_in_client()
    try:
        listing = await client.get("/admin/skills")
        assert listing.status_code == 200
        # 列表页不再内联展开编辑表单。
        assert listing.text.count("data-edit-panel=") == 0
        assert f"resource={code}" in listing.text

        panel = await client.get(f"/admin/skills?resource={code}")
        assert panel.status_code == 200
        assert panel.text.count("data-edit-panel=") == 1
        assert "Skill body" in panel.text
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_legacy_agent_shows_masked_address_and_migration_hint(db):
    """UX-08：存量 Agent 要看得见旧地址（脱敏）与迁移指引。"""
    tag = uuid.uuid4().hex[:8]
    code = f"legacy-agent-{tag}"
    await agent_repo.create(
        db,
        code=code,
        tenant_id="default",
        name="Legacy Agent",
        system_prompt="Prompt.",
        model="legacy-model",
        llm_endpoint_code=None,
        # 旧配置里的地址常带凭据与路径：两者都不该出现在页面上。
        base_url="https://user:secret@legacy.example/v1/tenant?token=abc",
        api_key_cipher="",
        tools=[],
        skills=[],
        enabled=1,
    )
    client, _ = await _logged_in_client()
    try:
        page = await client.get("/admin/agents")
        assert page.status_code == 200
        assert "Legacy / manual" in page.text
        assert "https://legacy.example" in page.text
        # 凭据、路径与查询串都不回显。
        assert "secret" not in page.text
        assert "tenant" not in page.text
        assert "token=abc" not in page.text
        assert "Migrate by picking an LLM connection in the editor." in page.text
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_in_use_llm_endpoint_shows_reference_count(db):
    """UX-05：在用的连接直接显示被多少个 Agent 引用。"""
    tag = uuid.uuid4().hex[:8]
    endpoint_code = f"count-llm-{tag}"
    agent_code = f"count-agent-{tag}"
    await _seed_endpoint(db, endpoint_code, "Counted LLM")
    await agent_repo.create(
        db,
        code=agent_code,
        tenant_id="default",
        name="Counted Agent",
        system_prompt="Prompt.",
        model="ux-model",
        llm_endpoint_code=endpoint_code,
        base_url="",
        api_key_cipher="",
        tools=[],
        skills=[],
        enabled=1,
    )
    client, _ = await _logged_in_client()
    try:
        page = await client.get("/admin/llm")
        assert page.status_code == 200
        assert 'data-reference-count="1"' in page.text
        assert agent_code in page.text
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_llm_probe_status_is_announced(db):
    """UX-07：探测结果是可播报的活动区域，并带排查指引入口。"""
    client, _ = await _logged_in_client()
    try:
        page = await client.get("/admin/llm")
        assert page.status_code == 200
        assert 'role="status"' in page.text
        assert 'aria-live="polite"' in page.text
        assert "data-llm-help=" in page.text
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_capability_selector_supports_search_and_suggestions(db):
    """UX-19/UX-20：能力选择器有搜索、计数与建议差异展示。"""
    tag = uuid.uuid4().hex[:8]
    skill_code = f"cap-skill-{tag}"
    await skill_repo.create(
        db,
        code=skill_code,
        name=skill_code,
        description="Capability skill.",
        allowed_tools="calc, mcp__missing__ghost",
        body="Body",
        status="published",
        enabled=1,
        content_hash="hash",
    )
    client, _ = await _logged_in_client()
    try:
        page = await client.get("/admin/agents")
        assert page.status_code == 200
        assert "data-capability-search=" in page.text
        assert "data-capability-count=" in page.text
        assert 'aria-live="polite"' in page.text
        assert "data-capability-toggle=" in page.text
        # 建议与实际授权的差异被解析出来。
        assert "not granted to this agent" in page.text
        assert "tool does not exist" in page.text
    finally:
        await client.aclose()
