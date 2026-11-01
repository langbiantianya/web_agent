"""事件回放源抽象：V1 用 MySQL，V2 用 Redis Stream（设计文档 §4.2 / §5.4）。

**为什么要把回放源抽象出来**：V1 里「回放读的是 MySQL」这件事，直接导致
``hub.flush()`` 成为续传正确性的前提——因为 EventWriter 是批量异步落库的，
emit 完还没落库的那几个 token，既不在 DB 里、也不会再进新连接的队列。

单进程时这不是问题（flush 就在同一个进程里）。但**多实例下做不到**：SSE 落在
B 实例、EventWriter 在 A 实例，B 调 ``hub.flush()`` 是个空操作，尾部事件永远补不上——
V1 修过的「尾部 token 丢失」会在多实例下原样复活。

所以 V2 把回放源换成 Redis Stream：``emit()`` 时事件就**同步** ``XADD`` 进 Stream，
任意实例的 SSE 都能 ``XRANGE`` 到它，``flush()`` 不再是正确性前提，只剩优化意义。

两种实现满足同一个 :class:`EventLog` 契约，调用方（RunHub / sse_writer）不需要知道
自己跑在 V1 还是 V2：

- :class:`DbEventLog` —— ``append`` 是空操作（EventWriter 本来就在写 MySQL），
  ``replay`` 走 ``run_event`` 表。这是 V1 行为，现有 72 个测试全部跑这条路径。
- :class:`RedisEventLog` —— ``append`` 是 ``XADD``（Stream ID 固定为 ``<seq>-0``），
  ``replay`` 走 ``XRANGE``，Stream 为空时回落 MySQL。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # 仅供类型检查；运行时一律局部导入，避免与 core.run_hub 成环
    from app.core.run_hub import RunEventDTO

logger = logging.getLogger(__name__)

# 单次回放的总量上限。分页取完之前不会撞到它；撞上了说明这条 run 的事件数
# 极其夸张（一次回放不该把内存打满）。撞上限时置 ``ReplayResult.truncated``，
# 让调用方知道这段流不连续——「不漏字」是这套设计最硬的不变量，不能静默破。
REPLAY_TOTAL_LIMIT = 50000


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """一次回放的产物。

    ``first_seq`` 是**现存事件的最小 seq**，缺口检测靠它判断「客户端要的那段是不是
    已经被裁掉了」。只在续传（``after_seq > 0``）时才有意义——首次连接不存在缺口，
    多查一次纯浪费。

    ``truncated`` 表示回放**没有取完**：后面还有事件，但已经撞上总量上限。
    这时客户端拿到的流是**不连续**的（中间缺了一段），必须让调用方知道——
    「不漏字」是这套设计最硬的不变量，静默截断等于把它悄悄破了。
    """

    events: list["RunEventDTO"]
    first_seq: int | None = None
    truncated: bool = False


@runtime_checkable
class EventLog(Protocol):
    """事件回放源契约。"""

    async def append(self, run_id: int, seq: int, event_type: str, payload: dict) -> None:
        """把一个事件**持久化**下来（回放要能读到它）。

        要求同步语义：方法返回时，该事件已可被任意实例回放。
        """
        ...

    async def replay(self, run_id: int, after_seq: int) -> ReplayResult:
        """回放 ``seq > after_seq`` 的历史事件，按 seq 升序。"""
        ...

    async def finalize(self, run_id: int) -> None:
        """run 结束后的收尾（释放资源 / 设置过期）。"""
        ...


def _make_event_dto(run_id: int, seq: int, event_type: str, payload: dict) -> "RunEventDTO":
    """构造 ``RunEventDTO``。

    局部导入是为了打断 ``event_log → run_hub → event_log`` 的循环：RunHub 要用
    EventLog 追加事件，而 EventLog 的回放产物又必须是 RunEventDTO。``run_event_repo``
    里同样的局部导入是同一个原因、同一种解法。
    """
    from app.core.run_hub import RunEventDTO

    return RunEventDTO(run_id=run_id, seq=seq, event_type=event_type, payload=payload)


def _seq_of(entry_id: str) -> int:
    """把 Stream 条目 ID ``"<seq>-0"`` 解析回整数 seq。"""
    return int(entry_id.split("-", 1)[0])


class DbEventLog:
    """V1 实现：回放源是 MySQL ``run_event`` 表。"""

    async def append(self, run_id: int, seq: int, event_type: str, payload: dict) -> None:
        """空操作：落库由 :class:`app.core.event_writer.EventWriter` 负责。

        这里再写一遍就是重复 INSERT——虽然 ``uk_run_seq`` 会兜住，但纯属浪费往返，
        而且批量路径和单条路径混在一起会让 EventWriter 的重试语义变得难以推理。
        """
        return None

    async def replay(self, run_id: int, after_seq: int) -> ReplayResult:
        """从 ``run_event`` 表回放 ``seq > after_seq`` 的行。

        **分页取完，不截断**：以前这里是 ``ORDER BY seq ASC LIMIT 5000``——一次
        只取**最旧**的 5000 条。run 的事件数超过 5000 之后（每个 token delta 一
        条，长回答就到），后面那截就永远取不到：它既不在回放里，也不在实时队列
        里（实时队列只装「订阅之后发布的事件」），于是客户端拿到的是一段中间
        缺了一截、seq 跳变的回答，而且**没有任何告警**。

        现在按页取、直到取完或撞上总量上限；撞上限时置 ``truncated``，让调用方
        知道这段流不连续。

        ``first_seq`` 只在续传时才查：它是缺口检测的输入，首次连接不需要。
        """
        from app.config.db import SessionLocal
        from app.repo.run_event_repo import REPLAY_LIMIT, run_event_repo

        events: list[RunEventDTO] = []
        truncated = False
        cursor = after_seq
        async with SessionLocal() as db:
            while True:
                page = await run_event_repo.list_after(db, run_id, cursor)
                events.extend(page)
                if len(page) < REPLAY_LIMIT:
                    break
                cursor = page[-1].seq
                if len(events) >= REPLAY_TOTAL_LIMIT:
                    truncated = True
                    break
            first_seq = (
                await run_event_repo.first_seq(db, run_id) if after_seq > 0 else None
            )
        return ReplayResult(events=events, first_seq=first_seq, truncated=truncated)

    async def finalize(self, run_id: int) -> None:
        """空操作：MySQL 里的事件由 ``RUN_EVENT_RETENTION_DAYS`` 定期清理。"""
        return None


class RedisEventLog:
    """V2 实现：回放源是 Redis Stream，条目 ID 固定为 ``<seq>-0``。

    显式指定 ID 是这套方案能成立的关键。Redis 要求新 ID 严格大于当前末位，而 seq
    在单个 run 内单调递增，于是：

    - 写入是 ``XADD key 42-0 ...``，断点续传就是 ``XRANGE key (42-0 +``，
      不必按字段过滤，直接借 Redis 自己的有序结构；
    - 万一两处同时给同一个 run 发号，Redis 会拒绝较小的 ID——这正好是一道
      单写者约定的护栏，而不是悄悄写进一条乱序数据。
    """

    def __init__(self, client=None) -> None:
        """绑定 Redis 客户端；``None`` 表示延迟到首次使用时取全局单例。"""
        self._client = client

    async def _redis(self):
        """取 Redis 客户端，必要时绑定全局单例。

        ``get_redis()`` 本身是协程（连接由 lifespan 建立），所以这里必须 await。
        忘记 await 的症状很隐蔽：拿到的是一个 coroutine 对象，调用 ``.xadd`` 时
        才报 "'coroutine' object has no attribute"，和真正的连接问题长得完全不像。
        """
        if self._client is None:
            from app.core.redis_client import get_redis

            self._client = await get_redis()
        return self._client

    async def append(self, run_id: int, seq: int, event_type: str, payload: dict) -> None:
        """同步写入 Stream —— 这是「回放不依赖 flush」的全部前提。

        用近似裁剪（``MAXLEN ~ n``）限制单个 run 的条目数；只砍最旧的，砍掉之后
        续传会走缺口检测跳到实时流，不会静默漏字。

        「ID 重复/过小」被单独捕获：它意味着同一个 run 上出现了第二个写入者，
        属于严重 bug，但不该让吐字链路整个断掉。其余 Redis 错误照常抛出。
        """
        from redis.exceptions import ResponseError

        from app.config.settings import settings
        from app.core.redis_client import run_stream

        key = run_stream(run_id)
        try:
            await (await self._redis()).xadd(
                key,
                {"type": event_type, "data": json.dumps(payload, ensure_ascii=False)},
                id=f"{seq}-0",
                maxlen=settings.redis_stream_maxlen,
                approximate=True,
            )
        except ResponseError as exc:
            if "equal or smaller" in str(exc):
                logger.error(
                    "run %s 的 seq=%s 重复写入 Stream，同一 run 疑似有第二个写入者",
                    run_id,
                    seq,
                )
            else:
                raise

    async def replay(self, run_id: int, after_seq: int) -> ReplayResult:
        """``XRANGE`` 回放 ``seq > after_seq`` 的条目。

        **分页取完，不截断**：一次 ``XRANGE`` 的 count 只能取到一段，剩下的就
        丢了——而丢掉的正好是**最新**的那一段（Redis 返回的是从小到大的顺序），
        它不在 MySQL 归档里（归档是批量落库的，滞后），也不在实时队列里
        （实时队列只装「订阅之后发布的事件」）。于是长 run 的续传会拿到一段
        缺了一截的流，且毫无告警。

        上限取 ``REPLAY_TOTAL_LIMIT``；撞上限时置 ``truncated``。

        **Stream 里什么都没有时回落到 MySQL**。这不是可选兜底而是必需的：Stream
        有 TTL、MySQL 保留 7 天，而启用 Redis 之前产生的 run 压根没有 Stream。
        只读 Stream 会让这些 run 一被续传就读不到内容。

        **Stream 有内容时取两边的并集**。原因：Redis 抖动期间写 Stream 会失败，
        那些事件只进了 MySQL 归档（见 :meth:`app.core.run_hub.RunHub.emit` 的降级
        处理），于是 Stream 里会**缺一段**。只读 Stream 的话，续传就会漏掉那段——
        而且是「越靠后的越容易漏」，因为缺的是尾部。只读 MySQL 也不对：归档是批量
        落库的，生成方还没 flush 时它是滞后的，跨实例续传恰恰是靠 Stream 才拿到
        最新事件的。取并集，两边各自缺的部分正好互补。

        **``first_seq`` 取并集后的最小 seq**，不是 Stream 自己的最小 seq：Redis
        抖动导致 Stream 缺了前段时，MySQL 里那些事件其实**还在**。曾经这里报的是
        Stream 的最小 seq，缺口检测据此判成「缺了 699 条」→ 整段 history 一条都
        不发——明明 MySQL 里有，却被当成归档清理掉了。
        """
        entries = await self._xrange_all(run_id, after_seq)
        if not entries:
            return await DbEventLog().replay(run_id, after_seq)

        events = [
            _make_event_dto(
                run_id,
                _seq_of(entry_id),
                fields.get("type", ""),
                json.loads(fields.get("data") or "{}"),
            )
            for entry_id, fields in entries
        ]
        # 与 MySQL 归档取并集：Stream 缺的那段只能从归档里补
        merged: dict[int, RunEventDTO] = {e.seq: e for e in events}
        try:
            archived = await DbEventLog().replay(run_id, after_seq)
        except Exception:  # pragma: no cover - 归档读失败不该让回放整个失败
            logger.exception("run %s 读 MySQL 归档失败，只回放 Stream 里的部分", run_id)
            archived = ReplayResult(events=[])
        for e in archived.events:
            merged.setdefault(e.seq, e)
        ordered = [merged[seq] for seq in sorted(merged)]
        truncated = len(ordered) > REPLAY_TOTAL_LIMIT
        if truncated:
            ordered = ordered[:REPLAY_TOTAL_LIMIT]
        # 并集里最小的那条 seq ——「现存事件里最早的一条」，缺口检测要的就是它
        first_seq = ordered[0].seq if ordered else None
        return ReplayResult(events=ordered, first_seq=first_seq, truncated=truncated)

    async def _xrange_all(self, run_id: int, after_seq: int) -> list[tuple[str, dict]]:
        """分页 XRANGE，直到取完或撞上总量上限。

        每页用上一页最后一条的 seq 作为下界。XRANGE 的 ``min`` 是**闭区间**，
        所以要用 ``(seq-0`` 这种「排他」写法才不会把边界那条重复拿到。
        """
        from app.repo.run_event_repo import REPLAY_LIMIT

        from app.core.redis_client import run_stream

        redis = await self._redis()
        key = run_stream(run_id)
        out: list[tuple[str, dict]] = []
        cursor = f"({after_seq}-0"
        while True:
            page = await redis.xrange(key, min=cursor, max="+", count=REPLAY_LIMIT)
            if not page:
                break
            out.extend(page)
            if len(page) < REPLAY_LIMIT:
                break
            cursor = f"({_seq_of(page[-1][0])}-0"
            if len(out) >= REPLAY_TOTAL_LIMIT:
                break
        return out

    async def finalize(self, run_id: int) -> None:
        """run 结束后给 Stream 设过期，避免 Redis 里无限堆积。

        TTL 取 ``RUN_EVENT_RETENTION_DAYS``，让「Stream 还在」大致等价于
        「MySQL 里还在」；真正的兜底是 ``replay`` 里那条 MySQL 回落路径。
        """
        from app.config.settings import settings
        from app.core.redis_client import run_stream

        ttl = max(1, settings.run_event_retention_days) * 86400
        try:
            await (await self._redis()).expire(run_stream(run_id), ttl)
        except Exception:  # pragma: no cover - 收尾失败不该影响生成结果
            logger.exception("run %s 的 Stream 设置过期失败", run_id)


_log: EventLog | None = None


def get_event_log() -> EventLog:
    """全局单例回放源，按配置选择实现。

    与 :func:`app.core.bus.get_bus` 是同一套「按配置选实现」的套路，两处必须
    保持一致——否则会出现「事件写进 Redis、回放却去 MySQL 找」这种错配，
    症状是续传大面积失败且极难定位。
    """
    global _log
    if _log is None:
        from app.core.redis_client import redis_is_enabled

        _log = RedisEventLog() if redis_is_enabled() else DbEventLog()
    return _log


def reset_event_log() -> None:
    """丢弃全局回放源单例（仅测试用，理由同 ``reset_bus``）。"""
    global _log
    _log = None
