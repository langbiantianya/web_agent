"""画布 island 的协议端点（V4.10 阶段 E）。

Ludic 视图层下线后，画布是 Python 侧**唯一**还渲染 DOM 的东西，它靠三个
端点活着：

- ``GET /admin/workflows/{code}/canvas``——片段页；
- ``POST /admin/workflows/validate``——实时校验；
- ``POST /admin/workflows/{code}/update``——保存。

图定义的**唯一写路径**就是第三个端点，所以它的行为必须被钉住：坐标、
label、inputTemplate 原样落库；校验不过时留在画布上（返回 JSON 错误），
而不是把管理员还没保存的图丢掉。
"""

from __future__ import annotations

import json
import uuid

import pytest
import sqlalchemy as sa

from app.models.agent import Agent
from app.models.workflow import Workflow
from services.admin_web.db import AdminSessionLocal


async def _client():
    """建立带平台超级管理员会话的 ASGI 客户端。"""
    from tests.test_admin_web import _create_seeded_logged_in_super_admin

    return await _create_seeded_logged_in_super_admin()


async def _seed_workflow() -> tuple[str, str]:
    """建一个 Agent 与一个工作流，返回 (工作流 code, Agent code)。"""
    tag = uuid.uuid4().hex[:8]
    code = f"wf-{tag}"
    agent_code = f"planner-{tag}"
    async with AdminSessionLocal() as db:
        db.add(
            Agent(
                tenant_id="default",
                code=agent_code,
                name="Planner",
                model="test-model",
                base_url="https://llm.example.com/v1",
                api_key_cipher="",
                system_prompt="p",
                tools=[],
                skills=[],
                enabled=1,
            )
        )
        db.add(
            Workflow(
                code=code,
                name="Layout Flow",
                description="",
                definition={
                    "entry": "step1",
                    "nodes": [
                        {
                            "id": "step1",
                            "type": "agent",
                            "label": "意图识别",
                            "agentCode": agent_code,
                        }
                    ],
                    "edges": [],
                },
                enabled=1,
            )
        )
        await db.commit()
    return code, agent_code


@pytest.mark.asyncio
async def test_canvas_update_persists_layout_and_label() -> None:
    """保存后 layout 坐标与节点 label 必须原样落库。"""
    client, _, _, csrf = await _client()
    code, agent_code = await _seed_workflow()
    definition = {
        "entry": "step1",
        "inputTemplate": "问：{{user_input}}",
        "nodes": [
            {
                "id": "step1",
                "type": "agent",
                "label": "意图识别",
                "agentCode": agent_code,
            }
        ],
        "edges": [],
        "layout": {
            "nodes": {"step1": {"x": 320, "y": 640}},
            "viewport": {"x": 12, "y": 34, "scale": 1.5},
        },
    }
    try:
        updated = await client.post(
            f"/admin/workflows/{code}/update",
            data={
                "csrf_token": csrf,
                "name": "Layout Flow",
                "description": "",
                "enabled": "1",
                "definition": json.dumps(definition, ensure_ascii=False),
                "next": f"/admin/workflows/{code}/canvas",
            },
        )
        assert updated.status_code == 303, updated.text
        # 保存成功要回到画布并带上可见反馈。
        assert (
            updated.headers["location"]
            == f"/admin/workflows/{code}/canvas?notice=updated"
        )
        async with AdminSessionLocal() as db:
            row = await db.scalar(
                sa.select(Workflow).where(Workflow.code == code)
            )
            assert row is not None
            assert row.definition["layout"]["nodes"]["step1"] == {
                "x": 320,
                "y": 640,
            }
            assert row.definition["layout"]["viewport"]["scale"] == 1.5
            assert row.definition["nodes"][0]["label"] == "意图识别"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_canvas_update_reports_errors_to_json_clients() -> None:
    """画布是 fetch 提交的：校验不过要回 JSON，留在画布上。"""
    client, _, _, csrf = await _client()
    code, agent_code = await _seed_workflow()
    try:
        response = await client.post(
            f"/admin/workflows/{code}/update",
            data={
                "csrf_token": csrf,
                "name": "Layout Flow",
                "description": "",
                "enabled": "1",
                "definition": json.dumps(
                    {
                        "entry": "step1",
                        "nodes": [
                            {
                                "id": "step1",
                                "type": "agent",
                                "agentCode": "does-not-exist",
                            }
                        ],
                        "edges": [],
                    },
                    ensure_ascii=False,
                ),
            },
            headers={"Accept": "application/json"},
        )
        assert response.status_code == 400, response.text
        payload = response.json()
        assert payload["ok"] is False
        assert payload["errors"], payload
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_canvas_validate_reports_definition_errors() -> None:
    """实时校验：图不合法时给出错误，合法时给出 ok。"""
    client, _, _, csrf = await _client()
    code, agent_code = await _seed_workflow()
    try:
        # 边指向不存在的节点 → 图不合法
        bad = await client.post(
            "/admin/workflows/validate",
            json={
                "csrf_token": csrf,
                "definition": {
                    "entry": "step1",
                    "nodes": [
                        {
                            "id": "step1",
                            "type": "agent",
                            "agentCode": agent_code,
                        }
                    ],
                    "edges": [
                        {"from": "step1", "to": "ghost"}
                    ],
                },
            },
        )
        assert bad.status_code == 200, bad.text
        assert bad.json()["ok"] is False
        assert bad.json()["errors"]

        good = await client.post(
            "/admin/workflows/validate",
            json={
                "csrf_token": csrf,
                "definition": {
                    "entry": "step1",
                    "nodes": [
                        {
                            "id": "step1",
                            "type": "agent",
                            "agentCode": agent_code,
                        }
                    ],
                    "edges": [],
                },
            },
        )
        assert good.status_code == 200, good.text
        assert good.json()["ok"] is True
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_canvas_fragment_missing_workflow_returns_404() -> None:
    """不存在的编排返回 404，不泄露存在性差异。"""
    client, _, _, _ = await _client()
    try:
        response = await client.get("/admin/workflows/does-not-exist/canvas")
        assert response.status_code == 404
    finally:
        await client.aclose()


def test_unknown_pydantic_error_type_falls_back_to_chinese() -> None:
    """未覆盖的 pydantic 错误类型也要给中文，不能把英文原文透出去。"""
    from services.admin_web.workflow_validation import _pydantic_message

    assert (
        _pydantic_message("some_unknown_type", "Input should be ...", "a.x")
        == "字段 a.x 不合法"
    )
    assert (
        _pydantic_message("some_unknown_type", "Input should be ...", None)
        == "定义不合法"
    )
