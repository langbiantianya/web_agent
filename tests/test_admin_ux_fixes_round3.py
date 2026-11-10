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


@pytest.mark.asyncio
async def test_platform_admin_delete_uses_confirm_dialog(db):
    """UX-01：平台管理员的删除也必须先过确认对话框。"""
    username = f"confirm_admin_{uuid.uuid4().hex[:8]}"
    await _seed_platform_admin(db, username)
    client, _account_id, csrf = await _owner_client()
    try:
        page = await client.get("/admin/platform-admins")
        assert page.status_code == 200, page.text
        # 删除按钮是**触发器**而不是提交按钮。
        assert "data-confirm-trigger=" in page.text
        assert "data-confirm-dialog=" in page.text
        # 旧的「单击即提交」写法不再存在。
        assert "data-confirm=" not in page.text
        # 对话框里写明删的是谁。
        assert f"Delete platform admin {username}" in page.text
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_platform_admin_delete_can_be_undone(db):
    """UX-01：平台管理员是硬删除，误删后要能原样恢复。"""
    client, _account_id, csrf = await _owner_client()
    try:
        # 造一个可删除的普通平台账号。
        username = f"undo_admin_{uuid.uuid4().hex[:8]}"
        account = await _seed_platform_admin(db, username)
        account_id = account.id

        deleted = await client.post(
            f"/admin/platform-admins/{account_id}/delete",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert deleted.status_code == 303, deleted.text
        assert "notice=deleted" in deleted.headers["location"]
        assert "undo=" in deleted.headers["location"]
        # 删除是在另一个会话里做的，本会话的 identity map 里有旧对象，
        # 先失效再查。
        db.expire_all()
        assert (
            await db.get(SuperAdminAccount, account_id) is None
        ), "账号没被删"

        entry = await db.scalar(
            sa.select(AdminDeletedResource).where(
                AdminDeletedResource.resource_type == "super_admin_account",
                AdminDeletedResource.resource_code == username,
            )
        )
        assert entry is not None, "没有留下可撤销的快照"

        undone = await client.post(
            f"/admin/undo/{entry.id}",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert undone.status_code == 303, undone.text
        db.expire_all()
        restored = await db.get(SuperAdminAccount, account_id)
        assert restored is not None, "撤销之后账号没有恢复"
        assert restored.username == username
    finally:
        await client.aclose()


# ---------------------------------------------------------------- UX-05


@pytest.mark.asyncio
async def test_undo_permission_follows_the_restored_resource_type(db):
    """UX-05：撤销一个 LLM 端点不该要求 Agent 编辑权。"""
    client, csrf = await _logged_in_client()
    try:
        code = f"undo-perm-{uuid.uuid4().hex[:8]}"
        await _seed_endpoint(db, code, "Undo Perm LLM")
        deleted = await client.post(
            f"/admin/llm/{code}/delete",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert deleted.status_code == 303, deleted.text
        entry = await db.scalar(
            sa.select(AdminDeletedResource).where(
                AdminDeletedResource.resource_code == code
            )
        )
        assert entry is not None
        undone = await client.post(
            f"/admin/undo/{entry.id}",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert undone.status_code == 303, undone.text
        assert "notice=restored" in undone.headers["location"]
        assert await llm_endpoint_repo.get_by_code(db, code) is not None
    finally:
        await client.aclose()


# ---------------------------------------------------------------- UX-03


@pytest.mark.asyncio
async def test_workflow_save_returns_errors_instead_of_navigating_away(db):
    """UX-03：保存不合法的工作流时，画布留在原地并带回逐条错误。"""
    from app.repo.workflow_repo import workflow_repo

    code = f"ux3-wf-{uuid.uuid4().hex[:8]}"
    await workflow_repo.create(
        db,
        code=code,
        name="UX3 Workflow",
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
                "name": "UX3 Workflow",
                "description": "",
                "enabled": "1",
                "locale": "zh-CN",
                "definition": (
                    '{"entry":"step1","nodes":[{"id":"step1","type":"agent",'
                    '"agentCode":"missing-agent"}],"edges":[]}'
                ),
            },
            headers={"Accept": "application/json"},
            follow_redirects=False,
        )
        assert response.status_code == 400, response.text
        payload = response.json()
        assert payload["ok"] is False
        assert payload["errors"], "没有回传任何错误"
        assert any(
            "missing-agent" in str(item.get("message", ""))
            for item in payload["errors"]
        ), payload["errors"]
    finally:
        await client.aclose()


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


@pytest.mark.asyncio
async def test_llm_connection_test_does_not_require_models_endpoint(db, monkeypatch):
    """LLM-05：能对话就能通过，``/models`` 不可用不应误判为失败。"""
    import services.admin_web.routes.llm_write as llm_routes

    code = f"probe-only-chat-{uuid.uuid4().hex[:8]}"
    await _seed_endpoint(db, code, "Chat Only LLM")
    client, csrf = await _logged_in_client()
    try:
        async def no_models(base_url: str, api_key: str):
            raise LlmProbeError("GET /models failed: HTTP 404", status_code=404)

        async def ok_chat(base_url: str, api_key: str, model: str):
            return ProbeResult(latency_ms=12, model=model, reply="pong")

        monkeypatch.setattr(llm_routes, "fetch_models", no_models)
        monkeypatch.setattr(llm_routes, "probe_chat", ok_chat)
        response = await client.post(
            "/admin/llm/probe",
            data={"csrf_token": csrf, "mode": "test", "code": code},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["ok"] is True
        assert payload["latency_ms"] == 12
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_llm_probe_error_does_not_echo_upstream_body(db, monkeypatch):
    """LLM-06：探测失败不回显上游原始报文。"""
    import httpx

    import services.admin_web.routes.llm_write as llm_routes
    from services.admin_web.llm_probe import _http_error

    code = f"probe-leak-{uuid.uuid4().hex[:8]}"
    await _seed_endpoint(db, code, "Leak LLM")
    client, csrf = await _logged_in_client()
    try:
        async def leaky_models(base_url: str, api_key: str):
            response = httpx.Response(
                401,
                text='{"error":{"message":"Your api key: sk-1234 is invalid"}}',
                request=httpx.Request("GET", f"{base_url}/models"),
            )
            raise _http_error("GET /models", response)

        monkeypatch.setattr(llm_routes, "fetch_models", leaky_models)
        response = await client.post(
            "/admin/llm/probe",
            data={"csrf_token": csrf, "mode": "models", "code": code},
        )
        assert response.status_code == 400
        error = response.json()["error"]
        assert "sk-1234" not in error
    finally:
        await client.aclose()


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
