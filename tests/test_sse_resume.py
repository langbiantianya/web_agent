"""SSE 续传：回放 + 实时合并去重（设计文档 §5.4）。

这是整个服务最容易写错、错了最难发现的一处，因此单独重点覆盖：

- 「先订阅 → 再回放 → 按 seq 合并」必须**不漏字、不重复、不乱序**；
- 关键用例用 monkeypatch 在「回放进行中」注入新事件，确定性地复现那个
  「两步之间产生的新事件会丢失」的经典竞态，而不是靠 sleep 碰运气。
"""

from __future__ import annotations

import asyncio

import pytest

from app.config.db import SessionLocal
from app.core.bus import InMemoryBackend
from app.core.run_hub import RunHub
from app.core.sse_writer import SSEStream
from app.models.enums import EventType, RunStatus
from app.models.run import Run
from app.repo.run_repo import run_repo

_run_ids = iter(range(910_000, 999_999))


@pytest.fixture
def run_id() -> int:
    """发一个独占的 run id：这些用例共用一个库，撞 id 会互相污染断言。"""
    return next(_run_ids)


@pytest.fixture
def hub() -> RunHub:
    """每个用例一份干净的 RunHub，订阅队列互不干扰。"""
    return RunHub(bus=InMemoryBackend())


async def _make_run(run_id: int, status: str, last_seq: int) -> None:
    """直接插入 run 行，绕开编排器——状态与 last_seq 要能被精确摆布。

    ``status`` 决定走不走收尾分支，``last_seq`` 决定合成 done 的 seq。
    """
    async with SessionLocal() as db:
        db.add(
            Run(
                id=run_id,
                conversation_id=0,
                workflow_id=0,
                status=status,
                last_seq=last_seq,
            )
        )
        await db.commit()


async def _collect(stream: SSEStream, run_id: int, resume_from: int, limit: int):
    """从 ``resume_from`` 起收满 limit 条就断开（模拟读到一半的客户端）。"""
    out = []
    async for evt in stream.stream(run_id, resume_from):
        out.append(evt)
        if len(out) >= limit:
            break
    return out


async def test_full_replay_from_zero(hub: RunHub, run_id: int):
    """last_seq=0 的冷启动回放：seq 从 1 起连续、不跳号，token 拼接还原原文。

    这是续传的地基——seq 一旦有洞，客户端按 seq 合并就会出现空洞。
    """
    await _make_run(run_id, RunStatus.DONE.value, 5)
    hub.register(run_id, start_seq=0)
    for i in range(5):
        await hub.emit(run_id, EventType.TOKEN, {"content": str(i)})
    await hub.finalize(run_id)

    got = await _collect(SSEStream(hub), run_id, 0, 5)
    assert [e.seq for e in got] == [1, 2, 3, 4, 5]
    assert "".join(e.payload["content"] for e in got) == "01234"


async def test_resume_from_mid_seq_has_no_duplicates(hub: RunHub, run_id: int):
    """客户端在 seq=3 断线重连：只能收到 4、5，不能重发 1-3。"""
    await _make_run(run_id, RunStatus.DONE.value, 5)
    hub.register(run_id, start_seq=0)
    for i in range(5):
        await hub.emit(run_id, EventType.TOKEN, {"content": str(i)})
    await hub.finalize(run_id)

    got = await _collect(SSEStream(hub), run_id, 3, 2)
    assert [e.seq for e in got] == [4, 5]


async def test_events_emitted_during_replay_are_not_lost(
    hub: RunHub, run_id: int, monkeypatch
):
    """核心竞态：回放期间产生的新事件，必须通过订阅队列补齐。

    在 ``list_after`` 返回历史之后立刻注入 6/7/8 —— 这一刻订阅早已建立，
    所以这些事件必须被收到，且顺序正确。
    """
    await _make_run(run_id, RunStatus.RUNNING.value, 0)
    hub.register(run_id, start_seq=0)
    for i in range(1, 6):
        await hub.emit(run_id, EventType.TOKEN, {"content": str(i)})
    await hub.emit(run_id, EventType.NODE_END, {"node": "a"})
    await hub.finalize(run_id)  # 先把 1..6 冲刷进 DB

    import app.core.sse_writer as sw

    original = sw.run_event_repo.list_after

    async def inject(*args, **kwargs):
        """在「历史已查出、回放尚未下发」的窗口里注入 7/8/done。"""
        history = await original(*args, **kwargs)
        # 回放窗口内：事件 7、8、done 产生
        hub.register(run_id, start_seq=6)
        await hub.emit(run_id, EventType.TOKEN, {"content": "7"})
        await hub.emit(run_id, EventType.TOKEN, {"content": "8"})
        await hub.emit(run_id, EventType.DONE, {"status": "done"})
        return history

    monkeypatch.setattr(sw.run_event_repo, "list_after", inject)

    got = await _collect(SSEStream(hub), run_id, 0, 9)

    assert [e.seq for e in got] == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    kinds = [e.event_type for e in got]
    assert kinds[-1] == EventType.DONE.value
    contents = [e.payload.get("content") for e in got if e.event_type == "token"]
    assert contents == ["1", "2", "3", "4", "5", "7", "8"]


