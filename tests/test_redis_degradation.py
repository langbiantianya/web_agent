"""Redis 不可用时的降级行为测试。

**不需要真实 Redis**：全部用假件模拟「Redis 抖动」，跑得动、也不会被外部依赖的
状态影响。

要钉住的命题只有一条：

    **外部 Redis 瞬时不可达，不能把正在生成的 run 打死。**

这是实测踩到的：外部 Redis 抖一下，``lease.claim`` 的 ``SET`` 抛 ConnectionError，
run 在第一个节点都没开始前就夭折了，用户看到的是「什么都没发生就失败了」。
而同一个模块里的租约续期循环早就写着「Redis 抖动不该杀掉生成」——只是 claim
这条路没遵守自己的约定。
"""

from __future__ import annotations

import asyncio
import json

import pytest
import sqlalchemy as sa

from app.core.bus import InMemoryBackend
from app.core.event_log import RedisEventLog
from app.core.lease import RedisLease
from app.core.run_hub import RunHub
from app.models.enums import EventType
from app.models.run_event import RunEvent

pytestmark = pytest.mark.asyncio


class _BoomRedis:
    """所有命令都抛 ConnectionError 的假 Redis。"""

    def __init__(self) -> None:
        """记录被调用过的命令名，方便断言「哪条路径碰到了 Redis」。"""
        self.calls: list[str] = []

    async def _boom(self, name: str):
        """统一抛错，模拟 Redis 不可达。"""
        self.calls.append(name)
        raise ConnectionError("Error 22 while writing to socket. 模拟 Redis 抖动。")

    async def set(self, *a, **kw):
        """必失败。"""
        return await self._boom("set")

    async def expire(self, *a, **kw):
        """必失败。"""
        return await self._boom("expire")

    async def exists(self, *a, **kw):
        """必失败。"""
        return await self._boom("exists")

    async def delete(self, *a, **kw):
        """必失败。"""
        return await self._boom("delete")

    async def xrange(self, *a, **kw):
        """必失败。"""
        return await self._boom("xrange")

    async def xadd(self, *a, **kw):
        """必失败。"""
        return await self._boom("xadd")

    async def publish(self, *a, **kw):
        """必失败。"""
        return await self._boom("publish")


class _FlakyRedis:
    """``set`` 前若干次失败、之后正常——用来验证「重试后能拿到租约」。"""

    def __init__(self, fail_times: int = 2) -> None:
        """``fail_times`` 是还要失败的次数；归零后所有命令都正常。"""
        self.fail_times = fail_times
        self.keys: dict[str, str] = {}

    async def set(self, key, value, ex=None, **kw):
        """前 ``fail_times`` 次失败，之后写入成功。"""
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionError("模拟 Redis 抖动")
        self.keys[key] = value
        return True

    async def expire(self, key, ttl, **kw):
        """键存在返回 1，不存在返回 0（与 Redis 语义一致）。"""
        return 1 if key in self.keys else 0

    async def exists(self, key, **kw):
        """键存在返回 1。"""
        return 1 if key in self.keys else 0

    async def delete(self, key, **kw):
        """删键。"""
        self.keys.pop(key, None)
        return 1


class _NoopLease:
    """不需要跨实例能力的假租约。"""

    async def claim(self, run_id: int) -> None:
        """空操作。"""

    async def release(self, run_id: int) -> None:
        """空操作。"""

    async def renew(self, run_id: int) -> None:
        """空操作。"""

    async def request_cancel(self, run_id: int) -> None:
        """空操作。"""

    async def is_owned(self, run_id: int) -> bool:
        """恒为 False。"""
        return False

    async def watch_cancel(self, run_id: int, on_cancel):
        """无需监听。"""
        return None

    async def unwatch_cancel(self, run_id: int) -> None:
        """空操作。"""


# ----------------------------------------------------------------- 归属租约


async def test_claim_degrades_when_redis_down():
    """Redis 不可用时 claim 不能抛——抛了就等于 run 直接夭折。"""
    lease = RedisLease(client=_BoomRedis())
    await lease.claim(9001)  # 不抛即通过
    # 降级后仍然要拉起续期循环：Redis 恢复后它会把归属键补回来
    assert 9001 in lease._renewers  # noqa: SLF001
    await lease.release(9001)


