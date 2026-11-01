"""跨实例协调：归属租约 + 取消信号（V2 多实例）。

V1 里这三件事全是进程内的：

- ``hub.request_cancel`` 置一个本地布尔字段；
- ``hub.is_active`` 看本地 ``_states`` 里有没有这个 run；
- 启动时 ``recover_stale_runs`` 扫全表，把所有 running 的 run 标成 error。

单实例都没问题，**多实例下后两个直接出错**：

- 在 B 实例点「停止」，A 实例上正在跑的 run 收不到信号，用户看到界面停了，
  后台其实还在烧 token；
- B 实例刚启动、扫到 A 实例正在生成的 run，就把它标成 error——用户眼睁睁看着
  自己的回答在生成中途被判死刑。这是最严重的一个，属于「多活反而比单活更糟」。

于是把「谁在管这个 run」这件事变成 Redis 里一把**带 TTL 的租约**：生成方持租约并
周期性续期，租约过期即视为无主。任何实例都能据此判断「这个 run 还活着吗」，
而启动恢复只回收**没有主**的 run。

取消走控制频道而不是改共享布尔字段，是为了不把「每 chunk 轮询一次」变成
「每 chunk 查一次 Redis」——``is_cancelled`` 仍然只读进程内的布尔字段，
慢一点但绝对便宜，这是吐字热路径上更值得保住的性质。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class RunLease(Protocol):
    """run 归属租约 + 取消信号。"""

    async def claim(self, run_id: int) -> None:
        """声明「本实例接管了这个 run」，并开始续期。"""
        ...

    async def release(self, run_id: int) -> None:
        """run 结束后释放租约。"""
        ...

    async def renew(self, run_id: int) -> None:
        """续期一次租约（由后台续期循环调用）。"""
        ...

    async def request_cancel(self, run_id: int) -> None:
        """跨实例请求取消。"""
        ...

    async def is_owned(self, run_id: int) -> bool:
        """该 run 是否**仍有人在管**（含别的实例）。"""
        ...

    async def watch_cancel(self, run_id: int, on_cancel) -> asyncio.Task | None:
        """开始监听该 run 的取消信号，收到即调用 ``on_cancel``。"""
        ...

    async def unwatch_cancel(self, run_id: int) -> None:
        """停止监听。"""
        ...


class NoopLease:
    """V1 实现：全部空操作，行为完全由 RunHub 的进程内字段承担。

    保留它而不是删掉，是为了让 RunHub 里不需要写 ``if redis_enabled`` 的分支——
    换实现就是换一个对象，这正是 ``BusBackend`` / ``EventLog`` 已建立的套路。
    """

    async def claim(self, run_id: int) -> None:
        """空操作：无跨实例概念。"""

    async def release(self, run_id: int) -> None:
        """空操作。"""

    async def renew(self, run_id: int) -> None:
        """空操作。"""

    async def request_cancel(self, run_id: int) -> None:
        """空操作：本地置标志已经够了。"""

    async def is_owned(self, run_id: int) -> bool:
        """恒为 False：跨实例视角下「没别人在管」，由本地状态兜底。"""
        return False

    async def watch_cancel(self, run_id: int, on_cancel) -> asyncio.Task | None:
        """无需监听，返回 None 表示没有监听任务。"""
        return None

    async def unwatch_cancel(self, run_id: int) -> None:
        """空操作。"""


class RedisLease:
    """V2 实现：租约用带 TTL 的 Redis 键，取消走控制频道。

    两类键：

    - ``P:run:<id>:owner``  值为本实例标识，TTL ``REDIS_ACTIVITY_TTL``，由生成方
      周期性续期。实例被 kill -9 时不再续期，键自然过期，run 自动变成「无主」；
    - ``P:run:<id>:cancel`` 值为 ``1``，用于兜住「取消信号发出去时对方还没订阅」
      这个窗口（见 :meth:`watch_cancel`）。
    """

    def __init__(self, client=None) -> None:
        """绑定 Redis 客户端；``None`` 表示延迟到首次使用时取全局单例。"""
        self._client = client
        self._renewers: dict[int, asyncio.Task] = {}
        self._watchers: dict[int, asyncio.Task] = {}
        # run_id → 是否已请求停止监听。监听循环靠它自行退出，见 _watch_loop。
        self._stopped: dict[int, bool] = {}

    async def _redis(self):
        """取 Redis 客户端，必要时绑定全局单例（``get_redis`` 是协程，必须 await）。"""
        if self._client is None:
            from app.core.redis_client import get_redis

            self._client = await get_redis()
        return self._client

    # -------------------------------------------------------------- 租约

    async def claim(self, run_id: int) -> None:
        """标记归属并拉起续期循环。

        只写一个 ``owner`` 键就够：``is_owned`` 问的是「还有没有主」，而「是谁」
        只在排障时有用，不必单独再存一份。

        **Redis 抖动不能杀掉这次生成**：实测外部 Redis 瞬时不可达时，``SET`` 会
        抛 ConnectionError，run 在第一个节点都没开始前就夭折了——用户看到的是
        「什么都没发生就失败了」。这里改成「重试两次，还不行就降级」：不持租约
        继续跑，靠续期循环在 Redis 恢复后把归属键补上（见 :meth:`_renew_loop`）。

        降级的风险只有一条：Redis 恢复前的窗口里没有归属键，别的实例启动恢复
        可能把这条 run 当成无主回收。这比起「直接失败」小得多——失败是必然的，
        降级只是概率性的。
        """
        from app.config.settings import settings
        from app.core.redis_client import instance_id, run_owner_key

        redis = await self._redis()
        key = run_owner_key(run_id)
        for attempt in (1, 2):
            try:
                await redis.set(key, instance_id(), ex=settings.redis_activity_ttl)
                break
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - Redis 抖动按降级处理
                if attempt == 2:
                    logger.warning(
                        "run %s 归属租约写入失败（Redis 不可用），降级为无租约运行", run_id
                    )
                    break
                logger.warning("run %s 归属租约写入失败，重试一次", run_id)
                await asyncio.sleep(0.3)

        # 续期循环照常拉起：它会在 Redis 恢复后把归属键补回来
        self._renewers[run_id] = asyncio.create_task(
            self._renew_loop(run_id), name=f"lease-renew-{run_id}"
        )
        logger.info("run %s 归属本实例 %s", run_id, instance_id())

    async def _renew_loop(self, run_id: int) -> None:
        """按 TTL 的三分之一周期续期，直到被 release 或进程退出。

        续期周期必须**明显小于 TTL**：Redis 抖动一次、网络抖一下，都可能让某次
        续期迟到；周期取 TTL/3 留出两次容错窗口。TTL 到期会被别的实例（以及启动
        恢复）判定为「无主」，代价是那个 run 被误标成 error——宁可多续，也别漏。

        续期的具体做法见 :meth:`renew`。
        """
        from app.config.settings import settings

        interval = max(1.0, settings.redis_activity_ttl / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                await self.renew(run_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - Redis 抖动不该杀掉生成
                logger.exception("run %s 的租约续期失败", run_id)

    async def renew(self, run_id: int) -> None:
        """续期一次租约（续期循环之外的按需使用点）。

        用 ``expire`` 而不是 ``set``：键还在时只延 TTL，归属实例的值保持不变，
        不会把别的实例的归属覆盖掉。但 ``expire`` 对不存在的键是 no-op——首次
        ``claim`` 撞上 Redis 抖动而降级时，键根本就没建起来，光续期等于空转，
        这条 run 会一直「无主」，别的实例可能把它当无主的回收掉。

        所以这里检查 ``expire`` 的返回值：为 0 说明键没了，补一个 ``set`` 把归属
        重新立起来（Redis 恢复了才补得上，没恢复就继续等下一次）。
        """
        from app.config.settings import settings
        from app.core.redis_client import instance_id, run_owner_key

        key = run_owner_key(run_id)
        redis = await self._redis()
        refreshed = await redis.expire(key, settings.redis_activity_ttl)
        if not refreshed:
            await redis.set(key, instance_id(), ex=settings.redis_activity_ttl)

    async def release(self, run_id: int) -> None:
        """删除归属键并停掉续期、取消监听。

        显式删键而不是等 TTL：正常结束的 run 不该在 Redis 里多占一个 TTL 的时间，
        而且启动恢复会立刻把它当成「无主」，虽然那条 run 已经是终态、无害。

        两条后台任务分开处理，因为取消的安全性不同：

        - 续期循环阻塞在 ``asyncio.sleep`` 上，直接 cancel 完全安全；
        - 取消监听阻塞在 Redis 的读上，cancel 会打断读、让连接锁状态不一致，
          所以交给 :meth:`unwatch_cancel` 走「置停止标志 → 自行退出」。
        """
        from app.core.redis_client import run_owner_key

        await self.unwatch_cancel(run_id)

        renewer = self._renewers.pop(run_id, None)
        if renewer is not None and not renewer.done():
            renewer.cancel()
            try:
                await renewer
            except (asyncio.CancelledError, Exception):  # noqa: B014 - 收尾期一律吞掉
                pass

        try:
            await (await self._redis()).delete(run_owner_key(run_id))
        except Exception:  # pragma: no cover
            logger.exception("run %s 的归属键删除失败", run_id)

    async def is_owned(self, run_id: int) -> bool:
        """归属键是否还在。键在 = 有人（或曾经有人）在管这个 run。

        **查询失败时返回 True（按「有人在管」处理）**，不是 False。

        这条口径很要紧：两个调用方都把「False」理解成「这条 run 没人管了」——
        一个是启动恢复（:func:`app.core.orchestrator.recover_stale_runs`）会把
        没人管的 run 标成 error，另一个是取消接口（``is_active_anywhere``）会把
        人家正在跑的 run 标成 cancelled。Redis 抖一下就够让这两个动作误伤别的
        实例正在生成的 run——那正是本模块存在的理由（「多活反而比单活更糟」）。

        所以查不到就往安全的方向偏：**宁可漏回收，也不误杀**。真正的无主 run
        会等到 Redis 恢复后、或租约 TTL 自然过期再被回收，代价只是晚一点收敛。
        """
        from app.core.redis_client import run_owner_key

        try:
            return bool(await (await self._redis()).exists(run_owner_key(run_id)))
        except Exception:  # pragma: no cover - Redis 不可用时按「有人管」处理
            logger.exception("查询 run %s 的归属失败，按仍有人管处理", run_id)
            return True

    # -------------------------------------------------------------- 取消

    async def request_cancel(self, run_id: int) -> None:
        """置取消键 + 发控制频道消息。

        **两个动作都做，缺一不可**：

        - 控制频道是实时的，但若此刻对方还没订阅上（刚 spawn、订阅还在路上），
          消息就丢了；
        - 取消键是持久的那一半，订阅方上线后主动补查（见 :meth:`watch_cancel`）。

        只做前者会漏取消，只做后者会有延迟——用户点了停止却要等几秒才停。
        """
        from app.config.settings import settings
        from app.core.redis_client import run_ctl_channel

        redis = await self._redis()
        await redis.set(_cancel_key(run_id), "1",
                        ex=max(60, settings.redis_activity_ttl * 5))
        await redis.publish(run_ctl_channel(run_id), json.dumps({"op": "cancel"}))

    async def watch_cancel(self, run_id: int, on_cancel) -> asyncio.Task | None:
        """订阅控制频道，收到取消消息即调用 ``on_cancel``。

        订阅建立后**立刻补查一次**取消键：这是覆盖「取消发生在对方订阅之前」那个
        窗口的关键。加上这次补查之后，理论上不存在消息丢失的窗口。

        **补查失败不抛异常**：这一步在 ``claim()`` 之后立刻执行，而 ``claim()``
        已经明确把「Redis 抖动不能杀掉这次生成」写成了设计（SET 失败就降级为
        无租约运行）。补查这里若把 ConnectionError 抛出去，降级就只剩一半——
        run 会在第一个节点开始前夭折，用户看到的是「什么都没发生就失败了」。
        查不到就当作「没被取消」，继续往下走：订阅本身失败由 :meth:`_watch_loop`
        兜住（降级为「没有跨实例取消监听」），生成照常进行。
        """
        from app.core.redis_client import run_ctl_channel

        redis = await self._redis()
        # 先补查：此刻若已有取消键，说明信号在本进程订阅之前就到了
        try:
            already_cancelled = bool(await redis.exists(_cancel_key(run_id)))
        except Exception:  # noqa: BLE001 - Redis 抖动不该杀掉这次生成
            logger.warning(
                "run %s 补查取消键失败，按未取消处理", run_id, exc_info=True
            )
            already_cancelled = False
        if already_cancelled:
            on_cancel()
            return None

        task = asyncio.create_task(
            self._watch_loop(run_id, run_ctl_channel(run_id), on_cancel),
            name=f"lease-watch-{run_id}",
        )
        self._watchers[run_id] = task
        return task

    async def _watch_loop(self, run_id: int, channel: str, on_cancel) -> None:
        """监听控制频道直到收到取消或被要求停止。

        ``get_message`` 用**轮询超时**而不是 ``timeout=None``：与
        :meth:`RedisPubSubBackend.unsubscribe` 同理，直接 cancel 掉一个阻塞在读上的
        任务会让 redis-py 的连接锁处于不一致状态，之后的 ``unsubscribe()/aclose()``
        会永久挂起。轮询 + 停止标志让监听能自己干净退出。
        """
        from app.config.settings import settings

        redis = await self._redis()
        pubsub = redis.pubsub(ignore_subscribe_messages=True)
        try:
            await pubsub.subscribe(channel)
            # 订阅生效后再补查一次，覆盖「补查与 SUBSCRIBE 之间」到达的消息
            if await redis.exists(_cancel_key(run_id)):
                on_cancel()
                return
            while not self._stopped.get(run_id):
                msg = await pubsub.get_message(
                    timeout=settings.redis_poll_interval,
                    ignore_subscribe_messages=True,
                )
                if msg is None or msg.get("type") != "message":
                    continue
                if json.loads(msg["data"]).get("op") == "cancel":
                    on_cancel()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - 连接断了交给上层兜底
            logger.exception("run %s 的取消监听异常退出", run_id)
        finally:
            try:
                await pubsub.unsubscribe(channel)
                await pubsub.aclose()
            except Exception:  # pragma: no cover
                logger.debug("关闭 run %s 的取消订阅失败", run_id, exc_info=True)
            self._stopped.pop(run_id, None)

    async def unwatch_cancel(self, run_id: int) -> None:
        """停止监听该 run 的取消信号。

        同样**不直接 cancel**：置停止标志后等监听循环自己在下一个轮询周期退出，
        只有等不到才强制取消兜底。理由见 :meth:`_watch_loop`。
        """
        from app.config.settings import settings

        task = self._watchers.pop(run_id, None)
        if task is None or task.done():
            return
        self._stopped[run_id] = True
        try:
            await asyncio.wait_for(
                asyncio.shield(task), timeout=settings.redis_poll_interval + 1.0
            )
        except asyncio.TimeoutError:  # pragma: no cover - 兜底
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: B014
                pass
        finally:
            self._stopped.pop(run_id, None)


def _cancel_key(run_id: int) -> str:
    """run 的取消标记键。

    没放在 :mod:`app.core.redis_client` 里，是因为它只在取消这一条链路上使用，
    不属于「对外公开的键布局」。和 owner/active 键保持同一命名风格。
    """
    from app.config.settings import settings

    return f"{settings.redis_key_prefix}:run:{run_id}:cancel"


_lease: RunLease | None = None


def get_lease() -> RunLease:
    """全局单例租约，按配置选择实现。"""
    global _lease
    if _lease is None:
        from app.core.redis_client import redis_is_enabled

        _lease = RedisLease() if redis_is_enabled() else NoopLease()
    return _lease


def reset_lease() -> None:
    """丢弃全局租约单例（仅测试用）。"""
    global _lease
    _lease = None