async def test_duplicate_events_across_replay_and_live_are_deduped(
    hub: RunHub, run_id: int, monkeypatch
):
    """回放里已经有 seq=2、3，实时队列又送来同样的 seq —— 只能各发一次。

    run 保持 running 状态，强制走「回放 → 实时」两段，才能真正触到去重分支。
    """
    await _make_run(run_id, RunStatus.RUNNING.value, 0)
    hub.register(run_id, start_seq=0)
    for i in range(1, 4):
        await hub.emit(run_id, EventType.TOKEN, {"content": str(i)})
    await hub.finalize(run_id)  # 1..3 已落库

    import app.core.sse_writer as sw

    original = sw.run_event_repo.list_after
    replayed: list = []

    async def replay_then_redeliver(*args, **kwargs):
        """回放后把已下发过的 2、3 再往实时队列塞一遍（模拟重复投递）。"""
        history = await original(*args, **kwargs)
        replayed.extend(history)
        # 订阅此时已建立，把刚回放过的 2、3 再实时推一遍（模拟重复投递）
        for e in history[1:]:
            await hub._bus.publish(f"run:{run_id}", e.to_wire())  # noqa: SLF001
        # 随后产生新事件
        hub.register(run_id, start_seq=3)
        await hub.emit(run_id, EventType.TOKEN, {"content": "4"})
        await hub.emit(run_id, EventType.DONE, {"status": "done"})
        return history

    monkeypatch.setattr(sw.run_event_repo, "list_after", replay_then_redeliver)

    got = await _collect(SSEStream(hub), run_id, 0, 4)
    seqs = [e.seq for e in got]

    assert [e.seq for e in replayed] == [1, 2, 3]
    assert seqs == [1, 2, 3, 4]  # 2、3 没有被重复下发
    assert len(seqs) == len(set(seqs))


async def test_live_only_when_nothing_in_db(hub: RunHub, run_id: int):
    """尚未落库（新 run）时也能正常实时收取。"""
    await _make_run(run_id, RunStatus.RUNNING.value, 0)
    hub.register(run_id, start_seq=0)

    async def produce():
        """DB 里啥都没有时，从实时订阅侧把 4 个事件送出来。"""
        await asyncio.sleep(0.02)
        for i in range(1, 4):
            await hub.emit(run_id, EventType.TOKEN, {"content": str(i)})
        await hub.emit(run_id, EventType.DONE, {"status": "done"})
        await hub.finalize(run_id)

    task = asyncio.create_task(produce())
    got = await _collect(SSEStream(hub), run_id, 0, 4)
    await task

    assert [e.seq for e in got] == [1, 2, 3, 4]


async def test_finished_run_without_events_yields_synthetic_done(hub: RunHub, run_id: int):
    """事件已归档清理：补一个合成 done，前端据此去拉最终 message。"""
    await _make_run(run_id, RunStatus.DONE.value, 42)

    got = await _collect(SSEStream(hub), run_id, 0, 1)
    assert len(got) == 1
    assert got[0].event_type == EventType.DONE.value
    assert got[0].payload["replayed"] is True
    assert got[0].seq == 42


async def test_client_disconnect_only_unsubscribes(hub: RunHub, run_id: int):
    """断开连接不能影响 run 继续生成（设计文档 §9）。"""
    await _make_run(run_id, RunStatus.RUNNING.value, 0)
    hub.register(run_id, start_seq=0)

    async def produce():
        """断开前后各发一个 token，供断言「断开不影响落库」比对。"""
        await asyncio.sleep(0.02)
        await hub.emit(run_id, EventType.TOKEN, {"content": "before"})
        await asyncio.sleep(0.05)
        await hub.emit(run_id, EventType.TOKEN, {"content": "after-disconnect"})

    producer = asyncio.create_task(produce())

    agen = SSEStream(hub).stream(run_id, 0)
    first = await agen.__anext__()
    assert first.payload["content"] == "before"

    await agen.aclose()  # 模拟客户端断开
    await producer
    await hub.finalize(run_id)

    async with SessionLocal() as db:
        from app.repo.run_event_repo import run_event_repo

        events = await run_event_repo.list_after(db, run_id, 0)

    # 断开之后 run 继续跑，事件照常落库
    assert [e.seq for e in events] == [1, 2]
    assert events[1].payload["content"] == "after-disconnect"


