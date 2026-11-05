"""数据库管理员、角色门禁与租户隔离。"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from app.main import create_app

BOOTSTRAP = {"X-Admin-Key": "test-admin-key"}


@pytest.fixture
async def client():
    """启动完整应用生命周期并提供内存 HTTP 客户端。"""
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=30
        ) as value:
            yield value


async def _create_user(client: httpx.AsyncClient, **overrides) -> dict:
    """用引导管理员创建一个数据库用户。"""
    suffix = uuid4().hex[:8]
    payload = {
        "username": f"user_{suffix}",
        "display_name": "测试用户",
        "tenant_id": f"tenant-{suffix}",
        "role": "operator",
        "enabled": True,
        **overrides,
    }
    response = await client.post(
        "/api/admin-users", json=payload, headers=BOOTSTRAP
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
async def test_database_admin_key_roles_and_tenant_skill(client):
    """数据库 Key 可认证，operator 只能写自己租户，viewer 只能读。"""
    operator = await _create_user(client)
    viewer = await _create_user(
        client, role="viewer", tenant_id=operator["tenant_id"]
    )
    operator_headers = {"X-Admin-Key": operator["api_key"]}
    viewer_headers = {"X-Admin-Key": viewer["api_key"]}
    code = f"tenant-skill-{uuid4().hex[:8]}"

    created = await client.post(
        "/api/skills",
        json={"code": code, "description": "租户技能", "body": "正文"},
        headers=operator_headers,
    )
    assert created.status_code == 201, created.text
    assert created.json()["tenant_id"] == operator["tenant_id"]

    own = await client.get(f"/api/skills/{code}", headers=operator_headers)
    assert own.status_code == 200
    assert own.json()["code"] == code

    default_view = await client.get(f"/api/skills/{code}")
    assert default_view.status_code == 404

    forbidden_write = await client.post(
        "/api/skills",
        json={"code": f"viewer-{uuid4().hex[:8]}", "description": "x"},
        headers=viewer_headers,
    )
    assert forbidden_write.status_code == 403
    assert (
        await client.get("/api/tool-audits", headers=viewer_headers)
    ).status_code == 200
    assert (
        await client.get("/api/admin-users", headers=operator_headers)
    ).status_code == 403


@pytest.mark.asyncio
async def test_key_rotation_invalidates_old_key(client):
    """轮换后旧 Key 立即失效，新 Key 可继续认证。"""
    user = await _create_user(client, role="admin")
    rotated = await client.post(
        f"/api/admin-users/{user['username']}/rotate-key",
        headers=BOOTSTRAP,
    )
    assert rotated.status_code == 200, rotated.text
    new_key = rotated.json()["api_key"]
    assert new_key != user["api_key"]

    old_headers = {"X-Admin-Key": user["api_key"]}
    assert (
        await client.get("/api/admin-users", headers=old_headers)
    ).status_code == 401
    assert (
        await client.get(
            "/api/admin-users", headers={"X-Admin-Key": new_key}
        )
    ).status_code == 200


@pytest.mark.asyncio
async def test_admin_cannot_demote_or_disable_self(client):
    """数据库 admin 不能把自己降级或停用，避免把管理面锁死。"""
    user = await _create_user(client, role="admin")
    headers = {"X-Admin-Key": user["api_key"]}
    demote = await client.put(
        f"/api/admin-users/{user['username']}",
        json={"role": "viewer"},
        headers=headers,
    )
    assert demote.status_code == 409, demote.text
    disable = await client.put(
        f"/api/admin-users/{user['username']}",
        json={"enabled": False},
        headers=headers,
    )
    assert disable.status_code == 409, disable.text
