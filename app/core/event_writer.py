"""EventWriter：run_event 批量落库（设计文档 §4.5）。

关键约束：**绝不能每个 token 一次 INSERT**，也**绝不能让落库阻塞吐字推送**。

实现要点：
- ``append()`` 是同步方法，只往内存 list 里追加，并唤醒 flush 任务，绝不 await；
- 后台 flush 循环按「累积条数 >= batch_size」或「距上次 flush >= flush_interval」
  两个条件触发批量 INSERT；
- 批量写入失败时把事件放回队首重试一次，避免丢事件（续传依赖事件完整性）。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import insert

from app.config.db import SessionLocal
from app.config.settings import settings
from app.models.run_event import RunEvent as RunEventRow

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PendingEvent:
    run_id: int
    seq: int
    event_type: str
    payload: dict


class EventWriter:
    """单个 run 的事件落库器，生命周期与 run 对齐。"""

    def __init__(
        self,
        run_id: int,
        batch_size: int | None = None,
        flush_interval: float | None = None,
    ) -> None:
        """为单个 run 拉起后台落库任务。

        关键状态：
        - ``_batch``：内存里的待落库事件。吐字路径只往这里 append，绝不 await；
        - ``_wake`` / ``_task``：唤醒信号与后台 flush 循环，二者成对使用；
        - ``_closed``：关停标志，置位后循环会把最后一批冲完再退出；
        - ``_lock``：让 SSE 回放前调的 ``flush()`` 排队等本轮写入提交。

        batch_size / flush_interval 留空时取全局配置，测试可逐 run 覆盖以获得
        确定的落库时序。

        注意：``asyncio.create_task`` 要求有 running loop，因此只能在事件循环
        内构造（不能从模块导入期创建）。
        """
        self.run_id = run_id
        self.batch_size = batch_size or settings.event_batch_size
        self.flush_interval = (
            flush_interval if flush_interval is not None else settings.event_flush_interval
        )
        self._batch: list[PendingEvent] = []
        self._wake = asyncio.Event()
        self._closed = False
        self._lock = asyncio.Lock()
        self._task = asyncio.create_task(self._flush_loop(), name=f"event-writer-{run_id}")

    # ------------------------------------------------------------------ 写

    def append(self, evt: PendingEvent) -> None:
        """同步追加，永不阻塞调用方（吐字路径上调用）。"""
        if self._closed:
            return
        self._batch.append(evt)
        if len(self._batch) >= self.batch_size:
            self._wake.set()

    # -------------------------------------------------------------- 内部

    async def _flush_loop(self) -> None:
        """后台落库循环：被唤醒或超时即冲一批。

        两个触发条件缺一不可——``batch_size`` 兜住高频长 run，避免内存无限涨；
        ``flush_interval`` 兜住低频 run，避免尾部零星几个 token 长时间压在内存
        里，让 SSE 续传读不到最新状态。

        退出条件是「已 close **且** 批次已空」，而不是只判 ``_closed``：关停的
        瞬间就退出，会把还躺在 ``_batch`` 里的尾部事件丢掉，SSE 续传就会漏掉
        最后几个字。

        单个事件写失败由 ``_flush`` 自行处理并重排，这里不退出循环——一次 DB
        抖动不该让整个 run 的事件永久停止落库。
        """
        while True:
            if self._closed and not self._batch:
                return
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.flush_interval)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            if self._closed and not self._batch:
                return
            await self._flush()

    async def _flush(self) -> None:
        """把当前批次写进 DB。

        整段（含 DB 写）都在 ``self._lock`` 内：这样并发的 ``flush()`` 会排队等
        到本轮写完之后才执行，从而一定能拿到「最新」的批次。若只锁住取出批次的
        那一瞬间，``flush()`` 可能在写入尚未提交时看到空批次就直接返回，调用方
        （SSE 回放）就会读到一份还缺尾部事件的 DB。
        """
        async with self._lock:
            if not self._batch:
                return
            batch, self._batch = self._batch, []
            rows = [
                {
                    "run_id": e.run_id,
                    "seq": e.seq,
                    "event_type": e.event_type,
                    "payload": e.payload,
                }
                for e in batch
            ]
            try:
                async with SessionLocal() as db:
                    await db.execute(insert(RunEventRow), rows)
                    await db.commit()
            except Exception:  # pragma: no cover - DB 抖动
                logger.exception(
                    "run_event 批量写入失败，事件将重试一次 (run=%s)", self.run_id
                )
                if not self._closed:
                    self._batch = batch + self._batch

    # -------------------------------------------------------------- 关闭

    async def flush(self) -> None:
        """立即落库当前批次（run 结束前调用）。"""
        await self._flush()

    async def close(self) -> None:
        """停止后台任务并保证最后一批落库。"""
        self._closed = True
        self._wake.set()
        if not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):  # pragma: no cover
                self._task.cancel()
        await self._flush()

    @property
    def pending(self) -> int:
        """当前还没落库的事件条数（测试与排障用）。

        这是内存批次的瞬时值——后台循环随时可能正在冲刷，读到的数字只能用来
        判断「还有没有没写下去的东西」，不能当作持久化水位。
        """
        return len(self._batch)
