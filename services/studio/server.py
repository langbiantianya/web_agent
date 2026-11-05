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

# 档位名（§2.4）。请求用 proto 枚举，响应回显实际档位。
PROFILE_FAST = "fast"
PROFILE_ISOLATED = "isolated"

_PROFILE_TO_PROTO = {
    PROFILE_FAST: studio_pb2.EXEC_PROFILE_FAST,
    PROFILE_ISOLATED: studio_pb2.EXEC_PROFILE_ISOLATED,
}

_PROTO_TO_PROFILE = {
    studio_pb2.EXEC_PROFILE_FAST: PROFILE_FAST,
    studio_pb2.EXEC_PROFILE_ISOLATED: PROFILE_ISOLATED,
}

# 请求非法 / 运行时不支持——``studio_exec_failed_total`` 的 reason 取值
# （skill-service §12.1）。执行器那边的三个在 :mod:`services.studio.executor`。
FAILURE_INVALID_REQUEST = "invalid_request"
FAILURE_UNKNOWN_LANGUAGE = "unknown_language"
FAILURE_ISOLATED_UNAVAILABLE = "isolated_unavailable"
FAILURE_CAPABILITY_MISSING = "capability_missing"
FAILURE_IMAGE_UNKNOWN = "image_unknown"


def _requested_profile(value: int) -> str:
    """proto 枚举 → 我们的档位名；``UNSPECIFIED`` 返回空串（沿用服务端路由）。"""
    return _PROTO_TO_PROFILE.get(int(value), "")


def _default_error_code(status: str) -> str:
    """按终态给一个机器可读的默认错误码（执行器没给时用）。"""
    return {
        STATUS_FAILED: "failed",
        STATUS_TIMEOUT: "timeout",
        STATUS_LIMITED: "limited",
        STATUS_CANCELLED: "cancelled",
    }.get(status, "")


def _image_digest(ref: str) -> str:
    """从 ``image@sha256:...`` 里取出 digest（没有就返回空串）。"""
    _, sep, digest = ref.partition("@")
    return digest if sep else ""


def _timeout_outcome(error: str) -> ExecOutcome:
    """造一个「排队就耗尽了时限」的结果。"""
    return ExecOutcome(
        status=STATUS_TIMEOUT,
        exit_code=-1,
        stdout="",
        stderr="",
        truncated=False,
        duration_ms=0,
        error=error,
        stdout_digest="",
        error_code="timeout",
    )


def _reservation_cost(
    config: StudioConfig, *, profile: str, timeout_ms: int
) -> float:
    """按执行上限估算要预占的共享成本单位。

    预占是上界，不是最终账单；真实成本由 cgroup 数据写入指标和日志。即使没有
    配置费率，也至少预占 1 个单位，让租户/技能预算仍能限制并发。
    """
    seconds = max(0.001, timeout_ms / 1000.0)
    if profile == PROFILE_ISOLATED:
        cpu = seconds * 1.0
        memory_gib = (512 * 1024 * 1024) / (1024 ** 3)
        value = (
            cpu * config.cpu_second_rate
            + memory_gib * seconds * config.memory_gib_second_rate
            + config.sandbox_start_rate
        )
        return max(1.0, value)
    return max(1.0, seconds * config.cpu_second_rate)


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

    @property
    def waiting(self) -> int:
        """当前在队列里等并发位的请求数（只读，供 Health 上报）。"""
        return self._waiting


