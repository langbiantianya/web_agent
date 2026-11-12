"""审批事件与 run 状态落库之间的竞态。

编排器暂停在审批节点时的顺序是「建审批记录 → emit ``approval_required``
→ 把 run 置为 ``waiting``」。中间只有几毫秒，而客户端收到事件就可能立刻
点批准——那一刻 run 的状态往往还是 ``running``。

**旧行为**：直接判 409，于是「手快点批准」变成一次失败（实测能稳定复现）。
**新行为**：有��审批记录就等编排器把状态落上（status 与 last_seq 是同一条
UPDATE 写的，所以状态一到，seq 锚点也就准了），然后正常决策。

这份用例钉三件事：

1. 状态还没落上时提交决策 → **成功**（不是 409）；
2. 压根没有待审批记录 → 仍然 409（不能把「没审批」也放过去）；
3. 等待期间编排器写进来的 ``last_seq`` 确实被读到（seq 锚点没退回旧值——
   退回去会与 ``run_event`` 的 ``uk_run_seq`` 撞车）。
"""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest

from app.config.db import SessionLocal
from app.main import create_app
from app.models.enums import RunStatus
from app.repo.conversation_repo import conversation_repo
from app.repo.run_approval_repo import run_approval_repo
from app.repo.run_repo import run_repo
from app.repo.workflow_repo import workflow_repo
from tests.conftest import authenticate_client

WORKFLOW = {
    "entry": "collect",
    "nodes": [
        {"id": "collect", "type": "agent", "agentCode": "a1", "outputKey": "out"},
        {"id": "gate", "type": "approval", "prompt": "确认？"},
    ],
    "edges": [{"from": "collect", "to": "gate"}],
    "inputTemplate": "{{user_input}}",
}


@pytest.fixture
async def client():
    """带终端用户会话的 HTTP 客户端。"""
    async with httpx.AsyncClient(
        base_url="http://testserver",
        transport=httpx.ASGITransport(app=create_app()),
    ) as c:
        await authenticate_client(c)
        yield c


async def _seed_run(status: str, *, with_approval: bool = True):
    """建一条 run（可选地带上待审批记录）。

    Args:
        status: run 的初始状态。
        with_approval: 是否开一条待审批记录。

    Returns:
        ``(run_id, user_id)``。
    """
    from app.models.user import AppUser

    async with SessionLocal() as db:
        user = await db.scalar(
            sa_select_active_user()
        )
        assert user is not None
        wf = await workflow_repo.get_by_code(db, "race-wf")
        if wf is None:
            wf = await workflow_repo.create(
                db,
                code="race-wf",
                name="竞态用例工作流",
                description="",
                definition=WORKFLOW,
                enabled=1,
            )
        conv = await conversation_repo.create(
            db,
            session_id=f"race-conv-{uuid.uuid4().hex}",
            workflow_id=wf.id,
            tenant_id="default",
            owner_user_id=user.id,
            title="竞态用例",
        )
        run = await run_repo.create(
            db,
            conversation_id=conv.id,
            workflow_id=wf.id,
            status=status,
            last_seq=7,
        )
        await db.commit()
        if with_approval:
            await run_approval_repo.create(db, run.id, "gate", "确认？")
            await db.commit()
        return int(run.id), user.id


def sa_select_active_user():
    """取一个 active 用户（测试会话里已经有）。"""
    import sqlalchemy as sa

    from app.models.user import AppUser

    return sa.select(AppUser).where(AppUser.status == "active").limit(1)


async def test_approve_while_status_not_yet_flipped(client):
    """状态还没落上时提交决策要成功——等编排器落状态，而不是判 409。"""
    run_id, _user_id = await _seed_run(RunStatus.RUNNING.value)

    async def flip() -> None:
        """模拟编排器：emit 事件之后才把状态落上。"""
        await asyncio.sleep(0.15)
        async with SessionLocal() as db:
            # status 与 last_seq 同一条 UPDATE：状态到了，seq 锚点也就准了。
            await run_repo.pause(db, run_id, 9, "gate")

    task = asyncio.create_task(flip())
    resp = await client.post(
        f"/api/runs/{run_id}/approval", json={"approved": True, "comment": "通过"}
    )
    await task
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["approved"] is True
    assert body["status"] == "approved"


async def test_seq_anchor_is_not_stale_after_wait(client):
    """等状态落上之后读到的 last_seq 必须是新的（seq 锚点没退回旧值）。"""
    run_id, _user_id = await _seed_run(RunStatus.RUNNING.value)

    async def flip() -> None:
        await asyncio.sleep(0.1)
        async with SessionLocal() as db:
            await run_repo.pause(db, run_id, 42, "gate")

    task = asyncio.create_task(flip())
    resp = await client.post(
        f"/api/runs/{run_id}/approval", json={"approved": True, "comment": "通过"}
    )
    await task
    assert resp.status_code == 200, resp.text
    async with SessionLocal() as db:
        run = await run_repo.get_by_id(db, run_id)
        assert run.last_seq == 42


async def test_no_pending_approval_still_conflicts(client):
    """压根没有待审批记录 → 仍然 409（不能把「没审批」也放过去）。"""
    run_id, _user_id = await _seed_run(
        RunStatus.RUNNING.value, with_approval=False
    )
    resp = await client.post(
        f"/api/runs/{run_id}/approval", json={"approved": True, "comment": "通过"}
    )
    assert resp.status_code == 409


async def test_normal_waiting_run_is_unchanged(client):
    """正常路径（状态已经是 waiting）不受影响。"""
    run_id, _user_id = await _seed_run(RunStatus.WAITING.value)
    resp = await client.post(
        f"/api/runs/{run_id}/approval", json={"approved": True, "comment": "通过"}
    )
    assert resp.status_code == 200, resp.text
