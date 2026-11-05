"""审批超时巡检。

审批超时默认 fail-closed 驳回；工作流节点可以显式选择超时通过。扫描通过数据库
条件更新收口，多实例同时巡检时只有一个实例能取得最终决策并恢复图。
"""

from __future__ import annotations

import asyncio
import logging

from app.config.db import SessionLocal
from app.config.settings import settings
from app.core.run_hub import get_hub
from app.models.base import utcnow
from app.models.enums import EventType
from app.repo.run_approval_repo import run_approval_repo

logger = logging.getLogger(__name__)
_task: asyncio.Task | None = None


async def expire_due_approvals(limit: int = 100) -> int:
    """收口一批到期审批并触发对应 run 恢复。"""
    from app.core.orchestrator import get_orchestrator

    finalized = 0
    async with SessionLocal() as db:
        rows = await run_approval_repo.list_expired(
            db, utcnow(), limit=max(1, limit)
        )
    for candidate in rows:
        async with SessionLocal() as db:
            approved = candidate.timeout_action == "approve"
            row = await run_approval_repo.finalize_timeout(
                db, candidate.id, approved
            )
        if row is None:
            continue
        finalized += 1
        await get_hub().emit(
            row.run_id,
            EventType.APPROVAL_TIMEOUT,
            {
                "node": row.node_id,
                "approved": approved,
                "action": row.timeout_action,
                "comment": row.comment,
            },
        )
        get_orchestrator().spawn_resume(
            row.run_id,
            {
                "approved": approved,
                "comment": row.comment or "审批超时",
                "decidedBy": "system:timeout",
            },
        )
    if finalized:
        logger.warning("审批超时巡检收口了 %s 个请求", finalized)
    return finalized


async def _loop(interval: float) -> None:
    """周期执行超时巡检，单轮失败不终止后台任务。"""
    while True:
        await asyncio.sleep(interval)
        try:
            await expire_due_approvals()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("审批超时巡检失败（下一周期继续）")


def start_approval_timeout_monitor() -> None:
    """启动审批超时巡检。"""
    global _task
    if _task is not None and not _task.done():
        return
    interval = max(1.0, settings.approval_timeout_scan_interval)
    _task = asyncio.create_task(_loop(interval), name="approval-timeout-monitor")


async def stop_approval_timeout_monitor() -> None:
    """停止并等待巡检任务退出。"""
    global _task
    task, _task = _task, None
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
