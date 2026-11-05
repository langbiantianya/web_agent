"""审批会签、重复投票与超时收口。"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest

from app.config.db import SessionLocal
from app.core.workflow_def import WorkflowDefinition
from app.models.base import utcnow
from app.repo.run_approval_repo import run_approval_repo


def _run_id() -> int:
    """生成不会与其它用例冲突的 run id。"""
    return 2_000_000_000 + uuid4().int % 1_000_000_000


@pytest.mark.asyncio
async def test_quorum_requires_distinct_approvers(db):
    """达到指定票数才通过，同一审批人重复投票无效。"""
    approval = await run_approval_repo.create(
        db,
        _run_id(),
        "gate",
        "请会签",
        required_approvals=2,
    )
    approval_id = approval.id
    first = await run_approval_repo.decide(
        db, approval_id, True, "同意", "alice"
    )
    assert first is not None and first.status == "pending"
    assert first.approved_count == 1

    duplicate = await run_approval_repo.decide(
        db, approval_id, True, "重复", "alice"
    )
    assert duplicate is None

    final = await run_approval_repo.decide(
        db, approval_id, True, "同意", "bob"
    )
    assert final is not None and final.status == "approved"
    assert final.approved_count == 2
    assert len(await run_approval_repo.list_votes(db, approval_id)) == 2


@pytest.mark.asyncio
async def test_any_rejection_fails_closed(db):
    """会签中任一反对票立即驳回。"""
    approval = await run_approval_repo.create(
        db,
        _run_id(),
        "gate",
        "请会签",
        required_approvals=3,
    )
    await run_approval_repo.decide(db, approval.id, True, "同意", "alice")
    rejected = await run_approval_repo.decide(
        db, approval.id, False, "拒绝", "bob"
    )
    assert rejected is not None
    assert rejected.status == "rejected"
    assert rejected.rejected_count == 1


@pytest.mark.asyncio
async def test_distinct_concurrent_votes_are_not_lost(db):
    """不同审批人并发投票时，计数必须通过数据库原子自增累加。"""
    approval = await run_approval_repo.create(
        db,
        _run_id(),
        "gate",
        "并发会签",
        required_approvals=2,
    )

    async def _vote(voter: str):
        """在独立事务里投一票，模拟两个 HTTP 请求同时到达。"""
        async with SessionLocal() as session:
            await asyncio.sleep(0.02)
            return await run_approval_repo.decide(
                session, approval.id, True, "同意", voter
            )

    first, second = await asyncio.gather(_vote("alice"), _vote("bob"))
    assert first is not None
    assert second is not None

    async with SessionLocal() as session:
        final = await run_approval_repo.get_by_id(session, approval.id)
        votes = await run_approval_repo.list_votes(session, approval.id)
    assert final is not None
    assert final.status == "approved"
    assert final.approved_count == 2
    assert len(votes) == 2


@pytest.mark.asyncio
async def test_timeout_is_selectable_and_fail_closed_by_default(db):
    """到期审批进入扫描结果，并可按记录的策略原子收口。"""
    approval = await run_approval_repo.create(
        db,
        _run_id(),
        "gate",
        "超时测试",
        required_approvals=2,
        timeout_action="reject",
        deadline_at=utcnow() - timedelta(seconds=1),
    )
    expired = await run_approval_repo.list_expired(db, utcnow())
    assert approval.id in {row.id for row in expired}
    row = await run_approval_repo.finalize_timeout(
        db, approval.id, approved=False
    )
    assert row is not None
    assert row.status == "rejected"
    assert row.decided_by == "system:timeout"
    assert (
        await run_approval_repo.finalize_timeout(
            db, approval.id, approved=True
        )
        is None
    )


def test_workflow_validates_quorum_and_timeout_fields():
    """审批节点可配置会签与超时，agent 节点不能误配。"""
    definition = WorkflowDefinition.model_validate(
        {
            "entry": "gate",
            "nodes": [
                {
                    "id": "gate",
                    "type": "approval",
                    "prompt": "批准？",
                    "requiredApprovals": 2,
                    "approvalTimeoutSec": 60,
                    "approvalTimeoutAction": "approve",
                }
            ],
        }
    )
    assert definition.nodes[0].requiredApprovals == 2

    with pytest.raises(ValueError):
        WorkflowDefinition.model_validate(
            {
                "entry": "gate",
                "nodes": [
                    {
                        "id": "gate",
                        "type": "approval",
                        "prompt": "批准？",
                        "approvalTimeoutAction": "ignore",
                    }
                ],
            }
        )
