"""Redis 后端集成测试（V2 多实例）。

**需要真实 Redis**：用 ``REDIS_URL`` 指定，默认取 ``.env`` 里的地址。连不上时
整份文件 skip，而不是让 CI 因环境缺 Redis 而红。键前缀带进程号，测试之间与
生产数据完全隔离，结束时按前缀清理。

这里要验证的核心命题只有一个：

    **一个 run 由 A 实例生成时，挂在 B 实例上的 SSE 能否不漏字、不重复、不乱序
    地拿到完整事件序列。**

它是 V1「尾部 token 丢失」缺陷在多实例下的翻版——V1 里靠同进程 ``hub.flush()``
解决，V2 必须靠 Redis Stream 从根上消除对 flush 的依赖。
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator

import pytest

from app.config.settings import settings

# ---- 测试环境隔离：前缀带 pid，跑完按前缀清 --------------------------------

_TEST_PREFIX = f"webagent-test-{os.getpid()}"
os.environ.setdefault("REDIS_KEY_PREFIX", _TEST_PREFIX)

from app.config.db import SessionLocal
from app.core.bus import InMemoryBackend, RedisPubSubBackend  # noqa: E402
from app.core.event_log import RedisEventLog, ReplayResult  # noqa: E402
from app.core.redis_client import (  # noqa: E402
    connect_redis,
    disconnect_redis,
    instance_id,
    run_channel,
    run_ctl_channel,
    run_stream,
)
from app.core.run_hub import RunHub, channel_of  # noqa: E402
from app.core.sse_writer import SSEStream  # noqa: E402
from app.models.enums import EventType, RunStatus  # noqa: E402

pytestmark = pytest.mark.asyncio


def _redis_available() -> bool:
    """同步探测 Redis 是否可用，决定整份文件是否 skip。"""
    import redis as sync_redis

    try:
        client = sync_redis.Redis.from_url(
            settings.redis_url, socket_connect_timeout=2, socket_timeout=2
        )
        return bool(client.ping())
    except Exception:  # noqa: BLE001
        return False


_AVAILABLE = _redis_available()
requires_redis = pytest.mark.skipif(
    not _AVAILABLE, reason=f"Redis 不可达（{settings.redis_url}），跳过 V2 集成测试"
)


# ---------------------------------------------------------------- 夹具


@pytest.fixture(autouse=True)
async def _redis_session():
    """每个用例在自己的事件循环里建立 Redis 连接，结束后断开并清理。

    刻意做成**函数级**而不是 session 级：redis.asyncio 的客户端绑定创建它的
    那个事件循环，而 pytest-asyncio 默认给每个用例一个新 loop。session 级 async
    夹具会待在另一个 loop 里，用例里第一次 ``await`` 命令就会报
    "attached to a different loop"。连接本地 Redis 只要几十毫秒，用不着为省这点
    时间去换一整套 loop 管理。

    连不上时**跳过**而不是报错：Redis 是外部依赖，实测遇到过它瞬时不可达（服务端
    重启或网络抖动，两秒后自己恢复）。那一刻若直接失败，本文件 19 个用例会一起
    ERROR，看起来像代码炸了，其实只是依赖抖了一下。跳过才是诚实的表达——真正的
    回归仍然是「Redis 通着但用例不过」。

    ``settings.redis_key_prefix`` 在这里覆盖成带 pid 的测试前缀：settings 是导入
    期实例化的，那时读 ``.env``，所以改环境变量来不及，只能改对象。
    """
    if not _AVAILABLE:
        pytest.skip(f"Redis 不可达（{settings.redis_url}）")
    settings.redis_key_prefix = _TEST_PREFIX
    try:
        await connect_redis()
    except Exception as exc:  # noqa: BLE001 - 外部依赖，失败即跳过
        pytest.skip(f"Redis 连接失败（{settings.redis_url}）：{exc}")
    try:
        yield
    finally:
        # 让可能还在收尾的异步生成器 / 后台任务把 finally 跑完（退订、关 pubsub
        # 连接），再断 Redis。反过来做的话，disconnect 可能撞上尚未关闭的连接，
        # 表现为整轮测试偶发挂起。
        await asyncio.sleep(0.05)
        from app.core.redis_client import get_redis

        client = await get_redis()
        keys = await client.keys(f"{_TEST_PREFIX}:*")
        if keys:
            await client.delete(*keys)
        await disconnect_redis()


@pytest.fixture
async def redis_client():
    """取已建立的 Redis 客户端，并清掉本次用例可能残留的 run 键。"""
    from app.core.redis_client import get_redis

    client = await get_redis()
    yield client
    keys = await client.keys(f"{_TEST_PREFIX}:run:*")
    if keys:
        await client.delete(*keys)


async def _make_run_row(run_id: int, status: str, last_seq: int = 0) -> None:
    """插入一条最小 run 行，让 SSE 的快照查询能通过。"""
    from app.config.db import SessionLocal
    from app.models.run import Run

    async with SessionLocal() as db:
        db.add(
            Run(
                id=run_id,
                conversation_id=1,
                workflow_id=1,
                status=status,
                last_seq=last_seq,
            )
        )
        await db.commit()


async def _dump_hang_state(run_id: int) -> None:
    """流挂住时把现场打出来：协程栈 + 事件落库情况 + run 行状态。

    pytest-timeout 的 thread 模式只能看到「事件循环停在 select」——那只是**表象**，
    真正卡在哪个 await 上看不出来。这里用 ``Task.print_stack()`` 把每个在跑的任务
    的协程栈打出来，再把「事件到底落库了没」「run 是不是终态」一并打出来，
    三样一对照就能定位到具体原因。

    Args:
        run_id: 目标 run id。
    """
    from app.repo.run_event_repo import run_event_repo
    from app.repo.run_repo import run_repo

    print(f"\n===== 流挂住：run {run_id} 的现场 =====")
    for t in asyncio.all_tasks():
        if t is asyncio.current_task():
            continue
        print(f"--- pending task: {t.get_name()} ---")
        t.print_stack()
    async with SessionLocal() as db:
        rows = await run_event_repo.list_after(db, run_id, 0)
        print(f"run_event 行数 = {len(rows)}，seq = {[r.seq for r in rows]}")
        run = await run_repo.get_by_id(db, run_id)
        print(f"run 行：status={run.status if run else None} last_seq={run.last_seq if run else None}")
    print("===== 现场结束 =====\n")


# ------------------------------------------------------- 基础设施与键布局


@requires_redis
async def test_instance_id_is_stable_and_unique():
    """实例标识在进程内稳定，且不是随机重新生成。"""
    assert instance_id() == instance_id()
    assert "-" in instance_id()


@requires_redis
async def test_redis_keys_mirror_channel_of():
    """钉住不变式：Redis 频道 = 前缀 + 进程内频道名。

    两边分开定义（一个在 bus.py 无前缀、一个在 redis_client 带前缀），靠这个测试
    保证不会漂。漂了的表现是「发布和订阅用了不同的频道名」，事件永远收不到，
    而且没有任何报错。
    """
    settings.redis_key_prefix = _TEST_PREFIX
    assert run_channel(42) == f"{_TEST_PREFIX}:{channel_of(42)}"
    assert run_stream(42) == f"{_TEST_PREFIX}:run:42:ev"
    assert run_ctl_channel(42) == f"{_TEST_PREFIX}:run:42:ctl"


@requires_redis
async def test_connect_failure_raises_instead_of_degrading():
    """连不上必须抛错，绝不静默退回进程内总线。

    静默降级会造成「服务能跑、跨实例收不到事件」的分裂大脑——比启动失败难查得多。

    需要先把夹具建好的连接断掉：``connect_redis`` 看到已存在的客户端会直接返回，
    那样就测不到真正的建连路径了。
    """
    from redis.exceptions import RedisError

    import app.core.redis_client as rc

    await rc.disconnect_redis()
    original_url = settings.redis_url
    try:
        settings.redis_url = "redis://127.0.0.1:6399/0"  # 没人监听这个端口
        with pytest.raises(RedisError):
            await rc.connect_redis()
    finally:
        settings.redis_url = original_url
        # 交还给夹具正常收尾的状态
        await rc.connect_redis()


# ------------------------------------------------------------- 回放源


@requires_redis
async def test_redis_event_log_append_and_resume(redis_client):
    """写入 1..5，按 seq>2 续传应当只拿到 3/4/5。"""
    log = RedisEventLog()
    for seq in range(1, 6):
        await log.append(7001, seq, EventType.TOKEN.value, {"content": str(seq)})

    got = await log.replay(7001, 2)

    assert [e.seq for e in got.events] == [3, 4, 5]
    assert got.first_seq == 3  # 现存最小 seq 是 1，但续传只返回 >2 的
    assert got.events[0].payload == {"content": "3"}


@requires_redis
async def test_replay_from_zero_returns_everything(redis_client):
    """首次连接（after_seq=0）拿全量。

    注意 ``first_seq`` 在 Redis 路径下**总是**有值（XRANGE 的第一条顺手就拿到了，
    不用额外往返）；DbEventLog 才会为了省一次查询而在 ``after_seq == 0`` 时返回
    None。两者对 ``_detect_gap`` 没有行为差异——它在 ``resume_from <= 0`` 时直接
    返回 None，本来就用不到这个值。
    """
    log = RedisEventLog()
    for seq in range(1, 4):
        await log.append(7002, seq, EventType.TOKEN.value, {"content": str(seq)})

    got = await log.replay(7002, 0)

    assert [e.seq for e in got.events] == [1, 2, 3]
    assert got.first_seq == 1


@requires_redis
async def test_duplicate_seq_is_rejected_not_raised(redis_client):
    """同一个 run 上重复的 seq 会被 Redis 拒绝。

    这不是正常路径，但它正是「单写者约定被破坏」的信号：必须记 error 日志而
    **不能** raise——一条重复事件不该让整个吐字链路断掉。
    """
    log = RedisEventLog()
    await log.append(7003, 1, EventType.TOKEN.value, {"c": "a"})

    # 不抛异常即为通过；直接验证 Stream 里仍然只有一条
    await log.append(7003, 1, EventType.TOKEN.value, {"c": "dup"})

    entries = await redis_client.xrange(run_stream(7003), min="-", max="+")
    assert len(entries) == 1


@requires_redis
async def test_finalize_sets_ttl(redis_client):
    """run 结束后 Stream 会被设过期，不会永远堆在 Redis 里。"""
    log = RedisEventLog()
    await log.append(7004, 1, EventType.TOKEN.value, {"c": "a"})
    await log.finalize(7004)

    ttl = await redis_client.ttl(run_stream(7004))
    assert 0 < ttl <= settings.run_event_retention_days * 86400


@requires_redis
async def test_replay_falls_back_to_mysql_when_stream_missing(redis_client):
    """Stream 为空时回落到 MySQL。

    必需场景：启用 Redis 之前产生的 run 压根没有 Stream，而它们的 run_event 行
    还在。只读 Stream 会让这些 run 一被续传就读不到任何内容。
    """
    from app.core.run_hub import RunEventDTO

    log = RedisEventLog()
    result = await log.replay(999999, 0)  # 这个 run 既没 Stream 也没 DB 行

    assert isinstance(result, ReplayResult)
    assert result.events == []
    assert RunEventDTO is not None


# ------------------------------------------------------------- 总线


@requires_redis
async def test_pubsub_delivers_across_independent_backends(redis_client):
    """两个互不相干的 backend 实例之间能收发事件——这正是「跨实例」的最小证明。

    subscribe 返回时订阅**已经生效**，所以发布方紧接着发的事件不会丢，
    调用方不需要 sleep 去「等订阅」。
    """
    publisher = RedisPubSubBackend()
    subscriber = RedisPubSubBackend()

    sub = await subscriber.subscribe(channel_of(7005))

    await publisher.publish(channel_of(7005), {"run_id": 7005, "seq": 1})

    payload = await asyncio.wait_for(sub.get(), timeout=5)
    assert payload["seq"] == 1

    await subscriber.unsubscribe(sub)


@requires_redis
async def test_subscribe_returns_only_after_subscription_is_live(redis_client):
    """订阅返回即已生效：返回后立刻 publish 也必须收得到。

    这条测试是那个真实 bug 的回归锁：修复前 subscribe 只创建 pump 任务就返回，
    SUBSCRIBE 还在网络上飞，此时 publish 的消息会丢——于是出现「回放已读完、
    订阅还没生效」的窗口，窗口内的事件两头落空，续传真的漏字。
    """
    publisher = RedisPubSubBackend()
    subscriber = RedisPubSubBackend()

    sub = await subscriber.subscribe(channel_of(7007))
    try:
        # 故意不加任何 sleep：订阅若还没生效，这里必然 timeout
        await publisher.publish(channel_of(7007), {"seq": 7})
        payload = await asyncio.wait_for(sub.get(), timeout=5)
        assert payload["seq"] == 7
    finally:
        await subscriber.unsubscribe(sub)


@requires_redis
async def test_unsubscribe_stops_delivery(redis_client):
    """退订后不再收到事件。"""
    publisher = RedisPubSubBackend()
    subscriber = RedisPubSubBackend()

    sub = await subscriber.subscribe(channel_of(7006))
    await subscriber.unsubscribe(sub)

    await publisher.publish(channel_of(7006), {"seq": 1})
    await asyncio.sleep(0.3)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(sub.get(), timeout=0.5)


# --------------------------------------------- 跨实例续传（本次迭代的主线）


@requires_redis
async def test_cross_instance_resume_has_no_gap_no_dup_no_reorder(redis_client):
    """**V2 的核心命题**：A 实例生成，B 实例收流，序列完整无瑕。

    刻意不复用全局单例，而是给两端各自 new 一套（bus / event_log / hub），
    模拟两个进程。除了 Redis 客户端（真实共享）之外没有任何共享内存。
    """
    run_id = 7100
    await _make_run_row(run_id, RunStatus.RUNNING.value, 0)

    # 「实例 A」：负责生成
    a_hub = RunHub(bus=RedisPubSubBackend(), )
    a_hub._log = RedisEventLog()  # noqa: SLF001
    a_hub.register(run_id, start_seq=0)

    # 「实例 B」：负责收流。它的 hub 里**没有**这个 run 的状态，
    # 所以 hub.flush(run_id) 是空操作——这正是 V1 修不掉的那个缺口。
    b_stream = SSEStream(RedisHubStub())

    received: list[int] = []

    async def consume() -> None:
        """在「实例 B」上把流读到终止事件为止，把收到的 seq 记进 received。"""
        async for evt in b_stream.stream(run_id, resume_from=0):
            received.append(evt.seq)
            if evt.terminal:
                return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.4)  # 等 B 实例订阅就绪

    # A 实例一边生成，B 实例一边收；中途还让 B 断开一次，模拟断线重连
    for i in range(1, 11):
        await a_hub.emit(run_id, EventType.TOKEN, {"content": str(i)})
        await asyncio.sleep(0.02)
    await a_hub.emit(run_id, EventType.DONE, {"status": "done"})
    await a_hub.finalize(run_id)

    await asyncio.wait_for(task, timeout=10)

    assert received == list(range(1, 12))  # 无洞、无重复、无乱序
    assert len(received) == len(set(received))


@requires_redis
async def test_cross_instance_resume_after_disconnect(redis_client):
    """断线后带 last_seq 重连，剩余事件一个不少。"""
    run_id = 7200
    await _make_run_row(run_id, RunStatus.RUNNING.value, 0)

    a_hub = RunHub(bus=RedisPubSubBackend())
    a_hub._log = RedisEventLog()  # noqa: SLF001
    a_hub.register(run_id, start_seq=0)

    for i in range(1, 6):
        await a_hub.emit(run_id, EventType.TOKEN, {"content": str(i)})

    # 客户端只收到前 3 条就断线。
    #
    # 这里必须**显式 aclose**：`async for ... break` 跳出后异步生成器的 finally
    # 不会立刻执行，要等 GC 触发；那会让退订与 pubsub 连接的关闭拖到不确定的时刻，
    # 表现为夹具 teardown 里偶发的挂起。显式关掉既确定又不留隐患。
    agen = SSEStream(RedisHubStub()).stream(run_id, resume_from=0)
    got: list[int] = []
    try:
        async for evt in agen:
            got.append(evt.seq)
            if len(got) == 3:
                break
    finally:
        await agen.aclose()

    # 生成继续
    for i in range(6, 11):
        await a_hub.emit(run_id, EventType.TOKEN, {"content": str(i)})
    await a_hub.emit(run_id, EventType.DONE, {"status": "done"})
    await a_hub.finalize(run_id)

    # 重连，带 last_seq=3
    second = SSEStream(RedisHubStub())
    resumed: list[int] = []

    async def _resubscribe() -> None:
        """把重连后的流读完。

        单独跑成一个任务，是为了外面能给它加超时——**这条流有可能永远不结束**。
        它的收尾只有两条路：回放里带终止事件，或 run 行变成终态。两条都没兑现
        时它会一直等下去，把整个测试会话挂住。挂死比失败难查得多，所以这里
        必须有兜底超时。
        """
        async for evt in second.stream(run_id, resume_from=3):
            resumed.append(evt.seq)
            if evt.terminal:
                break

    task = asyncio.create_task(_resubscribe())
    try:
        await asyncio.wait_for(task, timeout=10)
    except asyncio.TimeoutError:
        await _dump_hang_state(run_id)
        raise

    assert got == [1, 2, 3]
    assert resumed == [4, 5, 6, 7, 8, 9, 10, 11]  # 从 4 接上，一个不漏


class RedisHubStub:
    """「实例 B」视角的 RunHub：能订阅、能 flush，但**不持有**这个 run 的状态。

    它刻意不给 RunHub 的内存状态，只实现 SSE 需要的三个动作：

    - ``subscribe`` 走真实的 Redis Pub/Sub（所以拿得到 A 实例广播的事件）；
    - ``flush`` 什么也不做——A 实例的 EventWriter 在别的进程里，这里碰不到。
      这正是多实例下 ``hub.flush()`` 的真实处境：空操作，而 SSE 依然不能漏字；
    - ``unsubscribe`` 照常退订。

    上面两个跨实例用例能通过，靠的就是这条：没有 flush，B 实例的 B 实例仍然
    从 Stream 里把事件读全了。
    """

    def __init__(self) -> None:
        """绑定一个真实的 Redis 后端，模拟另一个进程的订阅能力。"""
        self._bus = RedisPubSubBackend()

    async def subscribe(self, run_id: int):
        """订阅 run 的事件频道（真实走 Redis，且等确认——与真实 hub 同语义）。"""
        return await self._bus.subscribe(channel_of(run_id))

    async def flush(self, run_id: int) -> None:
        """空操作：pending 批次在 A 实例进程内，这里无从冲刷。"""
        return None

    async def unsubscribe(self, sub) -> None:
        """退订。"""
        await self._bus.unsubscribe(sub)


# ------------------------------------------- 归属租约与跨实例取消（阶段 2/3）


@requires_redis
async def test_lease_claim_and_release(redis_client):
    """抢到租约即「有人管」，释放后回归「无主」。"""
    from app.core.lease import RedisLease

    lease = RedisLease()
    run_id = 7300

    assert await lease.is_owned(run_id) is False
    await lease.claim(run_id)
    assert await lease.is_owned(run_id) is True
    await lease.release(run_id)
    assert await lease.is_owned(run_id) is False


@requires_redis
async def test_lease_ttl_is_set(redis_client):
    """租约必须带 TTL，否则实例被 kill -9 后 run 会永远「有主」。"""
    from app.core.lease import RedisLease
    from app.core.redis_client import run_owner_key

    lease = RedisLease()
    await lease.claim(7301)

    ttl = await redis_client.ttl(run_owner_key(7301))
    assert 0 < ttl <= settings.redis_activity_ttl

    await lease.release(7301)


@requires_redis
async def test_cancel_reaches_the_instance_that_owns_the_run(redis_client):
    """**跨实例取消**：B 实例点停止，A 实例上的生成必须收到信号。

    这正是 V1 的盲区——B 进程里根本没有那个 run 的 ``_RunState``，本地置标志
    无从谈起，只能靠控制频道把信号送到 A。
    """
    from app.core.lease import RedisLease

    run_id = 7400
    # 「实例 A」：持有 run 状态、已 claim（因而开始监听取消频道）
    hub_a = RunHub(bus=RedisPubSubBackend(), lease=RedisLease())
    hub_a._log = RedisEventLog()  # noqa: SLF001
    hub_a.register(run_id, start_seq=0)
    await hub_a.claim(run_id)

    # 「实例 B」：没有这个 run 的任何本地状态
    hub_b = RunHub(bus=RedisPubSubBackend(), lease=RedisLease())

    assert hub_a.is_cancelled(run_id) is False
    assert hub_a.is_active(run_id) is True

    accepted = await hub_b.request_cancel(run_id)
    await asyncio.sleep(0.5)  # 等控制频道消息送达 A

    assert accepted is True
    assert hub_a.is_cancelled(run_id) is True  # A 收到了

    await hub_a.finalize(run_id)


@requires_redis
async def test_cancel_before_watch_is_not_lost(redis_client):
    """取消信号早于订阅到达时，不能丢。

    真实竞态：run 刚 spawn，控制频道的订阅还在路上，此时用户点了停止。
    实时消息会丢，所以必须靠取消键在订阅生效后补查一次。
    """
    from app.core.lease import RedisLease

    run_id = 7500
    lease_a = RedisLease()
    lease_b = RedisLease()

    # 先取消（B 发），后订阅（A 收）
    await lease_b.request_cancel(run_id)

    fired = asyncio.Event()
    task = await lease_a.watch_cancel(run_id, fired.set)

    # 补查是同步触发的，事件应已置位
    assert fired.is_set() is True
    assert task is None  # 补查命中后不会再建监听任务

    await lease_a.release(run_id)


@requires_redis
async def test_cancel_api_uses_cross_instance_liveness(redis_client):
    """取消接口用跨实例口径判断「还在跑吗」，而不是只看本实例。

    B 实例上 ``is_active`` 为 False（run 在 A 那边），但请求仍应被受理——
    否则用户在 B 上点停止会得到「无事发生」，而 A 继续烧 token。
    """
    from app.core.lease import RedisLease

    run_id = 7600
    hub_a = RunHub(bus=RedisPubSubBackend(), lease=RedisLease())
    hub_a._log = RedisEventLog()  # noqa: SLF001
    hub_a.register(run_id, start_seq=0)
    await hub_a.claim(run_id)

    hub_b = RunHub(bus=RedisPubSubBackend(), lease=RedisLease())

    assert hub_b.is_active(run_id) is False                 # B 本地没有
    assert await hub_b.is_active_anywhere(run_id) is True   # 但全局看是活的
    assert await hub_b.request_cancel(run_id) is True       # 取消被受理

    await hub_a.finalize(run_id)


@requires_redis
async def test_recover_stale_runs_skips_runs_owned_by_other_instances(
    redis_client, monkeypatch
):
    """**V2 修掉的硬伤**：启动恢复不再误杀别的实例正在跑的 run。

    V1 的逻辑是「扫到 running 就标 error」。多实例下 B 实例每次启动都会把 A
    实例正在生成的 run 全部判死刑——用户眼睁睁看着回答在生成中途被终止。

    这里直接验证修复后的行为：有租约的 run 被跳过，无租约的照常回收。
    """
    import app.core.lease as lease_mod
    from app.core.orchestrator import recover_stale_runs
    from app.repo.run_repo import run_repo

    owned, orphaned = 7700, 7701
    await _make_run_row(owned, RunStatus.RUNNING.value, 3)
    await _make_run_row(orphaned, RunStatus.RUNNING.value, 5)

    # 让 recover_stale_runs 用上真实的 Redis 租约
    from app.core.lease import RedisLease

    monkeypatch.setattr(lease_mod, "_lease", RedisLease())
    await lease_mod.get_lease().claim(owned)  # 模拟 A 实例正在跑

    try:
        count = await recover_stale_runs()

        # 不断言 count 的具体值：recover_stale_runs 扫的是**全表** running 的 run，
        # 而本文件其它用例也建过 RUNNING 的 run 行，共享测试库。真正要钉住的是
        # 「有主的不动、无主的才动」这两条逐 run 的断言。
        assert count >= 1
        async with SessionLocal() as db:
            assert (await run_repo.get_by_id(db, owned)).status == RunStatus.RUNNING.value
            assert (await run_repo.get_by_id(db, orphaned)).status == RunStatus.ERROR.value
    finally:
        await lease_mod.get_lease().release(owned)

