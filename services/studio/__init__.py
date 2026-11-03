"""studio：技能脚本的受限执行服务（skill-service §3）。

它是**独立的**服务：只认「一段脚本 + 一组参数」，不认识用户、不认识技能、不
认识 web_agent。与 web_agent 之间只有一个 gRPC 契约（proto 文件在仓库里只有
一份：``proto/webagent/studio/v1/studio.proto``）。

用法::

    python -m services.studio            # 监听 0.0.0.0:9000
    STUDIO_PORT=9001 python -m services.studio
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Callable

# ---------------------------------------------------------------- gRPC 的 fork 支持
#
# **必须在 import grpc 之前**设好——gRPC 的 C 核心在**首次初始化**时读这个变量，
# 之后再改就晚了。
#
# 为什么关掉：studio 跑脚本要 fork 子进程（:mod:`services.studio.executor` 用
# ``preexec_fn`` 设 RLIMIT、用 ``start_new_session`` 建独立进程组），而 gRPC 的
# C 核心为了支持多进程，会注册 ``pthread_atfork`` 处理器，在 fork 之后清理自己的
# 状态。它在「fork 时别的线程正在 gRPC 里」的场景下会**放弃清理**：
#
#     I fork_posix.cc:71] Other threads are currently calling into gRPC,
#                         skipping fork() handlers
#     F ev_epoll1_linux.cc:1121] Check failed: next_worker->state == KICKED
#
# 然后**子进程**带着坏掉的 epoll 状态去 exec，exec 之前就被 glog 的断言
# ``abort()`` 掉——脚本根本没跑起来，调用方收到的是 ``exit_code=-6``
# （SIGABRT）、stdout/stderr 全空。
#
# 这个坑在本地测不出来：它要「别的线程正好在 gRPC 里」才触发，而本地测试是
# 单线程顺序调用。真实部署里 studio 一直在跟 web_agent 保持长连接，
# 命中率就高了（实测在真容器里连着跑，十次里有三四次直接 -6）。
#
# 关掉 fork 支持之后，gRPC 不再注册 atfork 处理器，fork 出来的子进程不再被
# 它的状态拖累；子进程是脚本解释器，exec 之后也不碰 gRPC，所以没有任何副作用。
# 代价是：**这个进程不能再 fork 出用 gRPC 的子进程**——studio 没有这个需求。
#
# 用 ``setdefault`` 而不是硬写：万一将来确实需要在 gRPC 进程里 fork 出另一个
# gRPC 进程，可以显式把它设回 "1"。
os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "0")

from services.studio.config import StudioConfig
from services.studio.server import StudioService

logger = logging.getLogger(__name__)

__all__ = ["StudioConfig", "StudioService", "serve"]


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop, on_stop: Callable[[], None]
) -> None:
    """把 ``SIGTERM`` / ``SIGINT`` 接到停机回调上。

    只在 POSIX 上生效：``loop.add_signal_handler`` 在 Windows 上不支持
    （``NotImplementedError``）。Windows 上靠 Ctrl-C 的默认行为与服务端的
    ``finally`` 收尾——容器里的生产部署是 Linux。

    Args:
        loop: 事件循环。
        on_stop: 停机回调（无参）。
    """
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, on_stop)
        except (NotImplementedError, RuntimeError, ValueError):
            logger.debug("当前平台不支持 %s 的信号处理器", sig)
            return


async def serve(
    config: StudioConfig | None = None,
    stop_event: asyncio.Event | None = None,
    service: StudioService | None = None,
) -> None:
    """启动 gRPC 服务并一直跑下去。

    停机顺序按 §13.5：

    1. 收到 ``SIGTERM`` → **立刻**把 readiness 置为 false（先于 drain，
       否则新请求会一直进来，drain 永远结束不了）；
    2. 停止接受新请求；
    3. 等在跑的执行结束，最多 ``drain_timeout_sec``；
    4. 超时未结束的 RPC 被取消——执行器那边会杀掉整个进程组（§6.1）；
    5. 退出。

    Args:
        config: 服务配置；不给就从环境变量读。
        stop_event: 外部的停机信号。给了就**只**听它的（不再装信号处理器）——
            测试用这个把停机路径走一遍，不必真的往进程发信号。
        service: 已建好的服务实例。给了就用它（测试要读它的 ``ready`` 状态，
            才能验证「先摘 readiness 再 drain」的顺序）。
    """
    import grpc

    from webagent.studio.v1 import studio_pb2_grpc

    from services.studio.reaper import sweep_stale_processes

    cfg = config or StudioConfig.from_env()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    # 启动巡检：清掉上次硬杀留下的孤儿进程与工作目录（§6.5）。
    sweep_stale_processes()

    svc = service if service is not None else StudioService(cfg)
    server = grpc.aio.server()
    studio_pb2_grpc.add_StudioServicer_to_server(svc, server)
    address = f"{cfg.host}:{cfg.port}"
    server.add_insecure_port(address)
    await server.start()
    logger.info(
        "studio 已启动：%s（版本 %s，运行时 %s，并发上限 %s，隔离档 %s）",
        address,
        cfg.version,
        "、".join(cfg.languages) or "（无）",
        cfg.max_concurrency,
        cfg.profile,
    )

    stopping = stop_event if stop_event is not None else asyncio.Event()
    if stop_event is None:
        loop = asyncio.get_running_loop()
        _install_signal_handlers(loop, stopping.set)

    termination = asyncio.create_task(server.wait_for_termination())
    waiter = asyncio.create_task(stopping.wait())
    try:
        await asyncio.wait(
            {termination, waiter}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        # 先摘 readiness，再 drain——顺序反了的话 drain 永远结束不了。
        svc.set_ready(False)
        # **不要取消 termination 这个 task**。实测踩过：grpc 的
        # ``wait_for_termination()`` 与 ``shutdown()`` 共用同一个内部等待对象，
        # 取消前者会让后面的 ``server.stop()`` 直接抛 CancelledError——停机
        # 路径整个断掉，容器被 SIGKILL 腰斩，在跑的执行全丢。
        await server.stop(grace=cfg.drain_timeout_sec)
        waiter.cancel()
        if not termination.done():
            await asyncio.wait({termination}, timeout=5)
        logger.info("studio 已关闭")
