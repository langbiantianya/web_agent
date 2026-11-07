"""Skill 草稿、发布、软删除与二进制资源回滚。"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from app.main import create_app
from tests.conftest import authenticate_admin

ADMIN: dict[str, str] = {}


@pytest.fixture
async def client():
    """启动真实应用生命周期。"""
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=30
        ) as value:
            await authenticate_admin(value)
            yield value


@pytest.mark.asyncio
async def test_draft_publish_and_soft_delete_restore(client):
    """草稿不可注入，发布后启用，删除为归档且可恢复为草稿。"""
    code = f"life-{uuid4().hex[:8]}"
    created = await client.post(
        "/api/skills",
        json={
            "code": code,
            "description": "生命周期",
            "body": "v1",
            "status": "draft",
            "enabled": True,
        },
        headers=ADMIN,
    )
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "draft"
    assert created.json()["enabled"] is False

    published = await client.post(
        f"/api/skills/{code}/publish",
        json={"enabled": True},
        headers=ADMIN,
    )
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "published"
    assert published.json()["enabled"] is True

    deleted = await client.delete(f"/api/skills/{code}", headers=ADMIN)
    assert deleted.status_code == 204, deleted.text
    assert (await client.get(f"/api/skills/{code}")).status_code == 404

    restored = await client.post(f"/api/skills/{code}/restore", headers=ADMIN)
    assert restored.status_code == 200, restored.text
    assert restored.json()["status"] == "draft"
    assert restored.json()["enabled"] is False
    assert restored.json()["deleted_at"] is None


@pytest.mark.asyncio
async def test_binary_resource_rollback_restores_exact_bytes(client):
    """二进制修订保存正文，回滚后能逐字节恢复并进入草稿态。"""
    code = f"binrb-{uuid4().hex[:8]}"
    created = await client.post(
        "/api/skills",
        json={"code": code, "description": "binary", "body": "v1"},
        headers=ADMIN,
    )
    assert created.status_code == 201, created.text

    first = b"\x00first-\xff"
    uploaded = await client.put(
        f"/api/skills/{code}/resources?path=assets/a.bin",
        content=first,
        headers={**ADMIN, "Content-Type": "application/octet-stream"},
    )
    assert uploaded.status_code == 201, uploaded.text
    snapshot = await client.get(f"/api/skills/{code}", headers=ADMIN)
    snapshot_version = snapshot.json()["version"]

    second = b"\x00second-\xfe"
    replaced = await client.put(
        f"/api/skills/{code}/resources?path=assets/a.bin",
        content=second,
        headers={**ADMIN, "Content-Type": "application/octet-stream"},
    )
    assert replaced.status_code == 201, replaced.text

    rolled = await client.post(
        f"/api/skills/{code}/rollback",
        json={"version": snapshot_version},
        headers=ADMIN,
    )
    assert rolled.status_code == 200, rolled.text
    assert rolled.json()["status"] == "draft"

    restored = await client.get(
        f"/api/skills/{code}/resources/assets/a.bin", headers=ADMIN
    )
    assert restored.status_code == 200
    assert restored.content == first

    revisions = await client.get(
        f"/api/skills/{code}/revisions", headers=ADMIN
    )
    assert revisions.status_code == 200
    assert any(item["binary_resource_count"] >= 1 for item in revisions.json())