async def test_replayed_done_event_closes_stream_even_if_run_row_still_running(
    hub: RunHub, run_id: int
):
    """回归：回放里已拿到 done、但 run 行还是 running 时，绝不能挂住。

    编排器的顺序是「先 emit done → 再回写 run 状态」，所以重连恰好落在这个
    窗口里是现实场景。若此时仍进实时流，连接会永远等下去。
    """
    # run 行保持 running，但 done 事件已经落库
    await _make_run(run_id, RunStatus.RUNNING.value, 0)
    hub.register(run_id, start_seq=0)
    await hub.emit(run_id, EventType.TOKEN, {"content": "x"})
    await hub.emit(run_id, EventType.DONE, {"status": "done"})
    await hub.finalize(run_id)

    # 只等到收流为止：若修复失效，这里会超时
    got = await asyncio.wait_for(
        _collect(SSEStream(hub), run_id, 0, 2), timeout=5
    )

    assert [e.event_type for e in got] == ["token", "done"]


async def test_reconnect_after_done_before_flush_closes_with_synthetic_done(
    hub: RunHub, run_id: int, monkeypatch
):
    """回归：run 已结束但事件还没落库时重连，不能一直挂着。

    真实窗口：编排器「先 emit done、再冲刷落库、再回写 run 状态」。客户端在
    done 之后立刻重连，可能正好落在「回放为空 + run 仍是 running」的窗口里。
    实时流空闲回查 run 状态后必须能自行收尾。
    """
    await _make_run(run_id, RunStatus.RUNNING.value, 0)  # 状态还没来得及回写

    import app.core.sse_writer as sw

    async def empty_replay(*args, **kwargs):
        """让回放查不到任何行，模拟「事件还卡在内存批量队列里」。"""
        return []  # 事件还在内存批量队列里，DB 里查不到

    monkeypatch.setattr(sw.run_event_repo, "list_after", empty_replay)

    # 等一小会儿，让空闲回查生效；同时把 run 置为终态
    async def finish_later():
        """延迟把 run 置为终态，验证实时流空闲回查能自己收尾。"""
        await asyncio.sleep(0.1)
        async with SessionLocal() as db:
            await run_repo.finish(db, run_id, RunStatus.DONE, 0, None)

    finisher = asyncio.create_task(finish_later())
    got = await asyncio.wait_for(_collect(SSEStream(hub), run_id, 0, 1), timeout=10)
    await finisher

    assert got[0].event_type == EventType.DONE.value
    assert got[0].payload["replayed"] is True


async def test_replay_sees_unflushed_events(hub: RunHub, run_id: int):
    """回归：订阅后回放前，必须先把内存里未落库的事件冲刷下去。

    EventWriter 是批量异步落库的。若回放直接读 DB，就会漏掉「已经 emit、但还在
    内存批次里」的那几个事件——它们既不在 DB，也不会再进本连接的队列。
    """
    await _make_run(run_id, RunStatus.RUNNING.value, 0)
    hub.register(run_id, start_seq=0)
    for i in range(1, 4):
        await hub.emit(run_id, EventType.TOKEN, {"content": str(i)})
    # 刻意不 finalize，也不 flush —— 事件此刻应当只在内存里

    flushed: list[str] = []
    original_flush = hub.flush

    async def spy_flush(rid: int) -> None:
        """记录回放前是否调用了 flush，再转发给真实实现。"""
        flushed.append(str(rid))
        await original_flush(rid)

    hub.flush = spy_flush  # type: ignore[method-assign]

    async def finish():
        """延迟补一个 done，保证流不会因为等不到结束而挂住。"""
        await asyncio.sleep(0.3)
        await hub.emit(run_id, EventType.DONE, {"status": "done"})

    finisher = asyncio.create_task(finish())
    got = await asyncio.wait_for(_collect(SSEStream(hub), run_id, 0, 4), timeout=10)
    await finisher

    assert flushed == [str(run_id)], "回放前应当调用 hub.flush"
    contents = [e.payload.get("content") for e in got if e.event_type == "token"]
    assert contents == ["1", "2", "3"], "未落库的事件必须在回放里出现"


async def test_multiple_subscribers_each_get_full_stream(hub: RunHub, run_id: int):
    """同一 run 多连接：各自维护 max_sent，互不影响。"""
    await _make_run(run_id, RunStatus.DONE.value, 4)
    hub.register(run_id, start_seq=0)
    for i in range(1, 5):
        await hub.emit(run_id, EventType.TOKEN, {"content": str(i)})
    await hub.finalize(run_id)

    stream = SSEStream(hub)
    a = await _collect(stream, run_id, 0, 4)
    b = await _collect(stream, run_id, 2, 2)

    assert [e.seq for e in a] == [1, 2, 3, 4]
    assert [e.seq for e in b] == [3, 4]


async def test_run_not_found_raises(hub: RunHub):
    """订阅不存在的 run 必须抛 NotFoundError，而不是开一条永不结束的流。

    没有订阅者保护时，用户会对着空白页等，而真正的原因被吞掉了。
    """
    from app.core.errors import NotFoundError

    hub.register(987654, start_seq=0)
    with pytest.raises(NotFoundError):
        async for _ in SSEStream(hub).stream(987654, 0):
            break
