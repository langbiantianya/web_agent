"""管理端 Agent 与 LLM 连接联动测试。"""

from __future__ import annotations

import re
import uuid

import httpx
import pytest
import sqlalchemy as sa

from app.models.agent import Agent
from app.repo.llm_endpoint_repo import llm_endpoint_repo
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
    """测试账号创建只需要成功回执，不发送真实通知。"""
    return DeliveryResult(email_status="sent", sms_status="skipped")


async def _logged_in_client() -> tuple[httpx.AsyncClient, str]:
    """建立 platform super admin 会话。"""
    tag = uuid.uuid4().hex[:10]
    async with AdminSessionLocal() as db:
        await seed_registry(db)
        account, _ = await create_super_admin_account(
            db,
            username=f"admin_{tag}",
            email=f"admin-{tag}@example.com",
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
async def test_agent_selects_managed_llm_connection_and_uses_default_model(db):
    """Agent 表单只接受已存在连接，空模型采用该连接的默认模型。"""
    tag = uuid.uuid4().hex[:8]
    primary = f"primary-{tag}"
    backup = f"backup-{tag}"
    await llm_endpoint_repo.create(
        db,
        code=primary,
        name="Primary connection",
        base_url="https://primary.example/v1",
        model="primary-model",
        api_key_cipher="encrypted-key",
        priority=10,
        enabled=1,
    )
    await llm_endpoint_repo.create(
        db,
        code=backup,
        name="Backup connection",
        base_url="https://backup.example/v1",
        model="backup-model",
        api_key_cipher="encrypted-key",
        priority=20,
        enabled=1,
    )
    client, csrf = await _logged_in_client()
    agent_code = f"agent-{tag}"
    try:
        page = await client.get("/admin/agents")
        assert page.status_code == 200
        assert "Primary connection" in page.text
        assert "Backup connection" in page.text
        assert 'name="llm_endpoint_code"' in page.text
        assert re.search(
            r'<select[^>]*name="llm_endpoint_code"[^>]*required="required"',
            page.text,
        )
        assert (
            '<option value="">Select a configured LLM connection</option>'
            in page.text
        )
        assert 'name="base_url"' not in page.text
        assert 'name="api_key"' not in page.text

        created = await client.post(
            "/admin/agents",
            data={
                "csrf_token": csrf,
                "code": agent_code,
                "name": "Linked agent",
                "llm_endpoint_code": primary,
                "model": "",
                "tools": "",
                "skills": "",
                "system_prompt": "Use the selected connection.",
                "enabled": "1",
            },
        )
        assert created.status_code == 303
        assert "notice=created" in created.headers["location"]

        stored = await db.scalar(
            sa.select(Agent).where(Agent.code == agent_code)
        )
        assert stored is not None
        assert stored.llm_endpoint_code == primary
        assert stored.model == "primary-model"
        assert stored.base_url == ""

        updated = await client.post(
            f"/admin/agents/{agent_code}/update",
            data={
                "csrf_token": csrf,
                "name": "Linked agent updated",
                "llm_endpoint_code": backup,
                "model": "",
                "tools": "",
                "skills": "",
                "system_prompt": "Use the backup connection.",
                "enabled": "1",
            },
        )
        assert updated.status_code == 303
        assert "notice=updated" in updated.headers["location"]
        await db.refresh(stored)
        assert stored.llm_endpoint_code == backup
        assert stored.model == "backup-model"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_in_use_llm_connection_cannot_be_disabled_or_deleted(db):
    """仍被 Agent 引用的连接不能从 LLM 页面停用或删除。"""
    tag = uuid.uuid4().hex[:8]
    endpoint_code = f"managed-{tag}"
    agent_code = f"managed-agent-{tag}"
    await llm_endpoint_repo.create(
        db,
        code=endpoint_code,
        name="Managed connection",
        base_url="https://managed.example/v1",
        model="managed-model",
        api_key_cipher="encrypted-key",
        priority=10,
        enabled=1,
    )
    client, csrf = await _logged_in_client()
    try:
        created = await client.post(
            "/admin/agents",
            data={
                "csrf_token": csrf,
                "code": agent_code,
                "name": "Managed agent",
                "llm_endpoint_code": endpoint_code,
                "model": "managed-model",
                "tools": "",
                "skills": "",
                "system_prompt": "Managed.",
                "enabled": "1",
            },
        )
        assert created.status_code == 303

        disabled = await client.post(
            f"/admin/llm/{endpoint_code}/toggle",
            data={"csrf_token": csrf, "enabled": "0"},
        )
        assert disabled.status_code == 303
        assert "error=in_use" in disabled.headers["location"]

        deleted = await client.post(
            f"/admin/llm/{endpoint_code}/delete",
            data={"csrf_token": csrf},
        )
        assert deleted.status_code == 303
        assert "error=in_use" in deleted.headers["location"]
        endpoint = await llm_endpoint_repo.get_by_code(db, endpoint_code)
        assert endpoint is not None and endpoint.enabled
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_agent_rejects_missing_or_model_less_llm_connection(db):
    """不存在、已停用或没有可用模型的连接都不能保存。"""
    tag = uuid.uuid4().hex[:8]
    empty_model = f"empty-model-{tag}"
    await llm_endpoint_repo.create(
        db,
        code=empty_model,
        name="Empty model",
        base_url="https://empty.example/v1",
        model="",
        api_key_cipher="encrypted-key",
        priority=10,
        enabled=1,
    )
    client, csrf = await _logged_in_client()
    try:
        missing = await client.post(
            "/admin/agents",
            data={
                "csrf_token": csrf,
                "code": f"missing-{tag}",
                "name": "Missing",
                "llm_endpoint_code": f"does-not-exist-{tag}",
                "model": "some-model",
                "tools": "",
                "skills": "",
                "system_prompt": "Missing.",
                "enabled": "1",
            },
        )
        # 校验失败不再退回一句通用 Flash，而是带着**字段级**错误回到
        # 原页面，已填内容也保留（UX-03）。
        assert "invalid=" in missing.headers["location"]
        page = await client.get(missing.headers["location"])
        assert page.status_code == 200
        assert "Select an enabled LLM connection." in page.text
        assert "Missing." in page.text

        no_model = await client.post(
            "/admin/agents",
            data={
                "csrf_token": csrf,
                "code": f"no-model-{tag}",
                "name": "No model",
                "llm_endpoint_code": empty_model,
                "model": "",
                "tools": "",
                "skills": "",
                "system_prompt": "No model.",
                "enabled": "1",
            },
        )
        assert "invalid=" in no_model.headers["location"]
        page = await client.get(no_model.headers["location"])
        assert page.status_code == 200
        assert "This connection has no default model; enter one." in page.text
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_agent_page_does_not_offer_disabled_llm_connection(db):
    """停用连接仍可在已有行中显示状态，但不能作为新的下拉候选。"""
    tag = uuid.uuid4().hex[:8]
    endpoint_code = f"disabled-{tag}"
    await llm_endpoint_repo.create(
        db,
        code=endpoint_code,
        name="Disabled connection",
        base_url="https://disabled.example/v1",
        model="disabled-model",
        api_key_cipher="encrypted-key",
        priority=10,
        enabled=0,
    )
    client, _ = await _logged_in_client()
    try:
        page = await client.get("/admin/agents")
        assert page.status_code == 200
        assert f'value="{endpoint_code}"' not in page.text
    finally:
        await client.aclose()
