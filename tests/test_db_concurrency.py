"""并发下的数据库行为（V3.2 压测带出来的两个真问题）。

压测（见 ``docs/performance.md``）在 128 并发时撞出 ``database is locked``，
顺带暴露了两件事：

1. **SQLite 的并发天花板**：WAL + busy_timeout 能把上限从「一卡就死」提到
   64 并发，但 SQLite 只有一个写者，128 并发必然有请求失败。
2. **SSE 流占着 DB 事务**：FastAPI 的 yield 依赖是在响应发完之后才关闭的，
   而 SSE 响应会一直开着——于是**每条开着的 SSE 流都占住一个 DB 连接**，
   直到流结束。MySQL 池化配置下（``pool_size=10`` + ``max_overflow=20``），
   这意味着**整个服务最多 30 条并发流**，第 31 个请求直接堵在等连接上。

第 2 条是真正的生产级问题：它把「能开多少条流」从几千直接压到 30。
本文件用两条测试把这两件事钉住。
"""

from __future__ import annotations

import asyncio
import itertools

import pytest

from app.api.routes.run import stream_run
from app.config.db import SessionLocal
from app.core.ticket import issue_ticket
from app.models.conversation import Conversation
from app.models.enums import RunStatus
from app.models.run import Run
from app.repo.run_repo import run_repo

_convs = itertools.count(980_000)


async def _make_run_row(run_id: int) -> None:
    """造一个 run 行，供 SSE 路由查询。

    Args:
        run_id: 目标 run id。
    """
    async with SessionLocal() as db:
        db.add(
            Run(
                id=run_id,
                conversation_id=next(_convs),
                workflow_id=1,
                status=RunStatus.RUNNING.value,
                last_seq=0,
            )
        )
        await db.commit()


async def test_sse_route_releases_db_transaction_before_streaming():
    """SSE 路由必须在**返回流之前**就放掉 DB 事务。

    ## 为什么这条断言值钱

    FastAPI 的 yield 依赖（``get_db``）是在**响应发完之后**才关闭的，SSE 响应
    却会一直开着。于是：

    - SQLAlchemy 的会话在整个流期间都**占着连接池里的一个连接**；
    - MySQL 的默认池是 ``pool_size=10`` + ``max_overflow=20``，也就是最多 30 个
      连接——**30 条并发 SSE 流就会把池占满**，第 31 个请求堵在等连接上；
    - SQLite 用 NullPool 看不出这个症状，但 WAL 的 checkpoint 同样会被长期挂着的
      读快照拖住。

    所以断言不是「能跑通」，而是**「路由返回后事务已经结束」**——事务结束，
    连接才会被归还池子。
    """
    run_id = 98_001
    await _make_run_row(run_id)
    ticket, _ = issue_ticket(user_id="u1", run_id=run_id)

    async with SessionLocal() as db:
        await stream_run(run_id=run_id, resume_from=0, db=db, ticket=ticket)
        # 关键断言：路由已经返回了 StreamingResponse，但事务必须已经结束
        assert not db.in_transaction(), (
            "SSE 路由返回后仍持有 DB 事务——连接会被一直占着，"
            "并发流的上限会掉到连接池大小"
        )


async def test_concurrent_read_then_write_transactions_survive():
    """32 个并发「先读后写」的事务不能撞上 ``database is locked``。

    「先读后写」正是 ``POST /api/conversations/{id}/messages`` 的形状：先查会话、
    查进行中的 run，再建 run、落消息。WAL 模式下这种事务的**升级**不触发 busy
    handler，撞上别的写者会立刻失败——WAL + busy_timeout 之后仍需实测确认。
    """
    conv_id = next(_convs)
    async with SessionLocal() as db:
        db.add(
            Conversation(
                id=conv_id, session_id=f"s-{conv_id}", workflow_id=1, user_id="t"
            )
        )
        await db.commit()

    async def one(i: int) -> int:
        """一个「先读后写」的事务。

        Args:
            i: 序号。

        Returns:
            建出来的会话 id。
        """
        async with SessionLocal() as db:
            # 先读：拿到读快照
            await db.get(Conversation, conv_id)
            # 再写：这一步在 WAL 下是「读事务升级为写事务」
            new_id = next(_convs)
            db.add(
                Conversation(
                    id=new_id, session_id=f"s-{new_id}", workflow_id=1, user_id="t"
                )
            )
            await db.commit()
            return new_id

    results = await asyncio.gather(*(one(i) for i in range(32)))
    assert len(set(results)) == 32

    # 确认确实落库了
    async with SessionLocal() as db:
        assert await db.get(Conversation, results[0]) is not None


@pytest.mark.parametrize("n", [8, 16, 32])
async def test_concurrent_run_creation_does_not_fail(n: int):
    """并发建 run 不该报 ``database is locked``。

    这是压测里 128 并发失败的那个点。SQLite 上限就在 64 附近（实测），这里只
    保证**在已知安全区间内不会失败**——生产用 MySQL，不受此限。
    """
    conv_id = next(_convs)
    async with SessionLocal() as db:
        db.add(
            Conversation(
                id=conv_id, session_id=f"s-{conv_id}", workflow_id=1, user_id="t"
            )
        )
        await db.commit()

    async def one(i: int) -> int:
        """建一个 run。

        Args:
            i: 序号。

        Returns:
            run id。
        """
        async with SessionLocal() as db:
            run = await run_repo.create(
                db,
                conversation_id=conv_id,
                workflow_id=1,
                status=RunStatus.PENDING.value,
            )
            # run_repo.create 只 flush 不 commit（事务边界归编排层），这里必须自己
            # 提交——不提交的话行会被回滚掉，自增 id 也没真正分配出去。
            await db.commit()
            return run.id

    results = await asyncio.gather(*(one(i) for i in range(n)))
    assert len(set(results)) == n
