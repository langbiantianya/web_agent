"""EventWriter 的关停语义：最后一批事件不能被静默丢掉。

这个文件盯的是一条**只有出故障时才看得见**的不变式：关停时最后一批落库失败，
事件必须被重试保住，而不是「记条日志然后丢掉」。

丢掉的后果不是少一条日志——续传靠 ``run_event`` 表回放，那几段一旦没了，
重连的客户端就**永远读不到它们**：前端看到的是回复少了一截，界面上却没有任何
报错，比直接失败难查得多。
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError

from app.config.db import SessionLocal
from app.core.event_writer import EventWriter, PendingEvent
from app.models.enums import EventType

_RUN_ID = 991_001


def _flaky_session_factory(fail_times: int):
    """造一个「前 N 次写失败、之后成功」的 SessionLocal。

    Args:
        fail_times: 前多少次 execute 抛错。

    Returns:
        (工厂, 状态字典)。状态字典里的 ``failed`` 是已失败次数。
    """
    import app.core.event_writer as ew

    real_session = ew.SessionLocal
    state = {"failed": 0}

    class _FlakySession:
        """代理真实会话，只在 execute 上按次数抛错。"""

        def __init__(self, inner):
            """记住被代理的真实会话。"""
            self._inner = inner

        async def __aenter__(self):
            """进入真实会话的上下文。"""
            await self._inner.__aenter__()
            return self

        async def __aexit__(self, *exc):
            """退出真实会话的上下文。"""
            return await self._inner.__aexit__(*exc)

        async def execute(self, *args, **kwargs):
            """前 N 次抛 ``database is locked``，之后照常执行。"""
            if state["failed"] < fail_times:
                state["failed"] += 1
                raise OperationalError(
                    "INSERT INTO run_event", {}, Exception("database is locked")
                )
            return await self._inner.execute(*args, **kwargs)

        async def commit(self):
            """提交真实会话。"""
            return await self._inner.commit()

    def factory(*args, **kwargs):
        """返回带故障注入的会话。"""
        return _FlakySession(real_session(*args, **kwargs))

    return factory, state


async def _rows_of(run_id: int) -> list[int]:
    """读出某个 run 已落库的 seq 列表。

    Args:
        run_id: 目标 run id。

    Returns:
        按 seq 升序的 seq 列表。
    """
    from app.repo.run_event_repo import run_event_repo

    async with SessionLocal() as db:
        rows = await run_event_repo.list_after(db, run_id, 0)
    return [r.seq for r in rows]


async def test_close_retries_final_batch_instead_of_dropping_it(monkeypatch):
    """关停时最后一批写失败必须重试保住，不能静默丢。

    去掉重试逻辑这条就会失败：批次被丢掉，库里一行都没有。
    """
    factory, state = _flaky_session_factory(fail_times=2)
    monkeypatch.setattr("app.core.event_writer.SessionLocal", factory)

    # batch_size 调大、flush_interval 调长：让后台循环**不会**自己触发 flush，
    # 于是失败只可能发生在 close() 的收尾路径上——正是要盯的那条路径。
    writer = EventWriter(run_id=_RUN_ID, batch_size=1000, flush_interval=60.0)
    writer.append(
        PendingEvent(
            run_id=_RUN_ID,
            seq=1,
            event_type=EventType.TOKEN.value,
            payload={"content": "x"},
        )
    )

    await writer.close()

    assert state["failed"] >= 2, "故障没被注入进去，测的不是重试路径"
    assert await _rows_of(_RUN_ID) == [1], "最后一批事件没能落库——被丢掉了"


async def test_close_keeps_working_when_db_is_healthy():
    """正常路径不受影响：关停后事件照常落库。

    守住「重试逻辑别把正常路径也拖慢」这条底线。
    """
    writer = EventWriter(run_id=_RUN_ID + 1, batch_size=1000, flush_interval=60.0)
    writer.append(
        PendingEvent(
            run_id=_RUN_ID + 1,
            seq=1,
            event_type=EventType.TOKEN.value,
            payload={"content": "y"},
        )
    )
    await writer.close()
    assert await _rows_of(_RUN_ID + 1) == [1]


@pytest.mark.parametrize("fail_times", [10, 50])
async def test_close_gives_up_loudly_instead_of_hanging(monkeypatch, caplog, fail_times: int):
    """DB 一直坏时不能无限重试——要**有界**地放弃，并且留下明确的日志。

    「一直重试」和「立刻丢掉」都是坏设计：前者把关停拖死，后者让丢失悄无声息。
    正确做法是重试几次、然后大声说清楚丢了多少条。

    ``fail_times`` 故意远大于重试预算：只有把预算耗光，这条断言才成立——
    中途任何一次成功都会把事件救回来，那属于上一条用例的范畴。
    """
    factory, _ = _flaky_session_factory(fail_times=fail_times)
    monkeypatch.setattr("app.core.event_writer.SessionLocal", factory)

    writer = EventWriter(run_id=_RUN_ID + 2, batch_size=1000, flush_interval=60.0)
    writer.append(
        PendingEvent(
            run_id=_RUN_ID + 2,
            seq=1,
            event_type=EventType.TOKEN.value,
            payload={"content": "z"},
        )
    )

    with caplog.at_level("ERROR", logger="app.core.event_writer"):
        await writer.close()

    assert await _rows_of(_RUN_ID + 2) == []
    assert "未能落库" in caplog.text, "事件丢了却没有留下任何日志"
