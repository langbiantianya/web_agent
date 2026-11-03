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
from services.studio.exec_log import emit_exec_log
from services.studio.executor import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_LIMITED,
    STATUS_TIMEOUT,
    ExecOutcome,
    ScriptRunner,
)
from services.studio.launcher import (
    ExecSpec,
    InProcessLauncher,
    IsolatedLauncher,
    IsolatedRuntime,
    LauncherUnavailableError,
)
from services.studio.metrics import StudioMetrics, build_metrics

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

# 档位名（§2.4）。
PROFILE_FAST = "fast"
PROFILE_ISOLATED = "isolated"

# 请求非法 / 运行时不支持——``studio_exec_failed_total`` 的 reason 取值
# （skill-service §12.1）。执行器那边的三个在 :mod:`services.studio.executor`。
FAILURE_INVALID_REQUEST = "invalid_request"
FAILURE_UNKNOWN_LANGUAGE = "unknown_language"
FAILURE_ISOLATED_UNAVAILABLE = "isolated_unavailable"


class QueueFullError(Exception):
    """等待队列满了（§8.1）。"""


class _ExecutionPool:
    """一个执行池：并发闸门 + 等待队列（§8.1）。

    ``fast`` 与 ``isolated`` **各有一个**——§8.4：「isolated 档必须单独排队，
    不能和 fast 档抢同一个并发池，否则几个慢脚本能把快脚本全堵死」。
    """

    def __init__(self, max_concurrency: int, max_queue: int) -> None:
        """按限额建池。

        Args:
            max_concurrency: 同时在跑的脚本数上限。
            max_queue: 等待队列深度；满了就拒绝。
        """
        self.max_queue = max_queue
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._queue_lock = asyncio.Lock()
        self._waiting = 0

    async def acquire(self) -> None:
        """拿并发位；队列满了就拒绝。

        Raises:
            QueueFullError: 等待队列已满。
        """
        counted = False
        if self._semaphore.locked():
            async with self._queue_lock:
                if self._waiting >= self.max_queue:
                    raise QueueFullError()
                self._waiting += 1
                counted = True
        try:
            await self._semaphore.acquire()
        finally:
            if counted:
                async with self._queue_lock:
                    self._waiting -= 1

    def release(self) -> None:
        """把并发位还回去。"""
        self._semaphore.release()


