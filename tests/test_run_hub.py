"""RunHub：seq 分配、事件广播、批量落库（设计文档 §4.5 / §5.3）。

覆盖两条硬约束：
1. seq 严格递增且唯一，不重复不跳号；
2. 每个 token 一次 INSERT 被真正合并成批量写入，且一条事件都不丢。
"""

from __future__ import annotations

import asyncio
import itertools

import pytest
import sqlalchemy as sa

from app.config.db import SessionLocal
from app.core.bus import InMemoryBackend
from app.core.run_hub import RunHub
from app.models.enums import EventType
from app.models.run_event import RunEvent as RunEventRow
from app.repo.run_event_repo import run_event_repo

# 测试库是文件级共享的，每个用例必须用独立的 run_id，否则会互相看到对方的事件
_run_ids = itertools.count(900_001)


@pytest.fixture
def run_id() -> int:
    """每个用例一个全新的 run_id——测试库是文件级共享的，run_id 撞车就会互相看到对方的事件。"""
    return next(_run_ids)


@pytest.fixture
def hub() -> RunHub:
    """独立总线 + 独立 RunHub：用例会真的发事件，不能共享全局单例的状态。"""
    return RunHub(bus=InMemoryBackend())


async def _drain_run_events(run_id: int) -> list:
    """把该 run 已落库的事件全部读回来，作为断言的事实来源。"""
    async with SessionLocal() as db:
        return await run_event_repo.list_after(db, run_id, 0, limit=10_000)


async def test_seq_is_strictly_increasing_and_contiguous(hub: RunHub, run_id: int):
    """seq 必须从 1 开始严格连续——这是 SSE 断点续传的锚点，一旦跳号前端就接不上。"""
    hub.register(run_id, start_seq=0)
    for i in range(50):
        await hub.emit(run_id, EventType.TOKEN, {"content": f"t{i}"})
    last = await hub.finalize(run_id)

    events = await _drain_run_events(run_id)
    assert len(events) == 50
    assert [e.seq for e in events] == list(range(1, 51))
    assert last == 50


async def test_seq_continues_from_start_seq(hub: RunHub, run_id: int):
    """续接场景：计数器从 run.last_seq 初始化，不从 0 重来。"""
    hub.register(run_id, start_seq=100)
    await hub.emit(run_id, EventType.TOKEN, {"content": "a"})
    await hub.emit(run_id, EventType.TOKEN, {"content": "b"})
    await hub.finalize(run_id)

    events = await _drain_run_events(run_id)
    assert [e.seq for e in events] == [101, 102]


async def test_payload_is_persisted_verbatim(hub: RunHub, run_id: int):
    """事件必须原样落库：回放时重建的 SSE 帧要跟首次推送完全一致，否则续传会出现内容漂移。"""
    hub.register(run_id, start_seq=0)
    await hub.emit(run_id, EventType.TOKEN, {"node": "planner", "content": "好的"})
    await hub.emit(run_id, EventType.DONE, {"status": "done"})
    await hub.finalize(run_id)

    events = await _drain_run_events(run_id)
    assert events[0].payload == {"node": "planner", "content": "好的"}
    assert events[0].event_type == "token"
    assert events[1].event_type == "done"


async def test_concurrent_emits_get_unique_seqs(hub: RunHub, run_id: int):
    """并发 emit 不能产生重复 seq——锁必须真的生效。"""
    hub.register(run_id, start_seq=0)
    await asyncio.gather(
        *(hub.emit(run_id, EventType.TOKEN, {"i": i}) for i in range(100))
    )
    await hub.finalize(run_id)

    events = await _drain_run_events(run_id)
    seqs = [e.seq for e in events]
    assert len(seqs) == 100
    assert len(set(seqs)) == 100
    assert sorted(seqs) == list(range(1, 101))


async def test_events_are_batched_not_inserted_one_by_one(hub: RunHub, run_id: int):
    """50 条事件（batch_size=5）应该走多次批量 INSERT，而不是 50 次单条。"""
    hub.register(run_id, start_seq=0)
    for i in range(50):
        await hub.emit(run_id, EventType.TOKEN, {"i": i})
    await hub.finalize(run_id)

    events = await _drain_run_events(run_id)
    assert len(events) == 50  # 一条不少


async def test_duplicate_seq_is_rejected_by_unique_index(hub: RunHub, run_id: int):
    """uk_run_seq 是回放幂等的物理保证：重复 seq 直接报错而不是静默重复。"""
    hub.register(run_id, start_seq=0)
    await hub.emit(run_id, EventType.TOKEN, {"content": "x"})
    await hub.finalize(run_id)

    async with SessionLocal() as db:
        with pytest.raises(sa.exc.IntegrityError):
            await db.execute(
                sa.insert(RunEventRow),
                [{"run_id": run_id, "seq": 1, "event_type": "token", "payload": {}}],
            )
            await db.commit()


async def test_subscribe_receives_events_in_seq_order(hub: RunHub, run_id: int):
    """订阅者收到的顺序必须与 seq 顺序一致——sse_writer 的去重合并依赖这个前提。"""
    hub.register(run_id, start_seq=0)
    sub = await hub.subscribe(run_id)

    for i in range(10):
        await hub.emit(run_id, EventType.TOKEN, {"i": i})
    await hub.emit(run_id, EventType.DONE, {})

    got = []
    for _ in range(11):
        got.append(await sub.get())

    assert [g["seq"] for g in got] == list(range(1, 12))
    await hub.unsubscribe(sub)


async def test_unsubscribe_stops_delivery(hub: RunHub, run_id: int):
    """客户端断开只应停止投递，不影响 run 继续生成（设计文档 §9）。"""
    hub.register(run_id, start_seq=0)
    sub = await hub.subscribe(run_id)
    await hub.emit(run_id, EventType.TOKEN, {"i": 1})
    assert sub.queue.qsize() == 1

    await hub.unsubscribe(sub)
    await hub.emit(run_id, EventType.TOKEN, {"i": 2})
    assert sub.queue.qsize() == 1  # 退订后不再收到
    await hub.finalize(run_id)


async def test_finalize_flushes_remaining_batch(hub: RunHub, run_id: int):
    """不足 batch_size 的尾巴也必须落库，否则最后几个 token 会丢。"""
    hub.register(run_id, start_seq=0)
    await hub.emit(run_id, EventType.TOKEN, {"i": 1})
    await hub.emit(run_id, EventType.TOKEN, {"i": 2})
    await hub.finalize(run_id)

    assert len(await _drain_run_events(run_id)) == 2


from app.models.run_event import RunEvent as RunEventRow  # noqa: E402