class StudioService(studio_pb2_grpc.StudioServicer):
    """studio 的 gRPC servicer。"""

    def __init__(
        self,
        config: StudioConfig,
        isolated_runtime: IsolatedRuntime | None = None,
        admission_runtime: IsolatedRuntime | None = None,
    ) -> None:
        """按配置建好闸门与执行器。

        Args:
            config: 服务配置。
            isolated_runtime: ``isolated`` 档的一次性容器执行器；没部署时给
                ``None``——那时走 isolated 档的技能会拿到明确的
                ``FAILED_PRECONDITION``，不会悄悄退回 ``fast`` 档。
            admission_runtime: 跨实例配额控制面。通常与 isolated runtime 是
                同一个 dispatcher 客户端，也可用于 fast 档准入。
        """
        self.config = config
        # 两个档位（§2.4）。isolated 档的容器运行时**默认没有部署**——它是一份
        # 骨架：接口、独立队列都在，缺运行时时明确报错，不悄悄退回 fast。
        self._fast_launcher = InProcessLauncher(ScriptRunner(config))
        self._isolated_launcher = IsolatedLauncher(isolated_runtime)
        self._admission_runtime = admission_runtime or isolated_runtime
        # 两个池各管各的（§8.4：isolated 必须单独排队）。
        self._fast_pool = _ExecutionPool(
            config.max_concurrency, config.max_queue
        )
        self._isolated_pool = _ExecutionPool(
            config.isolated_max_concurrency, config.isolated_max_queue
        )
        self._skill_pools = {
            code: _ExecutionPool(limit, config.skill_queue)
            for code, limit in config.skill_quotas
        }
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

        # 语言校验必须对着**本实例配置的**运行时清单，而不是一个常量：镜像里
        # 没装 node 时，请求应该在配置期/入口就被拒，而不是「启动失败」。
        if request.language not in self.config.languages:
            self.metrics.failed_total.inc(reason=FAILURE_UNKNOWN_LANGUAGE)
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                f"unknown_language: 本实例不支持 {request.language} 运行时"
                f"（支持：{'、'.join(self.config.languages)}）",
            )

        # 请求档位 → 实际档位。规则是**只能升、不能降**：请求 isolated 就绝不
        # 改走 fast；运维列表命中时可以把 fast 升级成 isolated。
        requested = _requested_profile(request.profile)
        profile = self._resolve_profile(request.skill, requested)

        # 镜像名必须在目录里。目录是服务端配置，调用方只能按名字请求——永远
        # 不能自己指定完整 ref。
        if profile == PROFILE_ISOLATED and request.image:
            if self.config.image_ref(request.image) is None:
                self.metrics.failed_total.inc(reason=FAILURE_IMAGE_UNKNOWN)
                await context.abort(
                    grpc.StatusCode.FAILED_PRECONDITION,
                    f"image_unknown: 本实例没有登记镜像「{request.image}」",
                )

        cached = self._lookup_dedup(request.request_id)
        if cached is not None:
            logger.info("命中去重窗口，直接返回上次的结果：%s", request.request_id)
            return cached

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
                f"capability_missing: 技能「{request.skill}」走 {PROFILE_ISOLATED} 档"
                "（每次执行起一次性容器），但本实例没有部署容器运行时，"
                "无法执行。",
            )

        # 排队时间计入时限（§8.1）：排队本身也要有上限，否则一个超过时限的
        # 请求还会占着队列位，最后只换来一个「排队耗尽」的 TIMEOUT。
        queue_started = time.monotonic()
        wait_budget = deadline - queue_started
        outcome: ExecOutcome | None = None
        if wait_budget <= 0:
            outcome = _timeout_outcome("排队时间已耗尽执行时限")
        else:
            try:
                await asyncio.wait_for(pool.acquire(), timeout=wait_budget)
            except asyncio.TimeoutError:
                outcome = _timeout_outcome(
                    f"排队时间超过执行时限（{request.timeout_ms} 毫秒）"
                )
            except QueueFullError:
                await context.abort(
                    grpc.StatusCode.RESOURCE_EXHAUSTED,
                    f"sandbox_capacity: 等待队列已满（{pool.max_queue}），请退避重试",
                )
        self.metrics.queue_wait.observe(
            time.monotonic() - queue_started, profile=profile
        )

        if outcome is not None:
            # 没拿到并发位：不 release，也不占 exec_active。
            self._record_metrics(
                request, outcome, time.monotonic() - queue_started, profile
            )
            response = self._to_response(outcome, profile)
            if outcome.status in (STATUS_COMPLETED, STATUS_TIMEOUT):
                self._store_dedup(request.request_id, response)
            return response

        skill_pool = self._skill_pools.get(request.skill)
        if skill_pool is not None:
            skill_wait_budget = deadline - time.monotonic()
            if skill_wait_budget <= 0:
                outcome = _timeout_outcome("技能配额等待耗尽执行时限")
            else:
                try:
                    await asyncio.wait_for(
                        skill_pool.acquire(), timeout=skill_wait_budget
                    )
                except asyncio.TimeoutError:
                    outcome = _timeout_outcome("技能配额等待超过执行时限")
                except QueueFullError:
                    pool.release()
                    await context.abort(
                        grpc.StatusCode.RESOURCE_EXHAUSTED,
                        f"skill_quota: 技能「{request.skill}」等待队列已满"
                        f"（{skill_pool.max_queue}），请退避重试",
                    )
            if outcome is not None:
                pool.release()
                self._record_metrics(
                    request,
                    outcome,
                    time.monotonic() - queue_started,
                    profile,
                )
                return self._to_response(outcome, profile)

        self.metrics.exec_active.inc()
        if profile == PROFILE_ISOLATED:
            self.metrics.sandbox_total.inc(
                phase="started", profile=profile
            )
        started = time.monotonic()
        quota_lease_id = ""
        try:
            acquire_quota = getattr(self._admission_runtime, "acquire_quota", None)
            if acquire_quota is not None:
                remaining_for_reservation = max(
                    1, int(max(0.0, deadline - time.monotonic()) * 1000)
                )
                try:
                    quota_lease_id = await acquire_quota(
                        tenant_id=(
                            getattr(request, "tenant_id", "default") or "default"
                        ),
                        skill=request.skill,
                        cost=_reservation_cost(
                            self.config,
                            profile=profile,
                            timeout_ms=remaining_for_reservation,
                        ),
                        lease_ms=max(
                            60_000,
                            (request.timeout_ms or self.config.max_timeout_ms)
                            + self.config.isolated_startup_budget_ms
                            + 60_000,
                        ),
                    )
                except Exception as exc:
                    self.metrics.failed_total.inc(reason="quota_exceeded")
                    await context.abort(
                        grpc.StatusCode.RESOURCE_EXHAUSTED,
                        f"quota_exceeded: {exc}",
                    )
            remaining_ms = int(max(0.0, deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                outcome = _timeout_outcome("排队时间已耗尽执行时限")
            else:
                outcome = await launcher.run(
                    ExecSpec(
                        skill=request.skill,
                        tenant_id=getattr(request, "tenant_id", "default")
                        or "default",
                        script=request.script,
                        language=request.language,
                        content=request.content,
                        argv=tuple(request.argv),
                        stdin=request.stdin,
                        timeout_ms=remaining_ms,
                        image=request.image,
                        request_id=request.request_id,
                        traceparent=request.traceparent,
                    )
                )
        except LauncherUnavailableError as exc:
            # 档位不可用：转成 FAILED_PRECONDITION（§10.2 同一类问题）。
            self.metrics.failed_total.inc(reason=FAILURE_CAPABILITY_MISSING)
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                f"capability_missing: {exc}",
            )
        except asyncio.CancelledError:
            # gRPC 取消必须显式传播到 isolated dispatcher；容器不会被 Python
            # 协程取消自动回收。等待取消回执有界，避免停机时再次挂住。
            try:
                await asyncio.wait_for(
                    launcher.cancel(request.request_id), timeout=5.0
                )
            except Exception:
                logger.warning(
                    "取消沙箱失败：request_id=%s", request.request_id,
                    exc_info=True,
                )
            raise
        finally:
            release_quota = getattr(self._admission_runtime, "release_quota", None)
            if quota_lease_id and release_quota is not None:
                try:
                    await release_quota(quota_lease_id)
                except Exception:
                    logger.warning(
                        "释放跨实例配额失败：%s", quota_lease_id, exc_info=True
                    )
            if skill_pool is not None:
                skill_pool.release()
            pool.release()
            self.metrics.exec_active.dec()

        self._record_metrics(request, outcome, time.monotonic() - started, profile)
        response = self._to_response(outcome, profile)
        # 只缓存「有结论」的结果：瞬时故障（调度失败、拉镜像失败）不该被缓存
        # 60 秒，否则重试会被静默吞掉。
        if outcome.status in (STATUS_COMPLETED, STATUS_TIMEOUT):
            self._store_dedup(request.request_id, response)
        logger.info(
            "执行完成：skill=%s script=%s profile=%s status=%s exit=%s 用时 %sms",
            request.skill,
            request.script,
            profile,
            outcome.status,
            outcome.exit_code,
            outcome.duration_ms,
        )
        return response

    # ------------------------------------------------------------- Health

    async def Health(self, request, context):  # noqa: N802 - gRPC 的命名
        """探活。

        必须**便宜**：不跑脚本、不读文件（§13.3 的探针要求）。

        除了原来的单值字段，这里按**档位**报能力：调用方要在启动期/配置期
        判断「这个实例能不能跑我的 isolated 技能」，单值 ``profile`` 不够用。

        Args:
            request: ``HealthRequest``。
            context: gRPC 上下文。

        Returns:
            ``HealthResponse``。
        """
        fast_ready = self._ready
        isolated_ready = self._ready and self._isolated_launcher.available
        # 实例默认档不可用 = 整体不可用；非默认档不可用只降级那个档。
        default_ready = (
            isolated_ready if self.config.profile == PROFILE_ISOLATED else fast_ready
        )
        if not self._ready:
            isolated_detail = "正在停机"
        elif not self._isolated_launcher.available:
            isolated_detail = "未部署沙箱运行时（isolated 档不可用）"
        else:
            isolated_detail = ""
        return studio_pb2.HealthResponse(
            ready=default_ready,
            version=self.config.version,
            language_info=[
                studio_pb2.LanguageInfo(name=name) for name in self.config.languages
            ],
            max_concurrency=self.config.max_concurrency,
            profile=self.config.profile,
            profiles=[
                studio_pb2.ProfileInfo(
                    name=PROFILE_FAST,
                    ready=fast_ready,
                    max_concurrency=self.config.max_concurrency,
                    queue_depth=self._fast_pool.waiting,
                    detail="" if fast_ready else "正在停机",
                ),
                studio_pb2.ProfileInfo(
                    name=PROFILE_ISOLATED,
                    ready=isolated_ready,
                    max_concurrency=self.config.isolated_max_concurrency,
                    queue_depth=self._isolated_pool.waiting,
                    detail=isolated_detail,
                ),
            ],
            images=[
                studio_pb2.ImageInfo(
                    name=name,
                    # Phase 0 只有一份语言矩阵；Phase 1 起按镜像分别配置。
                    languages=list(self.config.languages),
                    digest=_image_digest(ref),
                )
                for name, ref in self.config.isolated_images
            ],
            isolated_startup_budget_ms=self.config.isolated_startup_budget_ms,
        )

    # ------------------------------------------------------------- 内部

    def _resolve_profile(self, skill: str, requested: str) -> str:
        """请求档位 → 实际档位（§2.4、§14.3）。

        规则是**只能升、不能降**：

        1. 请求 ``isolated`` → 一定走 isolated（能力不够就拒绝，绝不改走 fast）；
        2. 实例默认档是 ``isolated`` → 全部走 isolated；
        3. 否则命中 ``isolated_skill_codes`` 的技能**升级**为 isolated；
        4. 其余走 fast。

        Args:
            skill: 技能 code。
            requested: 请求档位名；空串表示老客户端没带。

        Returns:
            ``fast`` 或 ``isolated``。
        """
        if requested == PROFILE_ISOLATED:
            return PROFILE_ISOLATED
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
        self.metrics.exec_duration.observe(
            elapsed, language=language, profile=profile
        )
        if outcome.status == STATUS_TIMEOUT:
            self.metrics.timeout_total.inc(language=language)
        if outcome.truncated:
            self.metrics.output_truncated_total.inc(language=language)
        if outcome.failure_reason:
            self.metrics.failed_total.inc(reason=outcome.failure_reason)
        if profile == PROFILE_ISOLATED:
            self.metrics.sandbox_total.inc(
                phase=outcome.status, profile=profile
            )
            self.metrics.sandbox_start.observe(
                max(0.0, outcome.startup_ms / 1000.0),
                profile=profile,
                image=request.image or "default",
            )
            self.metrics.sandbox_duration.observe(
                max(0.0, outcome.duration_ms / 1000.0),
                image=request.image or "default",
                status=outcome.status,
            )
            image = request.image or "default"
            self.metrics.sandbox_cpu_seconds.observe(
                max(0.0, outcome.cpu_seconds), profile=profile, image=image
            )
            self.metrics.sandbox_peak_memory_bytes.observe(
                max(0, outcome.peak_memory_bytes),
                profile=profile,
                image=image,
            )
            memory_gib = max(0, outcome.peak_memory_bytes) / (1024 ** 3)
            duration_sec = max(0.0, outcome.duration_ms / 1000.0)
            startup_sec = max(0.0, outcome.image_startup_ms / 1000.0)
            estimated_cost = (
                max(0.0, outcome.cpu_seconds) * self.config.cpu_second_rate
                + memory_gib * duration_sec * self.config.memory_gib_second_rate
                + startup_sec * self.config.sandbox_start_rate
            )
            self.metrics.estimated_cost_total.inc(
                estimated_cost,
                tenant=getattr(request, "tenant_id", "default") or "default",
                skill=request.skill or "unknown",
                profile=profile,
            )
        else:
            estimated_cost = 0.0
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
            profile=profile,
            image=request.image,
            startup_ms=outcome.startup_ms,
            image_startup_ms=outcome.image_startup_ms,
            cpu_seconds=outcome.cpu_seconds,
            peak_memory_bytes=outcome.peak_memory_bytes,
            estimated_cost=estimated_cost,
            error_code=outcome.error_code or _default_error_code(outcome.status),
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
        if len(request.image) > 64:
            return "image 超过 64 字符上限"
        return None

    def _to_response(
        self, outcome: ExecOutcome, profile: str
    ) -> studio_pb2.ExecResponse:
        """把执行结果转成 proto 响应。

        Args:
            outcome: 执行结果。
            profile: **实际**使用的档位——必须回显，供调用方对账防降级。

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
            profile=_PROFILE_TO_PROTO.get(
                profile, studio_pb2.EXEC_PROFILE_UNSPECIFIED
            ),
            startup_ms=outcome.startup_ms,
            error_code=outcome.error_code or _default_error_code(outcome.status),
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