class StudioService(studio_pb2_grpc.StudioServicer):
    """studio 的 gRPC servicer。"""

    def __init__(
        self,
        config: StudioConfig,
        isolated_runtime: IsolatedRuntime | None = None,
    ) -> None:
        """按配置建好闸门与执行器。

        Args:
            config: 服务配置。
            isolated_runtime: ``isolated`` 档的一次性容器执行器；没部署时给
                ``None``——那时走 isolated 档的技能会拿到明确的
                ``FAILED_PRECONDITION``，不会悄悄退回 ``fast`` 档。
        """
        self.config = config
        # 两个档位（§2.4）。isolated 档的容器运行时**默认没有部署**——它是一份
        # 骨架：接口、独立队列都在，缺运行时时明确报错，不悄悄退回 fast。
        self._fast_launcher = InProcessLauncher(ScriptRunner(config))
        self._isolated_launcher = IsolatedLauncher(isolated_runtime)
        # 两个池各管各的（§8.4：isolated 必须单独排队）。
        self._fast_pool = _ExecutionPool(
            config.max_concurrency, config.max_queue
        )
        self._isolated_pool = _ExecutionPool(
            config.isolated_max_concurrency, config.isolated_max_queue
        )
        # request_id → (落库时刻, 响应)（§5.2 的去重窗口）。
        self._dedup: dict[str, tuple[float, studio_pb2.ExecResponse]] = {}
        self._ready = True
        # 指标（§12.1）。**不记脚本输出内容**，只记字节数与摘要（§12.2）。
        self.metrics: StudioMetrics = build_metrics()

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
            self.metrics.failed_total.inc(reason=FAILURE_INVALID_REQUEST)
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, problem)

        if request.language not in _SUPPORTED_LANGUAGES:
            self.metrics.failed_total.inc(reason=FAILURE_UNKNOWN_LANGUAGE)
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                f"本实例不支持 {request.language} 运行时"
                f"（支持：{'、'.join(self.config.languages)}）",
            )

        cached = self._lookup_dedup(request.request_id)
        if cached is not None:
            logger.info("命中去重窗口，直接返回上次的结果：%s", request.request_id)
            return cached

        profile = self._profile_for(request.skill)
        launcher = (
            self._isolated_launcher
            if profile == PROFILE_ISOLATED
            else self._fast_launcher
        )
        pool = (
            self._isolated_pool
            if profile == PROFILE_ISOLATED
            else self._fast_pool
        )

        # 档位的运行时没部署 → 明确拒绝（**不悄悄退回 fast 档**）。
        if not launcher.available:
            self.metrics.failed_total.inc(reason=FAILURE_ISOLATED_UNAVAILABLE)
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                f"技能「{request.skill}」走 {PROFILE_ISOLATED} 档"
                "（每次执行起一次性容器），但本实例没有部署容器运行时，"
                "无法执行。",
            )

        queue_started = time.monotonic()
        try:
            await pool.acquire()
        except QueueFullError:
            await context.abort(
                grpc.StatusCode.RESOURCE_EXHAUSTED,
                f"等待队列已满（{pool.max_queue}），请退避重试",
            )
        # 排队时长：从「发出」算到「拿到并发位」（§8.1）。
        self.metrics.queue_wait.observe(time.monotonic() - queue_started)
        self.metrics.exec_active.inc()

        started = time.monotonic()
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
                outcome = await launcher.run(
                    ExecSpec(
                        skill=request.skill,
                        script=request.script,
                        language=request.language,
                        content=request.content,
                        argv=tuple(request.argv),
                        stdin=request.stdin,
                        timeout_ms=remaining_ms,
                    )
                )
        except LauncherUnavailableError as exc:
            # 档位不可用：转成 FAILED_PRECONDITION（§10.2 同一类问题）。
            self.metrics.failed_total.inc(reason=FAILURE_ISOLATED_UNAVAILABLE)
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
        except asyncio.CancelledError:
            # 调用方取消（§8.3）。gRPC 的取消不传播到对端的进程组，所以 studio
            # 侧的自设时限是**兜底**，不是可选项。
            raise
        finally:
            pool.release()
            self.metrics.exec_active.dec()

        self._record_metrics(request, outcome, time.monotonic() - started, profile)
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

    def _profile_for(self, skill: str) -> str:
        """这个技能走哪个档位（§2.4）。

        档位由**服务侧配置**决定，不放在请求里——所以 proto 不必加字段、调用方
        也不用关心（§2.4）。规则两条：

        1. 实例默认档是 ``isolated`` → 全部技能都走 isolated；
        2. 否则，命中 ``isolated_skill_codes`` 的技能走 isolated，其余走 fast。

        Args:
            skill: 技能 code。

        Returns:
            ``fast`` 或 ``isolated``。
        """
        if self.config.profile == PROFILE_ISOLATED:
            return PROFILE_ISOLATED
        if skill in self.config.isolated_skill_codes:
            return PROFILE_ISOLATED
        return PROFILE_FAST

    def _record_metrics(
        self, request, outcome: ExecOutcome, elapsed: float, profile: str
    ) -> None:
        """记这次执行的指标，并写一条结构化汇总日志（§12.1、§12.2）。

        日志里**只有字节数与摘要，没有输出内容**——脚本输出可能含敏感数据
        （主设计 §22.10 第 2 条）。

        Args:
            request: ``ExecRequest``。
            outcome: 执行结果。
            elapsed: 服务级端到端耗时（秒，含排队）。
            profile: 这次执行实际走的档位。
        """
        language = request.language
        self.metrics.exec_total.inc(
            status=outcome.status,
            language=language,
            profile=profile,
        )
        self.metrics.exec_duration.observe(elapsed, language=language)
        if outcome.status == STATUS_TIMEOUT:
            self.metrics.timeout_total.inc(language=language)
        if outcome.truncated:
            self.metrics.output_truncated_total.inc(language=language)
        if outcome.failure_reason:
            self.metrics.failed_total.inc(reason=outcome.failure_reason)
        emit_exec_log(
            request_id=request.request_id,
            run_id=request.run_id,
            node_id=request.node_id,
            skill=request.skill,
            script=request.script,
            language=language,
            status=outcome.status,
            exit_code=outcome.exit_code,
            duration_ms=outcome.duration_ms,
            stdout_bytes=outcome.stdout_bytes,
            stderr_bytes=outcome.stderr_bytes,
            truncated=outcome.truncated,
            stdout_digest=outcome.stdout_digest,
        )

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
