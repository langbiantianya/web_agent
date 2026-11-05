"""MCP 连接管理（设计文档 §21.7）。

## 为什么每台服务器要一条专属后台任务

MCP 的会话是**长连接**：stdio 是一个活着的子进程，HTTP 是一条保持着的会话。
而 SDK 的会话对象（``ClientSession``）是 anyio 任务组里的上下文——**进入与退出
必须在同一个任务里**，不能在一个协程里 ``async with``、在另一个协程里用。

所以每台服务器的连接都跑在一个**专属后台任务**里，外面用一个 ``asyncio.Queue``
当命令通道。这样：

- 连接的生命周期由那个任务自己管（退出时连接自然收尾，子进程自然被回收）；
- 调用方不关心 anyio 的那些约束，只需要 ``await conn.call(...)``；
- 并发调用靠「每个请求起一个 task」实现——JSON-RPC 的 ``id`` 天然区分请求，
  所以同一个会话上的并发调用是安全的。

## 几条关键口径

| 关注点 | 口径 |
| --- | --- |
| 什么时候连 | **惰性**：第一次用到该服务器的工具时才连。服务启动不因为某台 MCP 服务器挂了就失败 |
| 什么时候断 | 空闲超时（默认 5 分钟）后主动关闭——**stdio 子进程必须回收** |
| 断了怎么办 | 下次调用时重连，指数退避，最多 3 次；仍失败则该服务器标记为不可用 |
| 同一服务器的并发 | 允许，但有并发上限信号量（默认 4），避免把远端打爆 |
| 跨实例 | 连接是进程内的。stdio 子进程没法跨实例（物理限制） |

**为什么不预热**：预热会让启动变慢，且一台慢服务器拖慢整个启动。
**为什么不因为连不上就启动失败**：MCP 服务器是**可选依赖**，不是服务运行的前提。
一台 MCP 服务器挂掉，影响的是**配了它的那几个 Agent**，不是整个服务。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from app.config.settings import settings

logger = logging.getLogger(__name__)


class McpConnectionError(Exception):
    """连不上、调用失败、或服务器不可用。"""


class McpConnection:
    """一台 MCP 服务器的活连接。"""

    def __init__(self, server) -> None:
        """记住要连哪台服务器。

        Args:
            server: ORM ``McpServer`` 实例。
        """
        self.server = server
        self.code = server.code
        # 连上 / 连不上时的回调（由管理器注入，用来维护「可用性」状态）。
        self.on_unavailable: Callable[[str], None] = lambda _reason: None
        self.on_available: Callable[[], None] = lambda: None
        self._queue: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._error: Exception | None = None
        self._last_used = time.monotonic()
        self._inflight = 0
        self._lock = asyncio.Lock()
        # 并发上限：避免把远端打爆（§21.7）。
        self._semaphore = asyncio.Semaphore(settings.mcp_max_concurrency)

    # ------------------------------------------------------------- 生命周期

    async def ensure(self) -> None:
        """确保连接是活的；没有就起一条。"""
        async with self._lock:
            if self._task is None or self._task.done():
                self._start()

    def _start(self) -> None:
        """起一条后台任务跑这条连接。"""
        self._queue = asyncio.Queue()
        self._ready = asyncio.Event()
        self._error = None
        self._task = asyncio.create_task(self._run())
        logger.info("MCP 连接启动中：%s", self.code)

    async def _run(self) -> None:
        """后台任务本体：建立会话、循环处理请求、退出时收尾。"""
        from mcp import ClientSession

        from app.core.mcp.transport import open_transport

        try:
            async with open_transport(self.server) as streams:
                read_stream, write_stream = streams
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    self._ready.set()
                    self.on_available()
                    logger.info("MCP 连接就绪：%s", self.code)
                    await self._serve(session)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 连接失败是外部边界
            self._error = exc
            self._ready.set()
            logger.warning("MCP 连接失败：%s（%s）", self.code, exc)
            self._fail_pending(exc)
            # 连不上 → 这台服务器的工具不该再展开给模型（§21.9）。
            self.on_unavailable(str(exc))

    async def _serve(self, session) -> None:
        """处理请求队列。

        每个请求起一个 task——这样同一会话上的并发调用能真正并发（JSON-RPC 的
        ``id`` 天然区分请求），而不是排成一队。
        """
        pending: set[asyncio.Task] = set()
        assert self._queue is not None
        while True:
            request = await self._queue.get()
            if request is None:
                break
            task = asyncio.create_task(self._handle(session, request))
            pending.add(task)
            task.add_done_callback(pending.discard)
        # 关闭时把还在跑的请求等完。
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _handle(self, session, request: dict) -> None:
        """执行一个请求并回填结果。"""
        future: asyncio.Future = request["future"]
        try:
            async with self._semaphore:
                result = await self._dispatch(session, request)
            if not future.done():
                future.set_result(result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 外部边界，异常回灌给调用方
            if not future.done():
                future.set_exception(exc)

    async def _dispatch(self, session, request: dict) -> Any:
        """把请求翻译成 SDK 调用。"""
        method = request["method"]
        if method == "list_tools":
            return await session.list_tools(params=request.get("params"))
        if method == "call_tool":
            return await session.call_tool(
                request["tool"],
                request.get("args") or {},
                read_timeout_seconds=request.get("timeout"),
            )
        raise McpConnectionError(f"未知的 MCP 方法：{method}")

    def _fail_pending(self, exc: Exception) -> None:
        """连接断了，让还在排队的请求都失败——而不是永远挂着。"""
        if self._queue is None:
            return
        while not self._queue.empty():
            request = self._queue.get_nowait()
            if request is None:
                continue
            future = request.get("future")
            if future is not None and not future.done():
                future.set_exception(exc)

    async def close(self) -> None:
        """关掉这条连接（子进程随之被回收）。"""
        async with self._lock:
            task = self._task
            self._task = None
            if task is None:
                return
            if self._queue is not None:
                await self._queue.put(None)
            try:
                await asyncio.wait_for(task, timeout=10)
            except (TimeoutError, asyncio.CancelledError):
                task.cancel()
            logger.info("MCP 连接已关闭：%s", self.code)

    # ------------------------------------------------------------- 调用

    async def request(
        self, method: str, *, retries: int | None = None, **kwargs
    ) -> Any:
        """发一个请求，失败时按指数退避重连重试。

        Args:
            method: ``list_tools`` 或 ``call_tool``。
            retries: 重试次数；不给就用配置里的值。
            **kwargs: 按方法分发的参数。

        Returns:
            SDK 的返回对象。

        Raises:
            McpConnectionError: 重试用尽仍失败。
        """
        self._inflight += 1
        try:
            attempts = (settings.mcp_max_retries if retries is None else retries) + 1
            last: Exception | None = None
            for attempt in range(1, attempts + 1):
                try:
                    return await self._request_once(method, **kwargs)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    last = exc
                    # 连接本身断了才值得重试；参数错、工具不存在这类重试也没用。
                    if not self._is_connection_failure(exc):
                        raise McpConnectionError(str(exc)) from exc
                    logger.warning(
                        "MCP %s 调用失败（第 %s/%s 次）：%s",
                        self.code,
                        attempt,
                        attempts,
                        exc,
                    )
                    await self.close()
                    if attempt < attempts:
                        await asyncio.sleep(0.2 * (2 ** (attempt - 1)))
            raise McpConnectionError(
                f"MCP 服务器「{self.code}」不可用：{last}"
            ) from last
        finally:
            self._inflight -= 1
            self._last_used = time.monotonic()

    async def _request_once(self, method: str, **kwargs) -> Any:
        """发一次请求。"""
        await self.ensure()
        await self._ready.wait()
        if self._error is not None:
            raise self._error
        if self._queue is None:  # pragma: no cover - ensure 之后不该为空
            raise McpConnectionError(f"MCP 服务器「{self.code}」没有可用连接")
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        await self._queue.put({"method": method, "future": future, **kwargs})
        self._last_used = time.monotonic()
        return await future

    @staticmethod
    def _is_connection_failure(exc: Exception) -> bool:
        """这个异常值得重试吗？

        连接断了（子进程崩了、HTTP 断了）值得重试；「工具不存在」「参数非法」
        重试多少次都一样。
        """
        from mcp import MCPError

        if isinstance(exc, McpConnectionError):
            return True
        # SDK 的连接层异常（传输断了、EOF、超时）都归到 McpError 一类，
        # 但「工具不存在」也是 McpError——靠消息里的关键词区分。
        text = str(exc).lower()
        if isinstance(exc, MCPError):
            return not any(
                kw in text
                for kw in ("unknown tool", "tool not found", "invalid params", "validation")
            )
        return isinstance(exc, (OSError, TimeoutError, ConnectionError))


class McpClientManager:
    """按 ``server_code`` 管理 MCP 连接的进程内单例。"""

    def __init__(self) -> None:
        """初始化连接表。"""
        self._connections: dict[str, McpConnection] = {}
        self._lock = asyncio.Lock()
        self._sweeper: asyncio.Task | None = None
        # 已知不可用的服务器：code → 原因。**只有连失败过才会被记上**——
        # 「还没连过」不等于「不可用」（§21.9）。
        self._unavailable: dict[str, str] = {}

    def is_available(self, server_code: str) -> bool:
        """这台服务器现在算不算「可用」。

        判定口径是「**没被证明不可用**」而不是「已连接」——连接是惰性的
        （§21.7），一台注册好但还没被用过的服务器并没有连接，可它的工具
        是该给模型看的（不然就成了「模型看不见 → 调不到 → 永远连不上」的死结）。

        所以这里记的是**失败**：连不上就标不可用，连上了就清掉。

        Args:
            server_code: 服务器 code。

        Returns:
            可用返回 True。
        """
        return server_code not in self._unavailable

    def unavailable_reason(self, server_code: str) -> str | None:
        """这台服务器为什么被判为不可用。"""
        return self._unavailable.get(server_code)

    def mark_unavailable(self, server_code: str, reason: str) -> None:
        """标记一台服务器不可用。"""
        self._unavailable[server_code] = reason

    def clear_unavailable(self, server_code: str) -> None:
        """清掉不可用标记（连上了）。"""
        self._unavailable.pop(server_code, None)

    async def _get(self, server) -> McpConnection:
        """取（或建）一台服务器的连接。"""
        async with self._lock:
            conn = self._connections.get(server.code)
            if conn is None:
                conn = McpConnection(server)
                conn.on_unavailable = lambda reason, code=server.code: self.mark_unavailable(
                    code, reason
                )
                conn.on_available = lambda code=server.code: self.clear_unavailable(code)
                self._connections[server.code] = conn
            return conn

    async def call_tool(
        self, server, tool_name: str, args: dict, timeout: float | None = None
    ) -> Any:
        """在某个 MCP 服务器上调一个工具。

        Args:
            server: ORM ``McpServer`` 实例。
            tool_name: 服务器自己的工具名。
            args: 工具参数。
            timeout: 单次调用超时（秒）。

        Returns:
            SDK 的 ``CallToolResult``。
        """
        conn = await self._get(server)
        return await conn.request(
            "call_tool", tool=tool_name, args=args, timeout=timeout
        )

    async def list_tools(self, server) -> list[Any]:
        """列出某台服务器的全部工具（**翻完分页**）。

        ``tools/list`` 有分页（``nextCursor``），只取第一页会漏工具——漏掉的工具
        就永远进不了快照，也就永远不能被 Agent 绑定。

        Args:
            server: ORM ``McpServer`` 实例。

        Returns:
            工具对象列表。
        """
        from mcp.types import PaginatedRequestParams

        conn = await self._get(server)
        tools: list[Any] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            params = PaginatedRequestParams(cursor=cursor) if cursor else None
            result = await conn.request("list_tools", params=params)
            tools.extend(list(getattr(result, "tools", None) or []))
            cursor = getattr(result, "nextCursor", None)
            if not cursor:
                break
            if cursor in seen_cursors:
                # 服务器给了一个循环的 cursor——不防住就是死循环。
                logger.warning("MCP %s 的 nextCursor 循环了，停止翻页", server.code)
                break
            seen_cursors.add(cursor)
        return tools

    async def close_all(self) -> None:
        """关掉全部连接（进程退出 / 测试收尾时用）。"""
        async with self._lock:
            conns = list(self._connections.values())
            self._connections.clear()
        for conn in conns:
            try:
                await conn.close()
            except Exception:  # pragma: no cover - 收尾不该抛
                logger.warning("关闭 MCP 连接 %s 失败", conn.code, exc_info=True)

    async def close_idle(self) -> int:
        """关掉空闲超时的连接，返回关掉的数量。

        stdio 子进程必须回收——不回收就会攒出一堆僵尸进程（§21.7）。
        """
        timeout = settings.mcp_idle_timeout_sec
        now = time.monotonic()
        stale: list[McpConnection] = []
        async with self._lock:
            for code, conn in list(self._connections.items()):
                if (
                    conn._inflight == 0  # noqa: SLF001
                    and now - conn._last_used > timeout  # noqa: SLF001
                ):
                    stale.append(conn)
                    self._connections.pop(code, None)
        for conn in stale:
            try:
                await conn.close()
            except Exception:  # pragma: no cover
                logger.warning("关闭空闲 MCP 连接 %s 失败", conn.code, exc_info=True)
        return len(stale)

    def start_idle_reaper(self) -> None:
        """启动周期回收任务；重复调用不会创建第二条任务。"""
        if self._sweeper is not None and not self._sweeper.done():
            return
        interval = max(0.1, settings.mcp_idle_sweep_interval_sec)
        self._sweeper = asyncio.create_task(self._idle_reaper_loop(interval))
        logger.info("MCP 空闲连接巡检已启动，间隔 %.1f 秒", interval)

    async def _idle_reaper_loop(self, interval: float) -> None:
        """周期调用 :meth:`close_idle`；单次失败不终止巡检。"""
        while True:
            await asyncio.sleep(interval)
            try:
                closed = await self.close_idle()
                if closed:
                    logger.info("已回收 %s 条空闲 MCP 连接", closed)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("MCP 空闲连接巡检失败（下一周期继续）")

    async def stop_idle_reaper(self) -> None:
        """停止周期回收任务。"""
        task = self._sweeper
        self._sweeper = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def shutdown(self) -> None:
        """停止巡检并关闭全部连接，供应用生命周期调用。"""
        await self.stop_idle_reaper()
        await self.close_all()

    def is_connected(self, server_code: str) -> bool:
        """这台服务器当前有没有活连接。

        用于「离线服务器的工具不展开给模型」（§21.9）——注意这只是**近似**：
        连接可能刚好断了还没被重连逻辑发现。
        """
        conn = self._connections.get(server_code)
        if conn is None:
            return False
        return conn._task is not None and not conn._task.done()  # noqa: SLF001


mcp_manager = McpClientManager()
