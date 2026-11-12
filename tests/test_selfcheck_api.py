"""运行时链路自检接口（``GET /api/selfcheck``）。

这份用例盯住的是**判定逻辑**：哪一段配置算「通过」、哪一段算「失败」。
判定错了比没有判定更糟——它会把坏配置说成好的。
"""

from __future__ import annotations

import httpx
import pytest

from app.config.db import SessionLocal
from app.core.crypto import encrypt
from app.main import create_app
from app.repo.agent_repo import agent_repo
from app.repo.llm_endpoint_repo import llm_endpoint_repo
from app.repo.mcp_repo import mcp_server_repo, mcp_tool_repo
from app.repo.skill_repo import skill_repo
from app.repo.workflow_repo import workflow_repo
from tests.conftest import authenticate_client


@pytest.fixture
async def client():
    """真实 app + HTTP 客户端（带终端用户会话）。"""
    async with httpx.AsyncClient(
        base_url="http://testserver", transport=httpx.ASGITransport(app=create_app())
    ) as c:
        await authenticate_client(c)
        yield c


def _stages(report: dict, workflow_code: str) -> dict[str, dict]:
    """从报告里取出某个工作流的阶段表。

    Args:
        report: 自检报告。
        workflow_code: 工作流 code。

    Returns:
        ``{阶段 key: 阶段}``。
    """
    for chain in report["chains"]:
        if chain["workflow_code"] == workflow_code:
            return {s["key"]: s for s in chain["stages"]}
    return {}


async def _seed_full_chain() -> None:
    """布一套「全都对」的链路：端点 + 技能 + MCP 工具 + 智能体 + 工作流。

    测试库在模块之间不清理，所以这里先删掉上一轮留下的同名数据——否则第二
    条用例就会撞上 UNIQUE 约束，而不是测到想测的东西。
    """
    import sqlalchemy as sa

    async with SessionLocal() as db:
        await db.execute(sa.text("delete from mcp_tool"))
        await db.execute(sa.text("delete from mcp_server"))
        await db.execute(sa.text("delete from agent"))
        await db.execute(sa.text("delete from skill"))
        await db.execute(sa.text("delete from workflow"))
        await db.execute(sa.text("delete from llm_endpoint"))
        await db.commit()
        await llm_endpoint_repo.create(
            db,
            code="sc-llm",
            name="自检端点",
            base_url="https://example.test/v1",
            model="test-model",
            api_key_cipher=encrypt("secret"),
            priority=100,
            enabled=1,
        )
        await skill_repo.create(
            db,
            code="sc-skill",
            name="sc-skill",
            body="技能正文",
            origin="manual",
            content_hash="h",
            enabled=1,
            status="published",
        )
        server = await mcp_server_repo.create(
            db,
            code="scmcp",
            tenant_id="default",
            name="自检 MCP",
            transport="stdio",
            command="/usr/local/bin/python",
            args=[],
            enabled=1,
        )
        await mcp_tool_repo.replace_for_server(
            db,
            server.id,
            [
                {
                    "qualified_name": "mcp__scmcp__add",
                    "tool_name": "add",
                    "description": "加法",
                    "input_schema": {},
                    "output_schema": None,
                    "annotations": None,
                }
            ],
        )
        await agent_repo.create(
            db,
            code="sc-agent",
            name="自检智能体",
            system_prompt="你是自检助手",
            llm_endpoint_code="sc-llm",
            model="test-model",
            base_url="",
            api_key_cipher="",
            temperature=0.2,
            max_tokens=512,
            tools=["mcp__scmcp__add"],
            skills=["sc-skill"],
            enabled=1,
        )
        await workflow_repo.create(
            db,
            code="sc-wf",
            name="自检工作流",
            description="",
            definition={
                "entry": "n1",
                "nodes": [
                    {
                        "id": "n1",
                        "type": "agent",
                        "agentCode": "sc-agent",
                        "outputKey": "out",
                    }
                ],
                "edges": [],
                "inputTemplate": "{{user_input}}",
            },
            enabled=1,
        )


