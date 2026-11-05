"""run_approval 的仓储层：人工审批的待办与决策（V3.1）。"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import utcnow
from app.models.run_approval import RunApproval, RunApprovalVote


class RunApprovalRepo:
    """审批记录的读写。"""

    async def create(
        self,
        db: AsyncSession,
        run_id: int,
        node_id: str,
        prompt: str,
        *,
        required_approvals: int = 1,
        timeout_action: str = "reject",
        deadline_at: datetime | None = None,
    ) -> RunApproval:
        """开一条待审批记录。

        ``(run_id, node_id)`` 上有唯一约束：一个 run 里同一个审批节点只会遇到
        一次，所以这里天然是幂等的——重复调用会撞唯一索引而不是产生第二条。
        """
        row = RunApproval(
            run_id=run_id,
            node_id=node_id,
            status="pending",
            prompt=prompt,
            required_approvals=max(1, required_approvals),
            approved_count=0,
            rejected_count=0,
            timeout_action=timeout_action,
            deadline_at=deadline_at,
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
        """记录一张审批票并返回审批请求的最新状态。

        同一审批人只能投一票；会签达到 ``required_approvals`` 才通过，任一反对票
        立即驳回。未达到终态时返回 status=pending，调用方不得恢复图执行。

        Returns:
            最新审批记录；审批已结束或同一人重复投票时返回 ``None``。
        """
        row = await self.get_by_id(db, approval_id)
        if row is None or row.status != "pending":
            return None
        voter = (decided_by or "").strip()[:64]
        if not voter:
            voter = "anonymous"
        db.add(
            RunApprovalVote(
                approval_id=approval_id,
                decided_by=voter,
                approved=1 if approved else 0,
                comment=comment,
            )
        )
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            return None

        now = utcnow()
        values: dict[str, object]
        if approved:
            # 计数必须在 SQL 里自增，不能先读后写。两位审批人并发投票时，
            # 两个事务都会读到旧计数 0，后提交者会把先提交者的票覆盖掉，
            # 最终误以为仍未达到会签人数。
            reached_quorum = (
                RunApproval.approved_count + 1
                >= RunApproval.required_approvals
            )
            values = {
                "approved_count": RunApproval.approved_count + 1,
                "status": sa.case(
                    (reached_quorum, "approved"),
                    else_="pending",
                ),
                "comment": comment,
                "decided_by": voter,
                "decided_at": sa.case(
                    (reached_quorum, now),
                    else_=None,
                ),
            }
        else:
            values = {
                "rejected_count": RunApproval.rejected_count + 1,
                "status": "rejected",
                "comment": comment,
                "decided_by": voter,
                "decided_at": now,
            }
        result = await db.execute(
            sa.update(RunApproval)
            .where(
                RunApproval.id == approval_id,
                RunApproval.status == "pending",
            )
            .values(**values)
        )
        if not result.rowcount:
            # 另一个事务已先收口审批。回滚本次新插入的票，避免留下未计入
            # 终态的“幽灵投票”。
            await db.rollback()
            return None
        await db.commit()
        await db.refresh(row)
        return row

    async def list_expired(
        self, db: AsyncSession, now: datetime, *, limit: int = 100
    ) -> list[RunApproval]:
        """列出已过截止时间但仍 pending 的审批。"""
        stmt = (
            sa.select(RunApproval)
            .where(
                RunApproval.status == "pending",
                RunApproval.deadline_at.is_not(None),
                RunApproval.deadline_at <= now,
            )
            .order_by(RunApproval.deadline_at.asc())
            .limit(limit)
        )
        return list(await db.scalars(stmt))

    async def finalize_timeout(
        self, db: AsyncSession, approval_id: int, approved: bool
    ) -> RunApproval | None:
        """按超时策略原子收口审批；已被人工决策时返回 None。"""
        result = await db.execute(
            sa.update(RunApproval)
            .where(
                RunApproval.id == approval_id,
                RunApproval.status == "pending",
            )
            .values(
                status="approved" if approved else "rejected",
                comment="审批超时自动" + ("通过" if approved else "驳回"),
                decided_by="system:timeout",
                decided_at=utcnow(),
            )
        )
        await db.commit()
        if not result.rowcount:
            return None
        return await self.get_by_id(db, approval_id)

    async def list_votes(
        self, db: AsyncSession, approval_id: int
    ) -> list[RunApprovalVote]:
        """按发生顺序列出审批票。"""
        return list(
            await db.scalars(
                sa.select(RunApprovalVote)
                .where(RunApprovalVote.approval_id == approval_id)
                .order_by(RunApprovalVote.id)
            )
        )


run_approval_repo = RunApprovalRepo()
