"""run_approval 的仓储层：人工审批的待办与决策（V3.1）。"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import utcnow
from app.models.run_approval import RunApproval


class RunApprovalRepo:
    """审批记录的读写。"""

    async def create(
        self, db: AsyncSession, run_id: int, node_id: str, prompt: str
    ) -> RunApproval:
        """开一条待审批记录。

        ``(run_id, node_id)`` 上有唯一约束：一个 run 里同一个审批节点只会遇到
        一次，所以这里天然是幂等的——重复调用会撞唯一索引而不是产生第二条。
        """
        row = RunApproval(
            run_id=run_id, node_id=node_id, status="pending", prompt=prompt
        )
        db.add(row)
        await db.commit()
        return row

    async def get_by_id(self, db: AsyncSession, approval_id: int) -> RunApproval | None:
        """按 id 取审批记录。"""
        return await db.get(RunApproval, approval_id)

    async def get_pending(self, db: AsyncSession, run_id: int) -> RunApproval | None:
        """取该 run 当前待决策的审批记录。

        同一个 run 同一时刻只可能有一个待审批项（审批节点是串行到达的），所以取
        最新的一条就够。
        """
        stmt = (
            sa.select(RunApproval)
            .where(RunApproval.run_id == run_id, RunApproval.status == "pending")
            .order_by(RunApproval.id.desc())
        )
        return await db.scalar(stmt.limit(1))

    async def list_by_run(self, db: AsyncSession, run_id: int) -> list[RunApproval]:
        """列出该 run 的全部审批记录（按 id 正序即发生顺序）。"""
        stmt = (
            sa.select(RunApproval)
            .where(RunApproval.run_id == run_id)
            .order_by(RunApproval.id.asc())
        )
        return list(await db.scalars(stmt))

    async def decide(
        self,
        db: AsyncSession,
        approval_id: int,
        approved: bool,
        comment: str | None,
        decided_by: str | None,
    ) -> RunApproval | None:
        """记录决策。

        条件更新 ``status == 'pending'``：并发两次提交时只有一次会成功，第二次
        拿到 0 行受影响——这样「重复点批准」不会产生两条互相矛盾的决策记录。
        """
        await db.execute(
            sa.update(RunApproval)
            .where(RunApproval.id == approval_id, RunApproval.status == "pending")
            .values(
                status="approved" if approved else "rejected",
                comment=comment,
                decided_by=decided_by,
                decided_at=utcnow(),
            )
        )
        await db.commit()
        return await self.get_by_id(db, approval_id)


run_approval_repo = RunApprovalRepo()
