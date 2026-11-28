"""管理面 JSON API —— 技能资源（V4.10）。

锁住三件事：修订快照（每次更新都要留下可回滚的旧正文）、仍在被
智能体引用时不能停用/归档，以及「正文改了就要重算 content_hash」。
"""

from __future__ import annotations

import uuid

import httpx
import pytest

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
            username=f"api_{tag}",
            email=f"api-{tag}@example.com",
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


def _headers(client: httpx.AsyncClient) -> dict[str, str]:
    """取 CSRF 头。"""
    return {"x-csrf-token": client.cookies.get(super_csrf_cookie_name()) or ""}


from app.repo.agent_repo import agent_repo
from app.repo.skill_repo import skill_repo


async def test_skill_crud_round_trip() -> None:
    """创建 → 读取 → 更新 → 归档。"""
    client = await _client()
    code = f"skill-{uuid.uuid4().hex[:8]}"
    try:
        created = await client.post(
            "/admin/api/skills",
            headers=_headers(client),
            json={
                "code": code,
                "description": "写文档的技能",
                "allowedTools": ["fs_read", "fs_write"],
                "body": "# 技能正文",
                "status": "published",
                "enabled": True,
            },
        )
        assert created.status_code == 201, created.text
        item = created.json()["item"]
        assert item["code"] == code
        assert item["allowedTools"] == ["fs_read", "fs_write"]
        assert item["status"] == "published"

        fetched = await client.get(f"/admin/api/skills/{code}")
        assert fetched.status_code == 200
        assert fetched.json()["item"]["body"] == "# 技能正文"

        updated = await client.put(
            f"/admin/api/skills/{code}",
            headers=_headers(client),
            json={
                "description": "改过的描述",
                "allowedTools": ["fs_read"],
                "body": "# 新正文",
                "status": "published",
                "enabled": True,
            },
        )
        assert updated.status_code == 200, updated.text
        updated_item = updated.json()["item"]
        assert updated_item["description"] == "改过的描述"
        assert updated_item["body"] == "# 新正文"
        # 正文变了，content_hash 必须跟着变。
        assert updated_item["contentHash"] != item["contentHash"]
    finally:
        await client.aclose()


async def test_skill_update_writes_revision() -> None:
    """每次更新都要留下可回滚的旧正文。"""
    client = await _client()
    code = f"skill-{uuid.uuid4().hex[:8]}"
    try:
        await client.post(
            "/admin/api/skills",
            headers=_headers(client),
            json={
                "code": code,
                "description": "初版",
                "body": "v1",
                "status": "published",
                "enabled": True,
            },
        )
        await client.put(
            f"/admin/api/skills/{code}",
            headers=_headers(client),
            json={
                "description": "第二版",
                "body": "v2",
                "status": "published",
                "enabled": True,
            },
        )
        async with AdminSessionLocal() as db:
            row = await skill_repo.get_by_code(db, code)
            assert row is not None
            revisions = await skill_repo.list_revisions(db, row.id)
            assert len(revisions) == 1
            assert revisions[0].body == "v1"
    finally:
        await client.aclose()


async def test_skill_in_use_cannot_be_archived() -> None:
    """仍被智能体引用的技能不能归档。"""
    client = await _client()
    code = f"skill-{uuid.uuid4().hex[:8]}"
    agent_code = f"agent-{uuid.uuid4().hex[:8]}"
    try:
        async with AdminSessionLocal() as db:
            await skill_repo.create(
                db,
                tenant_id=None,
                code=code,
                name=code,
                description="被引用的技能",
                allowed_tools="",
                body="body",
                status="published",
                enabled=1,
                origin="manual",
                content_hash="h",
            )
            await agent_repo.create(
                db,
                code=agent_code,
                tenant_id=None,
                name="Bound",
                description="",
                system_prompt="hi",
                model="m",
                llm_endpoint_code="",
                base_url="",
                api_key_cipher="",
                tools=[],
                skills=[code],
                enabled=1,
            )
            await db.commit()

        archived = await client.post(
            f"/admin/api/skills/{code}/archive", headers=_headers(client)
        )
        assert archived.status_code == 409
        assert archived.json()["code"] == "in_use"
    finally:
        async with AdminSessionLocal() as db:
            await agent_repo.delete(db, agent_code)
            await skill_repo.delete(db, code)
            await db.commit()
        await client.aclose()


async def test_skill_write_requires_csrf_header() -> None:
    """写操作缺 CSRF 头一律 403。"""
    client = await _client()
    try:
        response = await client.post(
            "/admin/api/skills",
            json={
                "code": f"skill-{uuid.uuid4().hex[:8]}",
                "description": "无令牌",
                "body": "x",
            },
        )
    finally:
        await client.aclose()

    assert response.status_code == 403
    assert response.json()["code"] == "invalid_csrf"


async def test_skill_list_requires_authentication() -> None:
    """未登录返回 401 JSON。"""
    from services.admin_web.app import app as admin_asgi_app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_asgi_app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        response = await client.get("/admin/api/skills")

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"
