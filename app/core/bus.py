"""事件总线：Protocol 抽象 + 两种实现（设计文档 §7）。

- ``InMemoryBackend``：V1 单实例，事件只在本进程内广播。
- ``RedisPubSubBackend``：V2 多实例，事件经 Redis Pub/Sub 跨进程广播。

编排器与 SSE 代码只认 :class:`BusBackend` 这个契约，换后端不需要改它们一行——
这正是当初把它抽成 Protocol 的意义。

关于 Redis 后端为什么**必须等待订阅确认**才返回：
``sse_writer`` 的「先订阅 → 再回放」里，回放读的是 Redis Stream，而 emit 时事件
已同步 ``XADD`` 进 Stream。二者取并集才能没有遗漏，但这要求订阅**先于**回放生效：

    设 t1 = Redis 确认 SUBSCRIBE 的时刻，t2 = 回放执行 XRANGE 的时刻。
    任意事件 E 都满足 publish_time > append_time，故不可能同时出现
    ``append_time > t2``（不在回放里）与 ``publish_time < t1``（不在队列里）——
    **前提是 t1 < t2**。

若 subscribe() 不等确认就返回，pump 任务只是刚被创建，两个往返并发进行，
``t2 < t1`` 是完全可能的；此时 ``append_time ∈ (t2, t1)`` 的事件两头落空，
**真的会漏字**。这是实测抓到的真 bug，不是理论担忧。
所以本契约的 subscribe 一律是协程，调用方必须 await 到订阅真正生效。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


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

    async def subscribe(self, channel: str) -> Subscription:
        """订阅并返回缓冲队列；**返回时订阅必须已经真正生效**。

        必须是协程且必须被 await——理由见模块开头对 t1/t2 的分析。InMemory 实现
        内部没有任何 await，所以对调用方没有额外开销；Redis 实现则要等到
        收到 SUBSCRIBE 确认才返回。
        """
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

    async def subscribe(self, channel: str) -> Subscription:
        """**同步注册**订阅并返回缓冲队列（方法是协程，但内部没有任何 await）。

        做成协程只是为了满足 ``BusBackend`` 契约——Redis 实现必须 await 到订阅
        真正生效，这里则注册即返回，对调用方零开销。

        注册之所以必须是同步语义：sse_writer 的顺序是「先订阅 → 再回放 DB」，
        如果这一步 await 了别的东西，中间产生的事件就既不在队列里、也可能已被
        回放跳过，续传就会漏字。
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


_bus: BusBackend | None = None


def get_bus() -> BusBackend:
    """全局单例总线，按配置选择实现。

    默认返回 ``InMemoryBackend``（V1 行为，测试与单实例开发都依赖它）。启用
    ``REDIS_ENABLED`` 时返回 ``RedisPubSubBackend``——但**不在这里连 Redis**：
    连接由 :func:`app.core.redis_client.connect_redis` 在 lifespan 里建立，
    这里的构造只绑客户端引用。
    """
    global _bus
    if _bus is None:
        # 局部导入：不启用 Redis 时，导入 bus 不应该顺带把 redis 客户端库拉进来
        from app.core.redis_client import redis_is_enabled

        if redis_is_enabled():
            _bus = RedisPubSubBackend()
        else:
            _bus = InMemoryBackend()
    return _bus


def reset_bus() -> None:
    """丢弃全局总线单例（仅测试用）。

    ``lru_cache`` 风格的模块级单例没法靠改环境变量刷新，测试里切换配置后必须
    显式调用它，否则会拿到上一个配置造出来的实现。
    """
    global _bus
    _bus = None


