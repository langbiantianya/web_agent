"""RunHub：seq 分配 + 事件广播 + 取消信号（设计文档 §5.3 / §7）。

设计要点：

1. **seq 唯一且单调**：每个 run 一把 ``asyncio.Lock`` 保护的计数器，从
   ``run.last_seq`` 初始化。分配 seq、追加批量队列、广播订阅者三步在同一把锁内
   完成，因此广播顺序 == seq 顺序，前端不会收到乱序事件。
2. **写库与推送解耦**：``EventWriter.append`` 是同步的，落库由后台任务批量完成，
   不会阻塞吐字推送。
3. **run 结束时统一更新 last_seq**：避免每个 token 都 UPDATE run 造成的写放大。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from app.core.bus import BusBackend, Subscription, get_bus
from app.core.event_log import EventLog, get_event_log
from app.core.event_writer import EventWriter, PendingEvent
from app.core.lease import RunLease, get_lease
from app.models.enums import EventType

logger = logging.getLogger(__name__)


def channel_of(run_id: int) -> str:
    """run → 总线频道名的唯一映射。

    收发两端都必须走这个函数：频道名是 RunHub 广播与 sse_writer 订阅之间唯一的
    约定，V2 换 Redis Pub/Sub 时 Redis 的 channel 命名也复用它。
    """
    return f"run:{run_id}"


@dataclass(slots=True)
class RunEventDTO:
    """一个流式事件。``seq`` 即 SSE 的 id。"""

    run_id: int
    seq: int
    event_type: str
    payload: dict

    @property
    def terminal(self) -> bool:
        """是否终止事件（done / error）——决定这条 SSE 连接该不该收尾。

        直接对字符串做 ``EventType(...)`` 转换而不是查集合成员：event_type 若是
        脏数据，宁可抛 ValueError 让上层发现，也不要静默当成「非终止」让连接
        一直挂着。
        """
        return EventType(self.event_type).terminal

    def to_wire(self) -> dict:
        """进总线的结构（V2 换 Redis 时即为 JSON 序列化对象）。"""
        return {
            "run_id": self.run_id,
            "seq": self.seq,
            "event_type": self.event_type,
            "payload": self.payload,
        }

    @classmethod
    def from_wire(cls, d: dict) -> RunEventDTO:
        """把总线投递的裸 dict 还原成 DTO。

        订阅侧（sse_writer 的实时流）只拿得到 dict。这条路径同时服务于 InMemory
        后端与未来的 Redis 后端，所以结构约定集中收敛在 ``to_wire`` /
        ``from_wire`` 这一对函数上，改字段必须成对改。
        """
        return cls(
            run_id=d["run_id"],
            seq=d["seq"],
            event_type=d["event_type"],
            payload=d["payload"],
        )


@dataclass
class _RunState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    counter: int = 0
    cancel_requested: bool = False
    writer: EventWriter | None = None


class RunHub:
    def __init__(self, bus: BusBackend | None = None, lease: RunLease | None = None) -> None:
        """持有全部在飞 run 的运行时状态。

        关键状态：
        - ``_states``：run_id → seq 计数器 / 取消标志 / EventWriter。纯内存，进程
            重启即丢，所以 ``_ensure_state`` 还会兜底从 DB 的 ``run.last_seq``
            续接，否则 seq 会回退、撞上 run_event 的 ``uk_run_seq`` 唯一索引；
        - ``_bootstrap_lock``：防止并发 emit 各自重建同一 run 的状态、重复计数。

        **单写者约定**：一个 run 只由接下 ``POST /messages`` 的那个实例负责编排，
        别的实例只能读。这条约定撑起了两处设计——seq 分配用本地计数器（省掉每个
        token 一次 Redis 往返），以及 Stream 条目 ID 直接取 seq。V2 的 ``XADD``
        在 ID 冲突时会拒绝并记 error 日志，等于给这个约定加了一道运行时护栏。
        """
        self._bus = bus or get_bus()
        self._log: EventLog = get_event_log()
        self._lease: RunLease = lease or get_lease()
        self._states: dict[int, _RunState] = {}
        self._bootstrap_lock = asyncio.Lock()

    # ------------------------------------------------------------ 生命周期

    def register(self, run_id: int, start_seq: int = 0) -> _RunState:
        """为一个 run 建立状态，seq 计数器从 ``start_seq`` 续接。"""
        state = self._states.get(run_id)
        if state is None:
            writer = EventWriter(run_id)
            state = _RunState(counter=start_seq, writer=writer)
            self._states[run_id] = state
            logger.info("run %s 已注册，seq 起始=%s", run_id, start_seq)
        return state

    async def _ensure_state(self, run_id: int) -> _RunState:
        """emit 时的兜底注册。

        进程内状态可能已经丢失（重启、状态被清理），但事件流还在继续。若此时
        拿 ``counter=0`` 重新计数，seq 会回退到 1，与 run_event 里已有的行撞上
        ``uk_run_seq``。因此新状态一律以 ``run.last_seq`` 为起点。
        """
        state = self._states.get(run_id)
        if state is not None:
            return state
        async with self._bootstrap_lock:
            state = self._states.get(run_id)
            if state is not None:
                return state
            last_seq = 0
            try:
                # 局部导入：repo 层依赖 core.run_hub 的 RunEventDTO，
                # 顶层 import 会形成循环引用。
                from app.config.db import SessionLocal
                from app.repo.run_repo import run_repo

                async with SessionLocal() as db:
                    run = await run_repo.get_by_id(db, run_id)
                    if run is not None:
                        last_seq = run.last_seq or 0
            except Exception:  # pragma: no cover - DB 不可用时按 0 起算
                logger.exception("读取 run %s 的 last_seq 失败，seq 从 0 开始", run_id)
            logger.warning(
                "run %s 在 hub 中缺失状态，按 last_seq=%s 重建", run_id, last_seq
            )
            return self.register(run_id, last_seq)

    async def finalize(self, run_id: int) -> int:
        """结束 run：冲刷最后一批事件并返回最终 seq。

        注意顺序：**先把 writer 冲刷干净，再把状态摘掉**。反过来的话，状态
        一旦被摘除，并发的 ``hub.flush()`` 就会直接 no-op，于是新订阅者的回放
        可能读到「尾部事件还没落库」的 DB，漏掉最后几个 token。
        """
        state = self._states.get(run_id)
        if state is None:
            return 0
        try:
            if state.writer is not None:
                await state.writer.close()
            # 回放源也要收尾（Redis 模式下是给 Stream 设过期，避免无限堆积）。
            # 放在 writer 关闭之后：事件已经全部进过 Stream，不会再有新写入。
            await self._log.finalize(run_id)
            # 归属租约也一并释放。挂在这里而不是编排器的 finally 里，是因为
            # finalize 才是「这个 run 的运行态确实结束了」的唯一收口——成功、
            # 取消、异常三条路径都必经它，放一处就不会漏。
            await self.release(run_id)
        finally:
            self._states.pop(run_id, None)
        seq = state.counter
        logger.info("run %s 已结束，最终 seq=%s", run_id, seq)
        return seq

    # ---------------------------------------------------------------- 事件

    async def emit(self, run_id: int, event_type: str | EventType, payload: dict) -> RunEventDTO:
        """分配 seq → 写回放源 → 追加落库队列 → 广播订阅者。

        **三步的顺序是有意为之，不能调换**：

        1. 先 ``_log.append``（Redis 模式下是同步 ``XADD``）。这样任何一个订阅者
           收到的事件都必定已经能被回放读到——哪怕回放发生在别的实例上；
        2. 再 ``writer.append``，把事件放进 MySQL 长期归档队列（同步方法，不 await）；
        3. 最后 ``publish`` 广播。放最后是为了维持上面那条不变式：先落再播，
           订阅者不会拿到一条「有推送但回放不到」的幽灵事件。

        持锁覆盖这三步，因此广播顺序 == seq 顺序，前端不会收到乱序事件。

        **Redis 不可用时降级而不是崩掉**：外部 Redis 瞬时不可达是常态（实测踩过
        一次，整个 run 在第一个节点前就夭折了）。这里把第 1、3 步的失败降级成
        告警——第 2 步的 MySQL 归档照旧进行，回放读不到 Stream 时会回落到 MySQL
        （见 :meth:`app.core.event_log.RedisEventLog.replay`），所以事件不会丢。
        """
        state = await self._ensure_state(run_id)
        etype = EventType(event_type).value

        async with state.lock:
            state.counter += 1
            seq = state.counter
            evt = RunEventDTO(run_id=run_id, seq=seq, event_type=etype, payload=payload)
            try:
                await self._log.append(run_id, seq, etype, payload)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 回放源不可用不该杀掉生成
                logger.warning(
                    "run %s 的事件写入回放源失败，本次只进 MySQL 归档", run_id, exc_info=True
                )
            if state.writer is not None:
                # 同步追加，不阻塞；后台按条数/时间批量落库
                state.writer.append(
                    PendingEvent(
                        run_id=run_id, seq=seq, event_type=etype, payload=payload
                    )
                )
            # 无界队列 put_nowait，极快；广播顺序与 seq 顺序一致
            try:
                await self._bus.publish(channel_of(run_id), evt.to_wire())
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 实时广播失败不该杀掉生成
                logger.warning(
                    "run %s 的事件广播失败，订阅者需靠回放补齐", run_id, exc_info=True
                )
        return evt

    # -------------------------------------------------------------- 订阅

    async def subscribe(self, run_id: int) -> Subscription:
        """订阅。**返回时订阅已经真正生效**，之后产生的任何事件都会进入队列。

        必须 await——V2 的 Redis 订阅要走一次网络往返才知道 SUBSCRIBE 生效，
        而 sse_writer 的「先订阅 → 再回放」依赖「订阅先于回放」这个顺序。
        详见 :mod:`app.core.bus` 开头对 t1/t2 窗口的分析。
        """
        return await self._bus.subscribe(channel_of(run_id))

    async def flush(self, run_id: int) -> None:
        """把内存里尚未落库的事件立刻写下去。

        新订阅者在回放前必须先调用它：回放是从 DB 读的，而 EventWriter 是批量
        异步落库的，两者之间存在滞后。没有这一步，「emit 完但还没 flush」的那
        几个事件会被新连接漏掉——它们既不在 DB 里，也不会再进这个连接的队列。

        **V2（Redis）下它不再是正确性前提**，只是让 MySQL 归档更及时：回放源已经
        换成 Stream，事件在 emit 时就同步写进去了。跨实例调用时这里本就是空操作
        （``_states`` 里查不到不属于本实例的 run），而那正是 V2 依赖 Stream 的原因。
        """
        state = self._states.get(run_id)
        if state is None or state.writer is None:
            return
        await state.writer.flush()

    async def unsubscribe(self, sub: Subscription) -> None:
        """客户端断开时调用——只退订，不影响 run 继续生成（设计文档 §9）。"""
        await self._bus.unsubscribe(sub)

    # -------------------------------------------------------------- 归属租约

    async def claim(self, run_id: int) -> None:
        """声明「本实例接管了这个 run」，并开始监听跨实例取消信号。

        由编排器在真正开跑前调用。V2 下这就是别的实例能正确判断
        「这个 run 还有主吗」的唯一依据——没有租约，启动恢复会把别人正在跑的
        run 误判为无主并杀掉。
        """
        await self._lease.claim(run_id)
        await self._lease.watch_cancel(run_id, lambda: self._mark_cancelled(run_id))

    async def release(self, run_id: int) -> None:
        """run 结束后释放租约与取消监听。"""
        await self._lease.release(run_id)

    def _mark_cancelled(self, run_id: int) -> None:
        """收到跨实例取消信号时置本地标志。

        只是个本地布尔赋值——因为 :meth:`is_cancelled` 在吐字热路径上每个 chunk
        都要读一次，那里绝不能出现 await 或 Redis 往返。跨实例的部分由控制频道
        在**收到信号的那一刻**解决掉了。
        """
        state = self._states.get(run_id)
        if state is not None:
            state.cancel_requested = True
            logger.info("run %s 收到跨实例取消信号", run_id)

    # -------------------------------------------------------------- 取消

    async def request_cancel(self, run_id: int) -> bool:
        """请求取消：置本地标志 + 通知持有该 run 的实例。

        是「请求」而非强制中断：不去 cancel 编排器的 asyncio task，是因为那样
        会把 execute 的收尾路径连同 EventWriter 一起打断，丢掉尾部事件。

        **本进程没有这个 run 的状态时仍然要往下走**：V2 下 run 很可能正跑在别的
        实例上，此时本地没状态可置标志，但控制频道照样能把取消送过去。返回
        True/False 的语义也跟着变了——它现在表示「取消请求已被受理」，而不是
        「本地成功置了标志」。

        Returns:
            True 表示请求已发出；False 表示这个 run 本地和全局都查无此物
            （已经结束了），调用方据此当作「无事发生」，而不是伪装成取消成功。
        """
        state = self._states.get(run_id)
        if state is not None:
            state.cancel_requested = True
        await self._lease.request_cancel(run_id)
        if state is None:
            return await self.is_active_anywhere(run_id)
        return True

    def is_cancelled(self, run_id: int) -> bool:
        """是否已被请求取消。

        吐字路径每 chunk 都要轮询一次，所以必须廉价、无副作用、不 await——加锁
        或查 DB/Redis 都会直接拖慢流式输出。跨实例取消在收到控制频道消息时
        就被 :meth:`_mark_cancelled` 落到了这个本地字段上，所以这里读本地即可。
        run 状态不存在（已结束）时返回 False。
        """
        state = self._states.get(run_id)
        return bool(state and state.cancel_requested)

    def is_active(self, run_id: int) -> bool:
        """run 是否在本实例的 hub 里仍有状态。

        口径是「hub 里有状态」，不是「DB 里 status 是 running」：``finalize`` 摘掉
        状态之后、编排器回写 run 状态之前，两者会短暂不一致——这段窗口内 SSE
        已经可以安全收尾了。

        多实例下这个判断**只看本实例**，跑在别的实例上的 run 会返回 False。
        需要跨实例判断的地方用 :meth:`is_active_anywhere`。
        """
        return run_id in self._states

    async def is_active_anywhere(self, run_id: int) -> bool:
        """run 是否还在被**任意**实例生成（本实例或别的实例）。

        跨实例靠归属租约判断：键还在就说明有人管着。租约 TTL 到期即视为无主，
        所以实例被 kill -9 之后不需要任何额外清理动作，租约会自己过期。

        租约查询失败时退化为「本实例有状态就算活跃」——宁可漏判也不要把正在
        生成的 run 误报成已结束。
        """
        if self.is_active(run_id):
            return True
        return await self._lease.is_owned(run_id)

    async def shutdown(self) -> None:
        """进程退出时冲刷所有未落库事件。"""
        for run_id in list(self._states):
            try:
                await self.finalize(run_id)
            except Exception:  # pragma: no cover
                logger.exception("关闭 run %s 的 EventWriter 失败", run_id)


_hub: RunHub | None = None


def get_hub() -> RunHub:
    """全局单例 RunHub。

    必须是单例：seq 计数器与取消标志只存在于进程内存，编排器、SSE、取消接口
    若各拿各的实例，就会看到互相矛盾的 seq 并漏掉取消。

    这里不加锁是安全的——创建过程没有 await，FastAPI 启动与首次请求都在同一个
    事件循环里单线程执行，不存在中途让出执行权的窗口。
    """
    global _hub
    if _hub is None:
        _hub = RunHub()
    return _hub
