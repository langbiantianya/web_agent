"""客户端管理 API 已切换到后台 Session、Casbin 与超级管理员。"""

from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import authenticate_admin_sync


def test_legacy_admin_key_is_gone_and_session_is_required():
    """旧 X-Admin-Key 不再能访问管理 API。"""
    client = TestClient(create_app())
    assert client.get(
        "/api/agents", headers={"X-Admin-Key": "test-admin-key"}
    ).status_code == 401
    assert client.get("/api/admin-users").status_code == 410


def test_platform_super_admin_can_manage_agents_and_workflows():
    """平台超级管理员绕过 Casbin，完成最小 CRUD。"""
    client = TestClient(create_app())
    authenticate_admin_sync(client, tenant_id="tenant-admin-api")
    tag = uuid4().hex[:8]
    agent_code = f"admin-agent-{tag}"

    created_agent = client.post(
        "/api/agents",
        json={
            "code": agent_code,
            "name": "Admin Agent",
            "system_prompt": "你是管理员创建的 Agent",
            "model": "fake-model",
        },
    )
    assert created_agent.status_code == 201, created_agent.text

    created_workflow = client.post(
        "/api/workflows",
        json={
            "code": f"admin-wf-{tag}",
            "name": "Admin Workflow",
            "definition": {
                "entry": "start",
                "nodes": [
                    {
                        "id": "start",
                        "type": "agent",
                        "agentCode": agent_code,
                    }
                ],
                "edges": [],
            },
        },
    )
    assert created_workflow.status_code == 201, created_workflow.text

    assert client.delete(f"/api/workflows/admin-wf-{tag}").status_code == 204
    assert client.delete(f"/api/agents/{agent_code}").status_code == 204


def test_viewer_can_read_but_cannot_write_management_resources():
    """viewer 只投影读权限，写请求由 Casbin 拒绝。"""
    client = TestClient(create_app())
    authenticate_admin_sync(
        client,
        platform_super=False,
        roles=("viewer",),
    )
    assert client.get("/api/agents").status_code == 200
    denied = client.post(
        "/api/agents",
        json={
            "code": f"viewer-{uuid4().hex[:8]}",
            "name": "Denied",
            "system_prompt": "denied",
            "model": "fake-model",
        },
    )
    assert denied.status_code == 403
    assert denied.json()["code"] == "forbidden"


def test_oauth_admin_cannot_spoof_another_tenant():
    """OAuth 管理员提交其他租户 Header 时直接拒绝。"""
    client = TestClient(create_app())
    authenticate_admin_sync(
        client,
        tenant_id="tenant-a",
        platform_super=False,
        roles=("tenant_admin",),
    )
    client.headers["X-Tenant-ID"] = "tenant-b"
    response = client.get("/api/agents")
    assert response.status_code == 403


def test_agent_records_are_isolated_by_effective_tenant():
    """平台超级管理员切换租户后，不能读取或删除另一租户的 Agent。"""
    client = TestClient(create_app())
    authenticate_admin_sync(client, tenant_id="tenant-a")
    code = f"tenant-agent-{uuid4().hex[:8]}"
    client.headers["X-Tenant-ID"] = "tenant-a"
    created = client.post(
        "/api/agents",
        json={
            "code": code,
            "name": "Tenant A Agent",
            "system_prompt": "tenant-a",
            "model": "fake-model",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["code"] == code

    client.headers["X-Tenant-ID"] = "tenant-b"
    assert client.get(f"/api/agents/{code}").status_code == 404
    assert code not in {item["code"] for item in client.get("/api/agents").json()}
    assert client.delete(f"/api/agents/{code}").status_code == 404

    client.headers["X-Tenant-ID"] = "tenant-a"
    assert client.delete(f"/api/agents/{code}").status_code == 204
