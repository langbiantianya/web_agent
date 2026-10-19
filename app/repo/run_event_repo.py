"""RunEvent 数据访问 —— 断线续传的回放源（设计文档 §4.2 / §5.4）。"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import utcnow

if TYPE_CHECKING:  # 仅供类型检查，运行时用局部导入，避免循环引用
    from app.core.run_hub import RunEventDTO

from app.models.run_event import RunEvent

logger = logging.getLogger(__name__)

REPLAY_LIMIT = 5000


class RunEventRepo:
    async def list_after(
        self,
        db: AsyncSession,
        run_id: int,
        after_seq: int,
        limit: int = REPLAY_LIMIT,
    ) -> list["RunEventDTO"]:
        """回放 ``seq > after_seq`` 的事件，按 seq 升序。"""
        # 局部导入：core.run_hub 会经由 core/__init__ 回到本模块，顶层 import
        # 会在「先 import app.repo」时形成循环引用。
        from app.core.run_hub import RunEventDTO

        stmt = (
            sa.select(RunEvent)
            .where(RunEvent.run_id == run_id, RunEvent.seq > after_seq)
            .order_by(RunEvent.seq.asc())
            .limit(limit)
        )
        rows = await db.scalars(stmt)
        return [
            RunEventDTO(
                run_id=r.run_id, seq=r.seq, event_type=r.event_type, payload=r.payload
            )
            for r in rows
        ]

    async def first_seq(self, db: AsyncSession, run_id: int) -> int | None:
        """现存事件的最小 seq，用于判断回放是否存在缺口。"""
        stmt = (
            sa.select(sa.func.min(RunEvent.seq))
            .where(RunEvent.run_id == run_id)
        )
        return await db.scalar(stmt)

    async def max_seq(self, db: AsyncSession, run_id: int) -> int:
        """已落库的最大 seq；没有事件时返回 0（而非 None），调用方可直接做算术。

        用于新事件的 seq 分配与续传游标兜底。
        """
        stmt = sa.select(sa.func.max(RunEvent.seq)).where(RunEvent.run_id == run_id)
        return await db.scalar(stmt) or 0

    async def count_after(self, db: AsyncSession, run_id: int, after_seq: int) -> int:
        """统计 ``seq > after_seq`` 的事件条数，用于判断断线重连是否有可回放的事件。"""
        stmt = (
            sa.select(sa.func.count())
            .select_from(RunEvent)
            .where(RunEvent.run_id == run_id, RunEvent.seq > after_seq)
        )
        return await db.scalar(stmt) or 0

    async def has_type(
        self, db: AsyncSession, run_id: int, event_type: str
    ) -> bool:
        """判断该 run 是否已有指定类型事件（如已落过 done/error），用来快速收敛终态。"""
        stmt = (
            sa.select(sa.func.count())
            .select_from(RunEvent)
            .where(RunEvent.run_id == run_id, RunEvent.event_type == event_type)
        )
        return bool(await db.scalar(stmt))

    async def purge_older_than(self, db: AsyncSession, days: int) -> int:
        """归档清理：删除超过保留期的 run_event（最终内容以 message 为准）。

        cutoff 必须用 UTC：``run_event.created_at`` 是 ``server_default=sa.func.now()``
        写的（见 :func:`app.config.db._pin_session_timezone`，MySQL 会话时区已钉成
        UTC）。若这里用 ``datetime.now()`` 取本地时间，在 UTC+8 的机器上 cutoff 会
        比库里实际存的值**早 8 小时**，等于把保留期悄悄缩短了 1/3。
        """
        cutoff = utcnow() - timedelta(days=days)
        result = await db.execute(
            sa.delete(RunEvent).where(RunEvent.created_at < cutoff)
        )
        await db.commit()
        deleted = result.rowcount or 0
        if deleted:
            logger.info("已清理 %s 条过期 run_event", deleted)
        return deleted


run_event_repo = RunEventRepo()