async def test_claim_retries_before_degrading():
    """抖动是短促的：重试一次就拿到租约，不该白白降级。"""
    redis = _FlakyRedis(fail_times=1)
    lease = RedisLease(client=redis)
    await lease.claim(9002)
    assert any("9002" in k for k in redis.keys), f"归属键没建起来：{redis.keys}"
    await lease.release(9002)


async def test_renew_recreates_missing_owner_key():
    """归属键没建起来（降级运行过）时，续期要把它补上。

    ``expire`` 对不存在的键是 no-op，光续期等于什么都没做——Redis 恢复后这条 run
    会一直「无主」，别的实例可能把它当无主的回收掉。
    """
    redis = _FlakyRedis(fail_times=99)  # set 永远失败
    lease = RedisLease(client=redis)
    await lease.claim(9003)  # 降级，没有键
    assert redis.keys == {}

    redis.fail_times = 0  # Redis 恢复
    await lease.renew(9003)  # 续期应把键补起来
    assert redis.keys, "Redis 恢复后续期没把归属键补上"


# ------------------------------------------------------------------- 事件流


async def test_emit_survives_event_log_failure():
    """回放源（Redis Stream）写失败时，事件仍然要发出去、仍然要进 MySQL 归档。"""
    hub = RunHub(bus=InMemoryBackend(), lease=_NoopLease())

    class _BrokenLog:
        """回放源写不进去的假实现。"""

        async def append(self, run_id, seq, event_type, payload):
            """必失败。"""
            raise ConnectionError("回放源不可用")

        async def replay(self, run_id, after_seq):
            """用不到，返回空。"""
            return None

        async def finalize(self, run_id):
            """空操作。"""

    hub._log = _BrokenLog()  # noqa: SLF001
    evt = await hub.emit(9101, EventType.NODE_START, {"node": "root"})
    assert evt.seq == 1
    assert evt.event_type == EventType.NODE_START.value

    # MySQL 归档里必须有这条事件——回放读不到 Stream 时就靠它
    await hub.flush(9101)
    from app.config.db import SessionLocal

    async with SessionLocal() as db:
        rows = (
            await db.execute(
                sa.select(RunEvent).where(RunEvent.run_id == 9101)
            )
        ).scalars().all()
    assert [r.seq for r in rows] == [1], f"归档里没有这条事件：{rows}"
    await hub.finalize(9101)


async def test_emit_survives_publish_failure():
    """实时广播（Redis Pub/Sub）失败时也不能抛——吐字链路比实时性重要。"""

    class _BrokenBus(InMemoryBackend):
        """广播写不出去的假总线。"""

        async def publish(self, channel, payload):
            """必失败。"""
            raise ConnectionError("广播不可用")

    hub = RunHub(bus=_BrokenBus(), lease=_NoopLease())
    evt = await hub.emit(9102, EventType.TOKEN, {"node": "root", "content": "字"})
    assert evt.seq == 1
    await hub.finalize(9102)


# ------------------------------------------------------------------- 回放


async def test_replay_merges_stream_and_archive():
    """Stream 缺的那段要从 MySQL 归档补齐。

    Redis 抖动期间写 Stream 会失败（那段时间的事件只进了归档），于是 Stream 里
    会缺一段。只读 Stream 的话续传就会漏字——而且漏的是尾部。
    """
    from app.config.db import SessionLocal

    run_id = 9201
    # 归档里有 1、2、3 三条
    async with SessionLocal() as db:
        for seq in (1, 2, 3):
            db.add(RunEvent(
                run_id=run_id, seq=seq, event_type=EventType.TOKEN.value,
                payload={"node": "root", "content": str(seq)},
            ))
        await db.commit()

    class _PartialRedis:
        """Stream 里只有 1 和 3——2 号在 Redis 抖动时没写进去。"""

        async def xrange(self, key, min="-", max="+", count=None):
            """返回两条 Stream 条目。"""
            return [
                ("1-0", {"type": EventType.TOKEN.value, "data": json.dumps({"content": "1"})}),
                ("3-0", {"type": EventType.TOKEN.value, "data": json.dumps({"content": "3"})}),
            ]

    log = RedisEventLog(client=_PartialRedis())
    result = await log.replay(run_id, 0)
    seqs = [e.seq for e in result.events]
    assert seqs == [1, 2, 3], f"并集不完整：{seqs}"
    contents = {e.seq: e.payload.get("content") for e in result.events}
    assert contents[2] == "2", "缺的那条没从归档补上"
