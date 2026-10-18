"""事件总线：Protocol 抽象 + 进程内实现（设计文档 §7）。

V1 使用 InMemoryBackend；V2 只需新增 RedisPubSubBackend 实现同一 Protocol，
编排器与 SSE 代码无需改动即可水平扩容。
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Protocol, runtime_checkable


class Subscription:
    """一次订阅。

    ``queue`` 同时承担两个角色：实时事件投递通道，以及 sse_writer 在「先订阅
    → DB 回放」窗口期的事件缓冲区（设计文档 §5.4）。订阅动作是**同步注册**的，
    这保证了注册完成之后产生的任何事件都不会丢失——这正是续传不漏字的前提。
    """

    __slots__ = ("channel", "queue", "_closed")

    def __init__(self, channel: str, maxsize: int = 0) -> None:
        """建立一次订阅及其缓冲队列。

        Args:
            channel: 频道名，形如 ``run:<run_id>``（唯一映射见 ``channel_of``）。
            maxsize: 队列上界，默认 0 即**无界**。这是有意选的：生成侧绝不能被
                一个读得慢的 SSE 连接拖住，慢消费者的兜底是连接超时断开，
                而不是把背压顶回 LLM 流上。
        """
        self.channel = channel
        # maxsize=0 → 无界队列：慢消费者不会阻塞生成侧（背压由 SSE 连接断开兜底）
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._closed = False

    async def get(self) -> dict:
        """取一条实时事件。

        刻意用 ``asyncio.wait_for`` 包在外层而不是设队列上界：sse_writer 靠超时
        触发「队列空 → 回查 run 状态」的兜底，队列本身有界会让 put_nowait 抛
        QueueFull，反而把慢消费者的问题转嫁给生成侧。
        """
        return await self.queue.get()

    async def close(self) -> None:
        """标记订阅已关闭。

        当前只置标志位、不排空队列：排空反而会丢掉「关闭前刚投递、还没被读走」
        的事件，而这些事件对续传是唯一权威来源。
        """
        self._closed = True


@runtime_checkable
class BusBackend(Protocol):
    """总线后端契约。"""

    def subscribe(self, channel: str) -> Subscription:
        """同步注册订阅并返回缓冲队列。"""
        ...

    async def publish(self, channel: str, payload: dict) -> None:
        """向该频道所有订阅者投递事件。"""
        ...

    async def unsubscribe(self, sub: Subscription) -> None:
        """注销订阅（客户端断开时调用）。"""
        ...


class InMemoryBackend:
    """单实例进程内总线。"""

    def __init__(self) -> None:
        """``频道 → 该频道上的全部订阅``。

        用 ``defaultdict``：空频道隐式建空集合，省掉 publish/unsubscribe 两处的
        ``setdefault``；``__init__`` 里不需要预建任何频道。
        """
        self._channels: dict[str, set[Subscription]] = defaultdict(set)

    def subscribe(self, channel: str) -> Subscription:
        """**同步**注册订阅并返回缓冲队列。

        必须是同步的：sse_writer 的顺序是「先订阅 → 再回放 DB」，如果这一步
        await 了别的东西，中间产生的事件就既不在队列里、也可能已被回放跳过，
        续传就会漏字。
        """
        sub = Subscription(channel)
        self._channels[channel].add(sub)
        return sub

    async def publish(self, channel: str, payload: dict) -> None:
        """向该频道所有订阅者投递事件。

        先 ``tuple()`` 快照一份订阅列表再投递：``put_nowait`` 不会 await，
        但消费者可能在另一个 task 里 unsubscribe 并就地改这个 set，直接遍历会
        抛 RuntimeError。

        投递用 ``put_nowait`` 且队列无界，所以 publish 几乎不耗时，广播顺序与
        ``RunHub.emit`` 的 seq 分配顺序一致——前端不会收到乱序事件。
        """
        for sub in tuple(self._channels.get(channel, ())):
            sub.queue.put_nowait(payload)

    async def unsubscribe(self, sub: Subscription) -> None:
        """注销订阅；频道上无人时顺手删掉频道键，避免空集合无限堆积。

        退订只影响这个 SSE 连接，不影响 run 继续在后台生成（设计文档 §9）。
        """
        subs = self._channels.get(sub.channel)
        if subs is not None:
            subs.discard(sub)
            if not subs:
                self._channels.pop(sub.channel, None)


_bus: InMemoryBackend | None = None


def get_bus() -> InMemoryBackend:
    """全局单例总线。V2 可在此切换为 RedisPubSubBackend。"""
    global _bus
    if _bus is None:
        _bus = InMemoryBackend()
    return _bus