async def test_requires_login():
    """未登录时不能看链路配置。"""
    async with httpx.AsyncClient(
        base_url="http://testserver", transport=httpx.ASGITransport(app=create_app())
    ) as c:
        resp = await c.get("/api/selfcheck")
    assert resp.status_code == 401


async def test_empty_report_has_no_chains():
    """库里什么都没有时，报告是空链路而不是 500。"""
    async with httpx.AsyncClient(
        base_url="http://testserver", transport=httpx.ASGITransport(app=create_app())
    ) as c:
        await authenticate_client(c)
        resp = await c.get("/api/selfcheck")
    assert resp.status_code == 200
    body = resp.json()
    assert body["chains"] == []
    assert body["summary"]["workflows"] == 0
    # 没有链路就不算「就绪」——空报告说就绪是误导。
    assert body["summary"]["ready"] is False


async def test_full_chain_all_pass(client):
    """配置齐全时每一段都通过。"""
    await _seed_full_chain()
    resp = await client.get("/api/selfcheck")
    assert resp.status_code == 200
    body = resp.json()
    stages = _stages(body, "sc-wf")
    assert stages["graph"]["status"] == "pass"
    assert stages["node:n1:llm"]["status"] == "pass"
    assert stages["node:n1:skill"]["status"] == "pass"
    assert stages["node:n1:mcp"]["status"] == "pass"
    assert body["summary"]["ready"] is True
    assert body["summary"]["failed_stages"] == 0


async def test_endpoint_host_only(client):
    """端点地址只回显主机名，不回显路径与凭据。"""
    await _seed_full_chain()
    body = (await client.get("/api/selfcheck")).json()
    llm = body["llm_endpoints"][0]
    assert llm["host"] == "example.test"
    assert "/v1" not in llm["host"]
    assert llm["key_configured"] is True


async def test_disabled_endpoint_fails_stage(client):
    """端点停用时该节点判失败。"""
    await _seed_full_chain()
    async with SessionLocal() as db:
        await llm_endpoint_repo.update(db, "sc-llm", {"enabled": 0})
    body = (await client.get("/api/selfcheck")).json()
    stages = _stages(body, "sc-wf")
    assert stages["node:n1:llm"]["status"] == "fail"
    assert body["summary"]["ready"] is False


async def test_unpublished_skill_warns(client):
    """技能没发布时是「提醒」而不是「失败」——它不一定影响这次运行。"""
    await _seed_full_chain()
    async with SessionLocal() as db:
        row = await skill_repo.get_by_code(db, "sc-skill", tenant_id="default")
        row.status = "draft"
        await db.commit()
    body = (await client.get("/api/selfcheck")).json()
    stages = _stages(body, "sc-wf")
    assert stages["node:n1:skill"]["status"] == "warn"


async def test_unknown_mcp_tool_fails(client):
    """绑了目录里没有的 MCP 工具时判失败。"""
    await _seed_full_chain()
    async with SessionLocal() as db:
        row = await agent_repo.get_by_code(db, "sc-agent")
        row.tools = ["mcp__scmcp__ghost"]
        await db.commit()
    body = (await client.get("/api/selfcheck")).json()
    stages = _stages(body, "sc-wf")
    assert stages["node:n1:mcp"]["status"] == "fail"


async def test_missing_agent_fails_stage(client):
    """工作流引用了不存在的智能体时判失败。"""
    await _seed_full_chain()
    async with SessionLocal() as db:
        row = await workflow_repo.get_by_code(db, "sc-wf")
        row.definition = {
            "entry": "n1",
            "nodes": [
                {"id": "n1", "type": "agent", "agentCode": "ghost", "outputKey": "o"}
            ],
            "edges": [],
            "inputTemplate": "{{user_input}}",
        }
        await db.commit()
    body = (await client.get("/api/selfcheck")).json()
    stages = _stages(body, "sc-wf")
    assert stages["node:n1"]["status"] == "fail"

