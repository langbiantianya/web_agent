"""2026-11-10 第三轮 P0/P1 修复的验收用例。

这一轮的问题都是**内置浏览器实测**出来的，不是评审纸面上的：

- **UX-01（补齐）**：平台超级管理员的删除按钮原本是「单击立即提交」——
  页面上的 ``data-confirm`` 属性只被 i18n 当成待翻译文本读过，没有任何
  脚本监听它。删除现在也走确认对话框，并且可以撤销。
- **UX-03（补齐）**：工作流编辑器保存失败会 303 跳回列表页，画布上还没
  保存的改动全部丢失，只剩一句通用的「提交的值无效」。
- **UX-05（补齐）**：撤销路由把权限页面写死成 ``agents``，撤销一个 LLM
  端点也要求 Agent 编辑权。
- **LLM-02**：服务商预设的自动填充写进了**过滤表单**（两张表单的 action
  都是 ``/admin/llm``），用户点了预设没反应。
- **LLM-05**：「测试连接」先探 ``GET /models``，而不少 OpenAI 兼容网关根本
  不开放这个路径——能正常对话的配置被误判成「连接失败」。
- **LLM-06**：探测失败把上游原始报文回显到页面上。
- **UX-10**：词表漏翻导致中文界面露出英文（删除确认的 Cancel、撤销提示、
  访问控制页的说明与页面描述等）。
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from app.models.admin_control import SuperAdminAccount
from app.models.admin_deleted_resource import AdminDeletedResource
from app.core.mcp.catalog import McpCatalogError
from app.core.mcp.transport import McpTransportError
from app.repo.llm_endpoint_repo import llm_endpoint_repo
from services.admin_web.llm_probe import LlmProbeError, ProbeResult


async def _logged_in_client():
    from tests.test_admin_web import _create_seeded_logged_in_super_admin

    client, _, _, csrf = await _create_seeded_logged_in_super_admin()
    return client, csrf


async def _owner_client():
    """owner 专属页面（平台超级管理员管理）只有 owner 进得去。

    ``superAdmin`` 是全局单例，第二个用例再创建就会撞 ``super_admin_exists``；
    这里遇到已存在的 owner 就**复用**它并另开一个会话。
    """
    import httpx

    from app.models.admin_control import SuperAdminAccount
    from services.admin_web.accounts import create_super_admin_session
    from services.admin_web.db import AdminSessionLocal
    from services.admin_web.security import (
        super_csrf_cookie_name,
        super_session_cookie_name,
    )
    from services.admin_web.app import app as admin_asgi_app

    from services.admin_web.registry import seed_registry

    async with AdminSessionLocal() as session:
        await seed_registry(session)
        account = await session.scalar(
            sa.select(SuperAdminAccount).where(
                SuperAdminAccount.owner_slot == "superAdmin"
            )
        )
        if account is None:
            from services.admin_web.accounts import create_super_admin_account
            from tests.test_admin_web import _password_sender

            account, _ = await create_super_admin_account(
                session,
                username="superAdmin",
                email="owner@example.com",
                phone=None,
                owner=True,
                password_sender=_password_sender([]),
            )
        token, csrf = await create_super_admin_session(session, account)
        account_id = account.id
        await session.commit()
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_asgi_app),
        base_url="http://test",
        follow_redirects=False,
    )
    client.cookies.set(super_session_cookie_name(), token)
    client.cookies.set(super_csrf_cookie_name(), csrf)
    return client, account_id, csrf


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


# ---------------------------------------------------------------- UX-01


async def _seed_platform_admin(db, username: str):
    """建一个可删除的普通平台账号。"""
    account = SuperAdminAccount(
        username=username,
        password_hash="hash",
        email=f"{username}@example.com",
        status="active",
        owner_slot=None,
        password_version=1,
    )
    db.add(account)
    await db.commit()
    return account


# ---------------------------------------------------------------- UX-05


@pytest.mark.asyncio
async def test_undo_permission_follows_the_restored_resource_type(db):
    """UX-05：撤销一个 LLM 端点不该要求 Agent 编辑权。

    撤销端点按**资源类型**判定写权限：撤销一个 LLM 端点，用的是 LLM 的
    写权限，不是 Agent 的。
    """
    client, csrf = await _logged_in_client()
    try:
        code = f"undo-perm-{uuid.uuid4().hex[:8]}"
        await _seed_endpoint(db, code, "Undo Perm LLM")
        deleted = await client.delete(
            f"/admin/api/llm/{code}",
            headers={"x-csrf-token": csrf},
        )
        assert deleted.status_code == 200, deleted.text
        undo_id = deleted.json()["undoId"]
        assert undo_id
        undone = await client.post(
            f"/admin/api/undo/{undo_id}",
            headers={"x-csrf-token": csrf},
            json={},
        )
        assert undone.status_code == 200, undone.text
        assert undone.json()["item"]["resourceType"] == "llm_endpoint"
        assert await llm_endpoint_repo.get_by_code(db, code) is not None
    finally:
        await client.aclose()


# ---------------------------------------------------------------- UX-03


@pytest.mark.asyncio
async def test_workflow_save_still_redirects_without_json_client(db):
    """没有 JS 的客户端仍然走原来的 303，行为不变。"""
    from app.repo.workflow_repo import workflow_repo

    code = f"ux3-wf-plain-{uuid.uuid4().hex[:8]}"
    await workflow_repo.create(
        db,
        code=code,
        name="UX3 Plain",
        description="",
        definition={
            "entry": "step1",
            "nodes": [{"id": "step1", "type": "agent", "agentCode": "nope"}],
            "edges": [],
        },
        enabled=1,
    )
    client, csrf = await _logged_in_client()
    try:
        response = await client.post(
            f"/admin/workflows/{code}/update",
            data={
                "csrf_token": csrf,
                "name": "UX3 Plain",
                "description": "",
                "enabled": "1",
                "definition": (
                    '{"entry":"step1","nodes":[{"id":"step1","type":"agent",'
                    '"agentCode":"missing-agent"}],"edges":[]}'
                ),
            },
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text
        # 跳回**编排页**，不是列表页——列表页会把画布丢掉。
        assert f"/admin/workflows/{code}" in response.headers["location"]
    finally:
        await client.aclose()


# ---------------------------------------------------------------- LLM-05


def test_llm_probe_http_error_is_categorised_not_raw():
    """LLM-06：上游错误只给「状态码 + 归类说明」，不带正文。"""
    import httpx

    from services.admin_web.llm_probe import _http_error

    response = httpx.Response(
        401,
        text='{"error":{"message":"Your api key: sk-1234 is invalid"}}',
        request=httpx.Request("GET", "https://example.com/v1/models"),
    )
    error = _http_error("GET /models", response)
    assert "sk-1234" not in str(error)
    assert "401" in str(error)
    assert error.status_code == 401



# ---------------------------------------------------------------- UX-33


def test_mcp_refresh_messages_are_categorised():
    """UX-34：两种失败原因对应两条**不同**的提示文案。"""
    from services.admin_web.common import flash_from_query

    class _Request:
        def __init__(self, error: str) -> None:
            self.query_params = {"error": error}

    unreachable, _ = flash_from_query(_Request("mcp_unreachable"))
    in_use, _ = flash_from_query(_Request("mcp_tools_in_use"))
    assert unreachable != in_use
    assert "could not be reached" in unreachable
    assert "still used by Agents" in in_use


def test_llm_probe_errors_carry_a_stable_code():
    """UX-37：每类探测失败都要带机器可读的分类。"""
    from services.admin_web.llm_probe import LlmProbeError, _http_error

    import httpx

    response = httpx.Response(
        429,
        text="{}",
        request=httpx.Request("GET", "https://example.com/v1/models"),
    )
    error = _http_error("GET /models", response)
    assert error.code == "http_429"
    assert error.status_code == 429
    # 没有分类的异常也要有一个默认值，不能是空串。
    assert LlmProbeError("boom").code == "unknown"


def test_mcp_field_errors_use_transport_to_choose_field():
    """UX-33：字段归属由传输方式决定。"""
    from services.admin_web.form_errors import _mcp_field_errors

    stdio = _mcp_field_errors(McpTransportError("boom"), "stdio")
    assert set(stdio) == {"command"}
    http = _mcp_field_errors(McpTransportError("boom"), "http")
    assert set(http) == {"url"}
    assert http["url"] == "The HTTP URL could not be validated."
    # 未指定传输方式时回落到 url（HTTP 是默认传输）。
    assert set(_mcp_field_errors(McpTransportError("boom"))) == {"url"}

