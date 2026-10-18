"""SSE 流：回放 + 实时合并去重（设计文档 §5.4）。

这是整个服务的正确性核心。重连时存在一个经典的两难：

- 「先查 DB 回放，再订阅实时」→ 两步之间新产生的事件**永久丢失**（漏字）；
- 「先订阅，再回放」→ 订阅队列里的事件与回放结果**可能乱序或重复**。

本实现的做法是：**先同步订阅**（``hub.subscribe`` 立即注册并返回队列，队列本身就是
「先订阅」窗口期的事件缓冲区），**再回放 DB**，最后按 ``seq > max_sent`` 合并去重
输出。seq 单调递增且 ``run_event`` 上有 ``uk_run_seq`` 唯一索引，因此合并过程天然
幂等——不漏字、不重复、不乱序。

补充处理：
- **回放缺口**：若现存最小 seq 远大于 ``resume_from + 1``（事件已被归档清理），
  跳过回放直接进实时流，并在结束时补一个 done，让前端去拉最终 message；
- **已结束的 run**：若本次流没有见过终止事件，补发一个合成 done（携带
  ``replayed: true``），前端据此关闭连接并刷新会话消息。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

from app.config.db import SessionLocal
from app.config.settings import settings
from app.core.errors import GoneError, NotFoundError
from app.core.run_hub import RunEventDTO, RunHub, get_hub
from app.models.enums import EventType, RunStatus
from app.repo.run_event_repo import run_event_repo
from app.repo.run_repo import run_repo

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    """回放时读到的 run 终态快照。"""

    status: str
    last_seq: int
    error_msg: str | None
    first_seq: int | None

    @property
    def terminal(self) -> bool:
        """run 是否已进入终态（done / error / cancelled）。

        依据取自 DB 的 status 而不是事件流：本流可能压根没见过终止事件（重连太早，
        或事件已被归档清理），此时只有这份快照能告诉 SSE 连接「可以收尾了」。
        """
        return RunStatus(self.status).terminal


class SSEStream:
    def __init__(self, hub: RunHub | None = None) -> None:
        """绑定事件源。默认取全局 RunHub 单例，测试可注入假 hub 替代。"""
        self.hub = hub or get_hub()

    async def stream(
        self, run_id: int, resume_from: int = 0
    ) -> AsyncIterator[RunEventDTO]:
        """产出一条 SSE 事件流：DB 回放 + 实时合并去重。

        步骤顺序就是本函数全部复杂度的来源，**不要调整**：
        1. 先同步订阅——此后产生的任何事件都进 ``sub.queue``；
        2. 再 ``hub.flush()``——让 DB 追上内存，回放才读得到最新的尾部；
        3. 再回放 DB——此时回放集与订阅集在「订阅后、flush 前」这段存在重叠；
        4. 输出一律按 ``seq > max_sent`` 去重——seq 单调递增且 run_event 上有
           ``uk_run_seq`` 唯一索引，合并天然幂等，于是「不漏字、不重复、不乱序」
           三件事同时成立。

        「先订阅后回放」之所以敢这么做，靠的是第 4 步；若换成先回放再订阅，
        两步之间新产生的事件既不在 DB 也不会进队列，会永久丢失。

        Args:
            run_id: 要订阅的 run。
            resume_from: 客户端已收到的最大 seq，0 表示从头。优先用 SSE 的
                ``Last-Event-ID`` 头，query 参数只是兜底。

        Yields:
            按 seq 严格递增的 ``RunEventDTO``；见到终止事件即结束。

        Raises:
            NotFoundError: run 不存在。
        """
        # ① 同步订阅：此刻之后产生的任何事件都会进入 sub.queue，不会遗漏
        sub = self.hub.subscribe(run_id)
        try:
            max_sent = resume_from
            yielded_terminal = False

            # ② 先把内存里未落库的事件冲刷下去，让回放读到的是**最新**状态。
            #    顺序很重要：先订阅再 flush，这样 flush 期间产生的事件既进了队列
            #    也进了 DB，两条路径都能覆盖，不会出现「回放缺口」。
            await self.hub.flush(run_id)

            # ③ 回放 DB 历史
            history, run_snapshot = await self._replay(run_id, resume_from)
            if run_snapshot is None:
                raise NotFoundError(f"run {run_id} 不存在")

            gap = self._detect_gap(resume_from, history, run_snapshot)
            if gap is not None:
                logger.warning(
                    "run %s 回放缺口 seq %s -> %s（%s 条），跳到实时流",
                    run_id, resume_from, history[0].seq if history else "N/A", gap,
                )
            elif history:
                # ④ 按 seq 去重合并回放结果
                for evt in history:
                    if evt.seq > max_sent:
                        yield evt
                        max_sent = evt.seq
                        if evt.terminal:
                            yielded_terminal = True
                            break

            # ⑤ 已经见过终止事件 → 直接关闭。
            #    必须先于下面的「run 是否已结束」判断：编排器是「先 emit done、
            #    再回写 run 状态」，所以存在一个窗口——回放里已经拿到了 done，
            #    但快照里 run 仍是 running。此时若继续进实时流，就会永远等下去。
            if yielded_terminal:
                return

            # ⑥ run 已结束但本次流没拿到终止事件（重连得太早，或事件已归档）：
            #    补一个合成 done，让前端关闭连接并去拉最终 message。
            if run_snapshot.terminal:
                yield self._synthetic_done(run_snapshot, run_id)
                return

            # ⑦ 实时流：订阅队列里兜住「订阅之后、回放之前」产生的那些事件
            while True:
                try:
                    evt = RunEventDTO.from_wire(
                        await asyncio.wait_for(
                            sub.get(), timeout=settings.sse_idle_recheck
                        )
                    )
                except asyncio.TimeoutError:
                    # 队列空：回查 run 状态。
                    # 存在这种窗口——run 刚结束、终止事件还在批量队列里没落库，
                    # 或者进程内 hub 状态已丢失。此时若干等下去，连接永远不会关。
                    refreshed = await self._reload(run_id)
                    if refreshed is not None and refreshed.terminal:
                        yield self._synthetic_done(refreshed, run_id)
                        return
                    continue

                if evt.seq <= max_sent:
                    continue  # 回放阶段已经发过，跳过
                yield evt
                max_sent = evt.seq
                if evt.terminal:
                    return
        finally:
            # 客户端断开只退订，run 继续在后台生成并写库（设计文档 §9）
            await self.hub.unsubscribe(sub)

    # ------------------------------------------------------------- 内部

    async def _reload(self, run_id: int) -> RunSnapshot | None:
        """重新读取 run 快照（只取状态，不回放事件）。"""
        async with SessionLocal() as db:
            run = await run_repo.get_by_id(db, run_id)
            if run is None:
                return None
            return RunSnapshot(
                status=run.status,
                last_seq=run.last_seq or 0,
                error_msg=run.error_msg,
                first_seq=None,
            )

    async def _replay(self, run_id: int, resume_from: int):
        """从 DB 回放 ``seq > resume_from`` 的历史事件，并顺带取回 run 终态快照。

        Returns:
            ``(history, snapshot)``。run 不存在时 snapshot 为 None——抛不抛 404
            交给调用方决定，因为「run 不存在」和「run 存在但无事件」要区别对待。

        ``first_seq`` 只在 ``resume_from > 0`` 时才查：它是缺口检测的输入，首次
        连接（resume_from=0）无需多一次查询。

        注意读的是 DB 而不是内存批次——因此 ``stream()`` 必须先 ``hub.flush()``，
        否则刚 emit、还没落库的那几个 token 既不在回放里也不在实时流里。
        """
        async with SessionLocal() as db:
            run = await run_repo.get_by_id(db, run_id)
            if run is None:
                return [], None
            history = await run_event_repo.list_after(db, run_id, resume_from)
            first_seq = None
            if resume_from > 0:
                first_seq = await run_event_repo.first_seq(db, run_id)
            return history, RunSnapshot(
                status=run.status,
                last_seq=run.last_seq or 0,
                error_msg=run.error_msg,
                first_seq=first_seq,
            )

    @staticmethod
    def _detect_gap(resume_from: int, history, snapshot: RunSnapshot) -> int | None:
        """检测回放缺口。返回缺失条数，无缺口返回 None。"""
        if resume_from <= 0:
            return None
        first_seq = snapshot.first_seq
        if first_seq is None:
            # 该 run 一条事件都没有：要么刚开始，要么已被清理
            return None
        if first_seq <= resume_from + 1:
            return None
        missing = first_seq - resume_from - 1
        if missing > settings.sse_replay_gap_threshold:
            return missing
        return None

    @staticmethod
    def _synthetic_done(snapshot: RunSnapshot, run_id: int) -> RunEventDTO:
        """补一个合成的 done，用于「run 已结束但本流没见过终止事件」。

        ``seq`` 复用 ``snapshot.last_seq`` 而**不新分配**：合成事件不对应任何
        run_event 行，若给它一个更大的 seq，前端的 max_sent 就会越过真实尾部，
        续传时反而去要一条不存在的事件。

        ``payload.replayed=True`` 是给前端的信号——「内容不在流里了，请拉最终
        message」，而不是「本次生成没有输出」。带上 ``error`` 让前端能如实展示
        失败原因（例如「服务重启导致生成中断」）。

        永不抛异常：这一步就是最后一道兜底，再抛异常会让 SSE 连接悬着不断开。
        """
        return RunEventDTO(
            run_id=run_id,
            seq=snapshot.last_seq,
            event_type=EventType.DONE.value,
            payload={
                "status": snapshot.status,
                "replayed": True,
                "message": "事件已结束或已归档，请通过会话消息接口获取最终内容",
                "error": snapshot.error_msg,
            },
        )


_stream: SSEStream | None = None


def get_sse_stream() -> SSEStream:
    """全局单例 SSE 流，与 ``get_hub()`` 保持同一套「一个进程一份内存状态」的用法。

    ``SSEStream`` 自身无状态（只持一个 hub 引用），所以单例化不是正确性要求，
    只是让路由层有唯一的注入点、测试里也只需替换这一个入口。
    """
    global _stream
    if _stream is None:
        _stream = SSEStream()
    return _stream
