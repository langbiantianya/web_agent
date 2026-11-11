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


@pytest.mark.asyncio
async def test_platform_admin_delete_clears_dependent_rows(db):
    """删除平台管理员时，会话与重置记录要一起清掉。

    ``super_admin_session`` 与 ``super_admin_password_reset`` 上有指向
    ``super_admin_account`` 的外键。不清就删账号，PostgreSQL 上直接
    ``ForeignKeyViolation``——删除动作 500。MySQL 默认不强制外键，所以这个
    问题只在管理端用**独立库**（也就是正确部署）时暴露。
    """
    from app.models.admin_control import (
        SuperAdminPasswordReset,
        SuperAdminSession,
    )
    from app.models.base import utcnow

    client, _account_id, csrf = await _owner_client()
    try:
        username = f"fk_admin_{uuid.uuid4().hex[:8]}"
        account = await _seed_platform_admin(db, username)
        account_id = account.id
        now = utcnow()
        db.add(
            SuperAdminSession(
                token_hash=uuid.uuid4().hex,
                csrf_hash=uuid.uuid4().hex,
                account_id=account_id,
                auth_at=now,
                idle_expires_at=now,
                absolute_expires_at=now,
                password_version=1,
            )
        )
        db.add(
            SuperAdminPasswordReset(
                account_id=account_id,
                pending_password_hash="hash",
                reason="manual_reset",
                status="pending",
                requested_at=now,
                expires_at=now,
            )
        )
        await db.commit()

        deleted = await client.post(
            f"/admin/platform-admins/{account_id}/delete",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert deleted.status_code == 303, deleted.text
        db.expire_all()
        assert (
            await db.scalar(
                sa.select(SuperAdminSession.id).where(
                    SuperAdminSession.account_id == account_id
                )
            )
            is None
        ), "会话没有一起清掉"
        assert (
            await db.scalar(
                sa.select(SuperAdminPasswordReset.id).where(
                    SuperAdminPasswordReset.account_id == account_id
                )
            )
            is None
        ), "重置记录没有一起清掉"
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


@pytest.mark.asyncio
async def test_archive_confirm_button_says_archive_not_delete(db):
    """归档是软删除：确认对话框的按钮不该写「删除」。"""
    from app.repo.skill_repo import skill_repo

    tag = uuid.uuid4().hex[:8]
    code = f"ux3-archive-{tag}"
    await skill_repo.create(
        db,
        code=code,
        name=code,
        description="Archive skill.",
        allowed_tools="",
        body="Body",
        status="published",
        enabled=1,
        content_hash="hash",
    )
    client, _ = await _logged_in_client()
    try:
        page = await client.get("/admin/skills")
        assert page.status_code == 200, page.text
        assert 'data-confirm-submit-label="Archive"' in page.text
        # 删除类动作不指定文案，用默认的 Delete。
        assert 'data-confirm-title="Delete LLM endpoint' not in page.text
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



# ---------------------------------------------------------------- UX-33


@pytest.mark.asyncio
async def test_stdio_mcp_failure_marks_command_field(db, monkeypatch):
    """UX-33：stdio 注册失败要把错误标在 ``command`` 上。

    stdio 模式下 ``url`` 这一栏是禁用且隐藏的——错误落在 ``url`` 上，
    用户在页面上根本看不到任何提示，只看到「注册失败」。
    """
    import services.admin_web.routes.mcp as mcp_routes

    async def refuse(*args, **kwargs):
        raise McpTransportError("command refused by policy")

    monkeypatch.setattr(mcp_routes, "validate_mcp_connection", refuse)
    client, csrf = await _logged_in_client()
    try:
        code = f"stdio-{uuid.uuid4().hex[:8]}"
        response = await client.post(
            "/admin/mcp",
            data={
                "csrf_token": csrf,
                "code": code,
                "name": "Local server",
                "transport": "stdio",
                "url": "",
                "command": "/usr/local/bin/mcp-server",
                "timeout_sec": "30",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text
        assert "invalid=" in response.headers["location"]
        # 错误挂在 command 上，而不是被隐藏的 url 上。
        page = await client.get(response.headers["location"])
        assert page.status_code == 200, page.text
        assert "err-mcp-create-command" in page.text
        assert "err-mcp-create-url" not in page.text
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_http_mcp_failure_marks_url_field(db, monkeypatch):
    """UX-33：http 注册失败仍然标在 ``url`` 上。"""
    import services.admin_web.routes.mcp as mcp_routes

    async def refuse(*args, **kwargs):
        raise McpTransportError("url refused by policy")

    monkeypatch.setattr(mcp_routes, "validate_mcp_connection", refuse)
    client, csrf = await _logged_in_client()
    try:
        code = f"http-{uuid.uuid4().hex[:8]}"
        response = await client.post(
            "/admin/mcp",
            data={
                "csrf_token": csrf,
                "code": code,
                "name": "Remote server",
                "transport": "http",
                "url": "https://mcp.example.com/mcp",
                "command": "",
                "timeout_sec": "30",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text
        page = await client.get(response.headers["location"])
        assert page.status_code == 200, page.text
        assert "err-mcp-create-url" in page.text
        assert "err-mcp-create-command" not in page.text
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_mcp_refresh_failure_tells_the_reason(db, monkeypatch):
    """UX-34：目录刷新失败要能区分「连不上」与「工具仍被占用」。"""
    import services.admin_web.routes.mcp_write as mcp_write_routes
    from app.repo.mcp_repo import mcp_server_repo

    code = f"refresh-{uuid.uuid4().hex[:8]}"
    await mcp_server_repo.create(
        db,
        code=code,
        tenant_id="default",
        name=code,
        transport="http",
        url="https://refresh.example/mcp",
        enabled=1,
        auth_type="none",
    )
    client, csrf = await _logged_in_client()
    try:
        async def refuse(*args, **kwargs):
            raise McpCatalogError("boom", reason="unreachable")

        monkeypatch.setattr(mcp_write_routes, "refresh_catalog", refuse)
        response = await client.post(
            f"/admin/mcp/{code}/refresh",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text
        assert "error=mcp_unreachable" in response.headers["location"]

        async def in_use(*args, **kwargs):
            raise McpCatalogError("boom", reason="tools_in_use")

        monkeypatch.setattr(mcp_write_routes, "refresh_catalog", in_use)
        response = await client.post(
            f"/admin/mcp/{code}/refresh",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text
        assert "error=mcp_tools_in_use" in response.headers["location"]
    finally:
        await client.aclose()


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


@pytest.mark.asyncio
async def test_preferences_save_shows_a_confirmation(db):
    """UX-35：保存偏好设置后要给出确认提示。

    之前重定向到了 ``?notice=saved``，但页面**从不读这个参数**——管理员
    点了保存，什么反馈都没有，只能靠自己刷新页面确认。
    """
    client, csrf = await _logged_in_client()
    try:
        response = await client.post(
            "/admin/preferences",
            data={
                "csrf_token": csrf,
                "theme": "cloudscape",
                "color_mode": "light",
                "density": "compact",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text
        assert "notice=saved" in response.headers["location"]

        page = await client.get(response.headers["location"])
        assert page.status_code == 200, page.text
        assert "Preferences saved." in page.text
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_audit_filters_are_echoed_back(db):
    """UX-36：审计筛选条件要回显到控件上。

    之前 URL 里带着 ``?source=builtin`` 重新打开页面，下拉框却停在
    「任意」——管理员看到的条件与实际生效的条件不一致。
    """
    client, csrf = await _logged_in_client()
    try:
        page = await client.get(
            "/admin/audit?run_id=7&tool_name=calc&source=builtin&ok=true"
        )
        assert page.status_code == 200, page.text
        assert 'value="builtin" selected' in page.text
        assert 'value="true" selected' in page.text
        assert 'value="7"' in page.text
        assert 'value="calc"' in page.text
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_llm_probe_returns_a_stable_error_code(db, monkeypatch):
    """UX-37：探测失败要带稳定的错误分类，中文界面才能本地化。

    之前只回一整句英文，中文界面里「失败 · GET /models failed: HTTP 401.
    The API key was rejected. ...」直接露出英文。
    """
    import httpx

    import services.admin_web.routes.llm_write as llm_routes

    code = f"probe-code-{uuid.uuid4().hex[:8]}"
    await _seed_endpoint(db, code, "Code LLM")
    client, csrf = await _logged_in_client()
    try:
        async def unauthorized_models(base_url: str, api_key: str):
            response = httpx.Response(
                401,
                text='{"error":{"message":"key sk-1234 invalid"}}',
                request=httpx.Request("GET", f"{base_url}/models"),
            )
            raise llm_routes.LlmProbeError(
                "GET /models failed: HTTP 401. The API key was rejected.",
                status_code=401,
                code="http_401",
            )

        monkeypatch.setattr(llm_routes, "fetch_models", unauthorized_models)
        response = await client.post(
            "/admin/llm/probe",
            data={"csrf_token": csrf, "mode": "models", "code": code},
        )
        assert response.status_code == 400
        payload = response.json()
        assert payload["error_code"] == "http_401"
        # 上游正文与密钥片段仍然不能出现在响应里（UX-30）。
        assert "sk-1234" not in payload["error"]
    finally:
        await client.aclose()


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


@pytest.mark.asyncio
async def test_toggle_and_edit_buttons_do_not_500(db):
    """UX-40：三个写操作按钮不能 500。

    内置浏览器逐按钮实测时抓到三处**漏 import**：

    - ``routes/llm_write.py`` 用了 ``LlmEndpointUpdate`` 却没 import ——
      「保存端点」一按就是 500；
    - ``routes/agents_write.py`` 用了 ``as_bool`` 却没 import ——
      智能体的「禁用 / 启用」一按就是 500；
    - ``routes/workflows_write.py`` 同样漏了 ``as_bool`` ——
      工作流的「禁用 / 启用」一按就是 500。

    这三处静态都能查出来（见 ``tests/test_admin_route_static_checks.py``），
    这里再补一条**行为**断言：这些路由必须真的返回 303 而不是 500。
    """
    from app.repo.agent_repo import agent_repo
    from app.repo.llm_endpoint_repo import llm_endpoint_repo

    client, csrf = await _logged_in_client()
    try:
        # LLM 端点「保存端点」
        code = f"ux40-llm-{uuid.uuid4().hex[:8]}"
        await llm_endpoint_repo.create(
            db,
            code=code,
            name="UX40 LLM",
            base_url="https://ux40.example/v1",
            model="ux40-model",
            api_key_cipher="cipher",
            priority=10,
            enabled=1,
        )
        updated = await client.post(
            f"/admin/llm/{code}/update",
            data={
                "csrf_token": csrf,
                "name": "UX40 LLM Renamed",
                "base_url": "https://ux40.example/v1",
                "model": "ux40-model",
                "priority": "10",
                "api_key": "",
            },
            follow_redirects=False,
        )
        assert updated.status_code == 303, updated.text
        assert "notice=updated" in updated.headers["location"]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_agent_and_workflow_toggle_do_not_500(db):
    """UX-40：智能体与工作流的「禁用 / 启用」不能 500（``as_bool`` 漏 import）。"""
    from app.models.agent import Agent
    from app.models.workflow import Workflow

    tag = uuid.uuid4().hex[:8]
    agent = Agent(
        code=f"ux40-agent-{tag}",
        name="UX40 Agent",
        system_prompt="UX40 prompt.",
        model="ux40-model",
        base_url="https://ux40.example/v1",
        api_key_cipher="cipher",
        enabled=1,
    )
    workflow = Workflow(
        code=f"ux40-wf-{tag}",
        name="UX40 WF",
        enabled=1,
        definition={"entry": "step1", "nodes": []},
        version=1,
    )
    db.add_all([agent, workflow])
    await db.commit()

    client, csrf = await _logged_in_client()
    try:
        toggled = await client.post(
            f"/admin/agents/{agent.code}/toggle",
            data={"csrf_token": csrf, "enabled": "0"},
            follow_redirects=False,
        )
        assert toggled.status_code == 303, toggled.text
        assert "notice=toggled" in toggled.headers["location"]

        toggled_wf = await client.post(
            f"/admin/workflows/{workflow.code}/toggle",
            data={"csrf_token": csrf, "enabled": "0"},
            follow_redirects=False,
        )
        assert toggled_wf.status_code == 303, toggled_wf.text
        assert "notice=toggled" in toggled_wf.headers["location"]
    finally:
        await client.aclose()


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