class RedisPubSubBackend:
    """V2 跨实例事件总线：每个频道一条 Pub/Sub 连接。

    设计取舍：**按频道各开一条连接**，而不是一个连接订阅全部频道。看起来浪费，
    但 Pub/Sub 的 SUBSCRIBE/UNSUBSCRIBE 与并发读在 redis-py 里不是线程/任务安全
    的——共用一条连接就得再套一层命令队列来串行化，复杂度远高于收益。本服务的
    并发活跃频道数（= 正在被订阅的 run 数）通常只有个位数到几十，一条连接换来的
    正确性很划算。

    事件的**持久化与回放**不归这里管，那是 :mod:`app.core.event_log` 的职责。
    本类只负责「把已经产生的事件尽快推给同实例和别的实例的订阅者」。
    """

    def __init__(self, client=None) -> None:
        """绑定 Redis 客户端；``None`` 表示延迟到首次使用时再取全局单例。

        延迟取是为了让 :func:`get_bus` 可以在 lifespan 建立连接之前调用。
        """
        self._client = client
        # Subscription → 它的 pump 任务。Subscription 未定义 __eq__，
        # 走的是默认的身份哈希，所以可以直接当 dict 键。
        self._pumps: dict[Subscription, asyncio.Task] = {}

    async def _redis(self):
        """取 Redis 客户端；未绑定且全局单例也不可用时直接报错。

        ``get_redis()`` 是协程，必须 await——漏掉的话 ``.pubsub()`` 会报在
        一个 coroutine 对象上（"'coroutine' object has no attribute"），错误现场
        离真正的原因十万八千里。
        """
        if self._client is None:
            # 局部导入：redis_client 不依赖本模块，顶层导入不会成环，
            # 但放这里能让「不启用 Redis 时完全不碰 redis 库」这条约束更明显。
            from app.core.redis_client import get_redis

            self._client = await get_redis()
        return self._client

    @staticmethod
    def _key(channel: str) -> str:
        """给进程内频道名加环境前缀。

        ``channel_of()`` 产出的是 ``run:<id>``（无前缀，因为进程内总线不需要
        隔离）；Redis 是共享资源，必须加 ``REDIS_KEY_PREFIX`` 才能多环境共存。
        """
        from app.config.settings import settings

        return f"{settings.redis_key_prefix}:{channel}"

    async def subscribe(self, channel: str) -> Subscription:
        """订阅频道，**等到 Redis 确认后才返回**，然后起 pump 把消息灌进队列。

        「等确认」不是保守，是必需：不等就会出现模块开头分析的 t2 < t1 窗口，
        那个窗口里产生的事件既不在回放里也不在实时队列里，实测会真的漏字。

        pubsub 连接在 ``subscribe`` 里创建好再交给 pump，这样 pump 启动时不必
        重新建连，也就不会有「任务刚起来、连接还没握手」的第二段空窗。
        """
        sub = Subscription(channel)
        pubsub = (await self._redis()).pubsub(ignore_subscribe_messages=True)
        await pubsub.subscribe(self._key(channel))
        task = asyncio.create_task(
            self._pump(sub, pubsub), name=f"redis-bus-{channel}"
        )
        self._pumps[sub] = task
        return sub

    async def _pump(self, sub: Subscription, pubsub) -> None:
        """把已订阅频道上的消息反序列化后塞进订阅队列。

        订阅本身已经在 :meth:`subscribe` 里确认完毕，这里只负责收消息。
        退出条件有三个，任意一个满足即返回：订阅被关闭、pump 被取消、
        Redis 连接断开。最后一种会以异常结束——此时上层 SSE 会走
        ``sse_idle_recheck`` 的兜底路径去回查 run 状态，不会永远挂着。

        ``get_message`` 用轮询超时而非永久阻塞：这样 :meth:`unsubscribe` 只需置
        关闭标志就能让它自行退出，不必 cancel 掉一个阻塞在读上的任务。
        """
        from app.config.settings import settings

        key = self._key(sub.channel)
        try:
            while not sub._closed:
                msg = await pubsub.get_message(
                    timeout=settings.redis_poll_interval,
                    ignore_subscribe_messages=True,
                )
                if msg is None or msg.get("type") != "message":
                    continue
                payload = json.loads(msg["data"])
                # 无界队列 + put_nowait：慢消费者不会把背压顶回 LLM 流上
                sub.queue.put_nowait(payload)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - 连接异常交给上层兜底
            logger.exception("Redis 订阅 %s 异常退出", key)
        finally:
            try:
                await pubsub.unsubscribe(key)
                await pubsub.aclose()
            except Exception:  # pragma: no cover
                logger.debug("关闭 Redis 订阅 %s 时出错", key, exc_info=True)

    async def publish(self, channel: str, payload: dict) -> None:
        """把事件 JSON 发到该频道，所有实例上的订阅者都会收到。

        用 ``PUBLISH`` 而非 ``XADD``：Stream 由 :mod:`app.core.event_log` 负责写，
        这里若也写一遍就是同一事件两次往返。顺序上 emit 是「先 XADD 后 PUBLISH」，
        这样订阅者收到的任何事件都必定已经在 Stream 里可回放。
        """
        await (await self._redis()).publish(
            self._key(channel), json.dumps(payload, ensure_ascii=False)
        )

    async def unsubscribe(self, sub: Subscription) -> None:
        """注销订阅：置关闭标志，**等 pump 自己收尾，不强制 cancel**。

        为什么不 cancel：pump 绝大多数时间阻塞在 ``pubsub.get_message()`` 的读上，
        直接 ``task.cancel()`` 会把这次读打断在中间，redis-py 的连接锁就留在一个
        不一致的状态；随后 :meth:`_pump` 的 finally 去 ``unsubscribe()`` /
        ``aclose()`` 时抢同一把锁会**永久挂起**——实测会整个卡住测试会话，且现象
        出现在几十行之外的用例上，极难定位。

        所以这里只置 ``_closed``，pump 会在下一个 ``get_message`` 轮询周期（不超过
        ``REDIS_POLL_INTERVAL``）自行返回并正常关闭连接。退订本来就发生在 SSE 连接
        结束之后，多等这一个周期对用户无感。只有 pump 真的卡死时才强制取消兜底。
        """
        from app.config.settings import settings

        await sub.close()
        task = self._pumps.pop(sub, None)
        if task is None or task.done():
            return
        grace = settings.redis_poll_interval + 1.0
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=grace)
        except asyncio.TimeoutError:  # pragma: no cover - 兜底，正常路径不会走到
            logger.warning("Redis 订阅 pump 未在 %.1fs 内退出，强制取消", grace)
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: B014 - 收尾期一律吞掉
                pass
