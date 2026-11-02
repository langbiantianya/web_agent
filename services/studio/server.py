"""studio 的 gRPC 服务（skill-service §4、§5、§8）。

## 一条铁律：gRPC 成功 ≠ 脚本成功

脚本自己 ``exit 1`` 是 ``COMPLETED`` + ``exit_code=1``，**不是** gRPC 错误。
gRPC status 只用来表达 **studio 自己的问题**（参数非法、队列满、实例下线、
内部错误）。

理由在主设计 §17.5：工具执行失败要把错误**回灌给模型**，让模型有机会自己纠正。
调用方看到 ``exit_code=1`` + stderr，会把它当成「脚本跑完了但失败了」；如果
studio 用 gRPC 错误码报「脚本失败」，调用方就只能当成**服务不可用**处理——语义
完全错了，模型也拿不到 stderr 的细节。

## 排队时间计入时限

调用方给的 ``timeout_ms`` 是「**从发出算起**」的（§8.1）。调用方等不了「排在
第 3 位还要再等 20 秒」。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import grpc

from webagent.studio.v1 import studio_pb2, studio_pb2_grpc

from services.studio.config import StudioConfig
from services.studio.executor import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_LIMITED,
    STATUS_TIMEOUT,
    ExecOutcome,
    ScriptRunner,
)

logger = logging.getLogger(__name__)

# proto 枚举 ← 我们的状态名。
_STATUS_TO_PROTO = {
    STATUS_COMPLETED: studio_pb2.EXEC_STATUS_COMPLETED,
    STATUS_FAILED: studio_pb2.EXEC_STATUS_FAILED,
    STATUS_TIMEOUT: studio_pb2.EXEC_STATUS_TIMEOUT,
    STATUS_LIMITED: studio_pb2.EXEC_STATUS_LIMITED,
    STATUS_CANCELLED: studio_pb2.EXEC_STATUS_CANCELLED,
}

_SUPPORTED_LANGUAGES = ("python", "bash", "node")


class QueueFullError(Exception):
    """等待队列满了（§8.1）。"""


class StudioService(studio_pb2_grpc.StudioServicer):
    """studio 的 gRPC servicer。"""

    def __init__(self, config: StudioConfig) -> None:
        """按配置建好闸门与执行器。

        Args:
            config: 服务配置。
        """
        self.config = config
        self.runner = ScriptRunner(config)
        # 并发闸门（§8.1）：同时在跑的脚本数上限。
        self._semaphore = asyncio.Semaphore(config.max_concurrency)
        self._queue_lock = asyncio.Lock()
        self._waiting = 0
        # request_id → (落库时刻, 响应)（§5.2 的去重窗口）。
        self._dedup: dict[str, tuple[float, studio_pb2.ExecResponse]] = {}
        self._ready = True

    # ------------------------------------------------------------- 生命周期

    def set_ready(self, ready: bool) -> None:
        """设置就绪标志——优雅停机时先摘 readiness 再 drain（§13.5）。"""
        self._ready = ready

    @property
    def ready(self) -> bool:
        """当前是否就绪。

        优雅停机一开始就是 False——负载均衡据此把流量摘走（§13.5 第 1 步）。
        """
        return self._ready

    # --------------------------------------------------------------- Exec

    async def Exec(self, request, context):  # noqa: N802 - gRPC 的命名
        """执行一个脚本。

        Args:
            request: ``ExecRequest``。
            context: gRPC 上下文。

        Returns:
            ``ExecResponse``。
        """
        deadline = time.monotonic() + min(
            request.timeout_ms or self.config.max_timeout_ms,
            self.config.max_timeout_ms,
        ) / 1000.0

        problem = self._validate(request)
        if problem is not None:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, problem)

        if request.language not in _SUPPORTED_LANGUAGES:
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                f"本实例不支持 {request.language} 运行时"
                f"（支持：{'、'.join(self.config.languages)}）",
            )

        cached = self._lookup_dedup(request.request_id)
        if cached is not None:
            logger.info("命中去重窗口，直接返回上次的结果：%s", request.request_id)
            return cached

        try:
            await self._acquire_slot()
        except QueueFullError:
            await context.abort(
                grpc.StatusCode.RESOURCE_EXHAUSTED,
                f"等待队列已满（{self.config.max_queue}），请退避重试",
            )

        try:
            remaining_ms = int(max(0.0, deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                outcome = ExecOutcome(
                    status=STATUS_TIMEOUT,
                    exit_code=-1,
                    stdout="",
                    stderr="",
                    truncated=False,
                    duration_ms=0,
                    error="排队时间已耗尽执行时限",
                    stdout_digest="",
                )
            else:
                outcome = await self.runner.run(
                    language=request.language,
                    content=request.content,
                    argv=list(request.argv),
                    stdin=request.stdin,
                    timeout_ms=remaining_ms,
                )
        except asyncio.CancelledError:
            # 调用方取消（§8.3）。gRPC 的取消不传播到对端的进程组，所以 studio
            # 侧的自设时限是**兜底**，不是可选项。
            raise
        finally:
            self._semaphore.release()

        response = self._to_response(outcome)
        self._store_dedup(request.request_id, response)
        logger.info(
            "执行完成：skill=%s script=%s status=%s exit=%s 用时 %sms",
            request.skill,
            request.script,
            outcome.status,
            outcome.exit_code,
            outcome.duration_ms,
        )
        return response

    # ------------------------------------------------------------- Health

    async def Health(self, request, context):  # noqa: N802 - gRPC 的命名
        """探活。

        必须**便宜**：不跑脚本、不读文件（§13.3 的探针要求）。

        Args:
            request: ``HealthRequest``。
            context: gRPC 上下文。

        Returns:
            ``HealthResponse``。
        """
        return studio_pb2.HealthResponse(
            ready=self._ready,
            version=self.config.version,
            language_info=[
                studio_pb2.LanguageInfo(name=name) for name in self.config.languages
            ],
            max_concurrency=self.config.max_concurrency,
            profile=self.config.profile,
        )

    # ------------------------------------------------------------- 内部

    def _validate(self, request) -> str | None:
        """校验请求参数。

        Args:
            request: ``ExecRequest``。

        Returns:
            问题说明；没问题时返回 ``None``。
        """
        if not request.content.strip():
            return "content 不能为空"
        if len(request.content) > self.config.max_content_chars:
            return f"content 超过 {self.config.max_content_chars} 字符上限"
        if len(request.argv) > self.config.max_argv_items:
            return f"argv 超过 {self.config.max_argv_items} 项上限"
        for item in request.argv:
            if len(item) > self.config.max_argv_item_chars:
                return f"argv 单项超过 {self.config.max_argv_item_chars} 字符上限"
        if len(request.stdin) > self.config.max_stdin_chars:
            return f"stdin 超过 {self.config.max_stdin_chars} 字符上限"
        return None

    async def _acquire_slot(self) -> None:
        """拿并发位；队列满了就拒绝（§8.1）。

        Raises:
            QueueFullError: 等待队列已满。
        """
        counted = False
        if self._semaphore.locked():
            async with self._queue_lock:
                if self._waiting >= self.config.max_queue:
                    raise QueueFullError()
                self._waiting += 1
                counted = True
        try:
            await self._semaphore.acquire()
        finally:
            if counted:
                async with self._queue_lock:
                    self._waiting -= 1

    def _to_response(self, outcome: ExecOutcome) -> studio_pb2.ExecResponse:
        """把执行结果转成 proto 响应。

        Args:
            outcome: 执行结果。

        Returns:
            ``ExecResponse``。
        """
        return studio_pb2.ExecResponse(
            status=_STATUS_TO_PROTO.get(outcome.status, studio_pb2.EXEC_STATUS_FAILED),
            exit_code=outcome.exit_code,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            truncated=outcome.truncated,
            duration_ms=outcome.duration_ms,
            error=outcome.error,
            stdout_digest=outcome.stdout_digest,
        )

    def _lookup_dedup(self, request_id: str) -> studio_pb2.ExecResponse | None:
        """查去重窗口（§5.2）。

        Args:
            request_id: 追踪键。

        Returns:
            窗口内的上次结果；没有则 ``None``。
        """
        if not request_id:
            return None
        self._purge_dedup()
        entry = self._dedup.get(request_id)
        return entry[1] if entry else None

    def _store_dedup(self, request_id: str, response: Any) -> None:
        """把结果存进去重窗口。

        Args:
            request_id: 追踪键。
            response: ``ExecResponse``。
        """
        if not request_id:
            return
        self._purge_dedup()
        self._dedup[request_id] = (time.monotonic(), response)

    def _purge_dedup(self) -> None:
        """清掉过期的去重条目。"""
        now = time.monotonic()
        expired = [
            key
            for key, (stamp, _) in self._dedup.items()
            if now - stamp > self.config.dedup_window_sec
        ]
        for key in expired:
            self._dedup.pop(key, None)
