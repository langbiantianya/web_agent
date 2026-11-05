"""``isolated`` 隔离档的骨架（skill-service §2.4、§8.4）。

## 这里钉住的命题

1. **路由按服务侧配置**（§2.4「档位由服务侧配置决定（按 skill code 列表）」），
   请求里**没有**档位字段，proto 契约不变。
2. **运行时没部署时要明确报错**：``FAILED_PRECONDITION`` + 「没有部署容器运行时」，
   **绝不悄悄退回 ``fast`` 档**——悄悄退回是最坏的降级：调用方以为拿到的是强
   隔离，实际不是，而且没有任何痕迹。
3. **两个池各管各的**（§8.4「``isolated`` 档必须单独排队，不能和 fast 档抢同一个
   并发池——否则几个慢脚本能把快脚本全堵死」）。这条要用行为证明：isolated 档
   被占满时，fast 档照常跑。
4. **profile 标签要有用**：真正走 isolated 档跑完的那一次，
   ``studio_exec_total`` 的 ``profile`` 标签是 ``isolated``。
"""

from __future__ import annotations

import asyncio
import socket
import time

import grpc
import pytest

from services.studio.config import StudioConfig
from services.studio.executor import ExecOutcome
from services.studio.launcher import ExecSpec, IsolatedLauncher
from services.studio.server import StudioService
from webagent.studio.v1 import studio_pb2, studio_pb2_grpc


class _FakeIsolatedRuntime:
    """假的「一次性容器执行器」——用来证明接缝与队列隔离是真的。

    它**不是**容器：只是记住被调用了几次、跑了多久、并发峰值多少。
    """

    def __init__(
        self,
        delay: float = 0.0,
        status: str = "completed",
        *,
        cpu_seconds: float = 0.0,
        peak_memory_bytes: int = 0,
        image_startup_ms: int = 0,
    ) -> None:
        """记下模拟的耗时与终态。

        Args:
            delay: 每次执行模拟的耗时（秒）。
            status: 终态；``failed`` 用来验证失败结果不进去重缓存。
        """
        self.delay = delay
        self.status = status
        self.cpu_seconds = cpu_seconds
        self.peak_memory_bytes = peak_memory_bytes
        self.image_startup_ms = image_startup_ms
        self.calls: list[ExecSpec] = []
        self.max_active = 0
        self._active = 0

    async def run(self, spec: ExecSpec) -> ExecOutcome:
        """模拟跑一次。

        Args:
            spec: 执行输入。

        Returns:
            假的执行结果。
        """
        self.calls.append(spec)
        self._active += 1
        self.max_active = max(self.max_active, self._active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            failed = self.status != "completed"
            return ExecOutcome(
                status=self.status,
                exit_code=-1 if failed else 0,
                stdout="" if failed else "isolated ok",
                stderr="",
                truncated=False,
                duration_ms=1,
                error="boom" if failed else "",
                stdout_digest="deadbeef",
                error_code="failed" if failed else "",
                cpu_seconds=self.cpu_seconds,
                peak_memory_bytes=self.peak_memory_bytes,
                image_startup_ms=self.image_startup_ms,
            )
        finally:
            self._active -= 1

    async def close(self) -> None:
        """没有要收的尾。"""
        return None


class _FakeAdmission:
    """跨实例配额控制面的测试替身。"""

    def __init__(self, deny: bool = False) -> None:
        self.deny = deny
        self.acquired: list[dict] = []
        self.released: list[str] = []

    async def acquire_quota(self, **kwargs) -> str:
        self.acquired.append(kwargs)
        if self.deny:
            raise RuntimeError("tenant_budget 已满")
        return "lease-1"

    async def release_quota(self, lease_id: str) -> bool:
        self.released.append(lease_id)
        return True

    async def close(self) -> None:
        return None


def _free_port() -> int:
    """向 OS 借一个空闲端口。

    Returns:
        端口号。
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _config(**overrides) -> StudioConfig:
    """造一份测试配置。

    Args:
        **overrides: 覆盖默认字段。

    Returns:
        配置实例。
    """
    params = {
        "languages": ("python",),
        "max_concurrency": 2,
        "max_queue": 2,
        "max_timeout_ms": 5000,
        "isolated_max_concurrency": 1,
        "isolated_max_queue": 1,
    }
    params.update(overrides)
    return StudioConfig(**params)


async def _start(service: StudioService):
    """把服务起起来。

    Args:
        service: 服务实例。

    Returns:
        ``(客户端桩, 服务器)``。
    """
    server = grpc.aio.server()
    studio_pb2_grpc.add_StudioServicer_to_server(service, server)
    port = server.add_insecure_port(f"127.0.0.1:{_free_port()}")
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    return studio_pb2_grpc.StudioStub(channel), server


async def _exec(stub, **kwargs):
    """调一次 Exec。

    Args:
        stub: gRPC 客户端桩。
        **kwargs: 覆盖默认请求字段。

    Returns:
        ``ExecResponse``。
    """
    params = {
        "skill": "demo",
        "script": "scripts/run.py",
        "language": "python",
        "content": "print('ok')",
    }
    params.update(kwargs)
    return await stub.Exec(studio_pb2.ExecRequest(**params), timeout=30)


# ------------------------------------------------------------------ 路由


async def test_skill_in_isolated_list_goes_isolated():
    """命中 ``isolated_skill_codes`` 的技能走 isolated 档。"""
    service = StudioService(
        _config(isolated_skill_codes=("iso-skill",)),
        isolated_runtime=_FakeIsolatedRuntime(),
    )
    stub, server = await _start(service)
    try:
        resp = await _exec(stub, skill="iso-skill", request_id="r1")
        assert resp.status == studio_pb2.EXEC_STATUS_COMPLETED
        assert resp.stdout == "isolated ok"
        assert len(service._isolated_launcher._runtime.calls) == 1
    finally:
        await server.stop(grace=1)


async def test_other_skills_stay_on_fast():
    """没进名单的技能照常走 fast 档。"""
    service = StudioService(
        _config(isolated_skill_codes=("iso-skill",)),
        isolated_runtime=_FakeIsolatedRuntime(),
    )
    stub, server = await _start(service)
    try:
        resp = await _exec(stub, skill="plain-skill", request_id="r2")
        assert resp.status == studio_pb2.EXEC_STATUS_COMPLETED
        # 换行在不同平台不一样（Windows 是 \r\n），别去钉死它。
        assert resp.stdout.strip() == "ok"
        # isolated 档一次都没被碰过。
        assert service._isolated_launcher._runtime.calls == []
    finally:
        await server.stop(grace=1)


async def test_instance_default_profile_isolated_sends_everything():
    """实例默认档是 isolated 时，**所有**技能都走 isolated。"""
    service = StudioService(
        _config(profile="isolated"),
        isolated_runtime=_FakeIsolatedRuntime(),
    )
    stub, server = await _start(service)
    try:
        resp = await _exec(stub, skill="any-skill", request_id="r3")
        assert resp.stdout == "isolated ok"
    finally:
        await server.stop(grace=1)


# ------------------------------------------------------- 运行时缺失时明确报错


async def test_isolated_without_runtime_fails_precondition():
    """运行时没部署 → FAILED_PRECONDITION，且**没有**执行发生。"""
    service = StudioService(_config(isolated_skill_codes=("iso-skill",)))
    stub, server = await _start(service)
    try:
        with pytest.raises(grpc.aio.AioRpcError) as excinfo:
            await _exec(stub, skill="iso-skill", request_id="r4")
        assert excinfo.value.code() == grpc.StatusCode.FAILED_PRECONDITION
        assert "没有部署容器运行时" in excinfo.value.details()
        # **不能**悄悄退回 fast 档：一次执行都没发生。
        assert "studio_exec_total{" not in service.metrics.registry.render()
    finally:
        await server.stop(grace=1)


async def test_isolated_unavailable_is_counted_as_failure():
    """档位不可用要进 ``studio_exec_failed_total``。"""
    service = StudioService(_config(isolated_skill_codes=("iso-skill",)))
    stub, server = await _start(service)
    try:
        with pytest.raises(grpc.aio.AioRpcError):
            await _exec(stub, skill="iso-skill", request_id="r5")
        text = service.metrics.registry.render()
        assert 'studio_exec_failed_total{reason="isolated_unavailable"} 1' in text
    finally:
        await server.stop(grace=1)


async def test_launcher_raises_when_runtime_missing():
    """launcher 本身也要明确抛错，不能返回一个假的成功结果。"""
    launcher = IsolatedLauncher()
    assert launcher.available is False
    with pytest.raises(Exception) as excinfo:
        await launcher.run(ExecSpec(content="print(1)"))
    assert "没有部署容器运行时" in str(excinfo.value)


# --------------------------------------------------------------- 队列隔离


async def test_isolated_pool_does_not_block_the_fast_pool():
    """isolated 档被占满时，fast 档**照常跑**（§8.4 的核心）。

    这是 §8.4 那句话的可执行版本：「isolated 档必须单独排队，不能和 fast 档
    抢同一个并发池——否则几个慢脚本能把快脚本全堵死」。
    """
    runtime = _FakeIsolatedRuntime(delay=1.0)
    service = StudioService(
        _config(isolated_skill_codes=("iso-skill",), max_concurrency=1),
        isolated_runtime=runtime,
    )
    stub, server = await _start(service)
    try:
        # 占住 isolated 档的并发位（它的上限是 1）。
        slow = asyncio.create_task(
            _exec(stub, skill="iso-skill", request_id="slow")
        )
        await asyncio.sleep(0.15)

        # fast 档这时必须还能跑——它有**自己的**池。
        started = time.monotonic()
        resp = await _exec(stub, skill="plain-skill", request_id="fast")
        cost = time.monotonic() - started
        assert resp.status == studio_pb2.EXEC_STATUS_COMPLETED
        assert cost < 0.8, f"fast 档被 isolated 档堵住了（{cost:.2f}s）"

        await slow
    finally:
        await server.stop(grace=1)


async def test_isolated_queue_is_separate_from_fast_queue():
    """isolated 档的等待队列满了，拒的也只是 isolated 档的请求。"""
    runtime = _FakeIsolatedRuntime(delay=1.0)
    service = StudioService(
        _config(
            isolated_skill_codes=("iso-skill",),
            isolated_max_concurrency=1,
            isolated_max_queue=1,
            max_concurrency=2,
            max_queue=2,
        ),
        isolated_runtime=runtime,
    )
    stub, server = await _start(service)
    try:
        # 1 个在跑 + 1 个在排队 → isolated 池满了。
        first = asyncio.create_task(
            _exec(stub, skill="iso-skill", request_id="iso-1")
        )
        await asyncio.sleep(0.1)
        second = asyncio.create_task(
            _exec(stub, skill="iso-skill", request_id="iso-2")
        )
        await asyncio.sleep(0.1)
        with pytest.raises(grpc.aio.AioRpcError) as excinfo:
            await _exec(stub, skill="iso-skill", request_id="iso-3")
        assert excinfo.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED

        # fast 档完全不受影响。
        resp = await _exec(stub, skill="plain-skill", request_id="fast-1")
        assert resp.status == studio_pb2.EXEC_STATUS_COMPLETED

        await asyncio.gather(first, second)
    finally:
        await server.stop(grace=1)


# --------------------------------------------------------------- 指标标签


async def test_profile_label_reflects_the_actual_profile():
    """真正走 isolated 档跑完的那一次，profile 标签是 isolated。"""
    service = StudioService(
        _config(isolated_skill_codes=("iso-skill",)),
        isolated_runtime=_FakeIsolatedRuntime(),
    )
    stub, server = await _start(service)
    try:
        await _exec(stub, skill="iso-skill", request_id="r6")
        await _exec(stub, skill="plain-skill", request_id="r7")
        text = service.metrics.registry.render()
        assert (
            'studio_exec_total{status="completed",language="python",'
            'profile="isolated"} 1' in text
        )
        assert (
            'studio_exec_total{status="completed",language="python",'
            'profile="fast"} 1' in text
        )
    finally:
        await server.stop(grace=1)


async def test_sandbox_resource_usage_and_estimated_cost_are_recorded():
    """cgroup CPU/memory data is exported and priced with deployment rates."""
    runtime = _FakeIsolatedRuntime(
        cpu_seconds=0.2,
        peak_memory_bytes=64 * 1024 * 1024,
        image_startup_ms=500,
    )
    service = StudioService(
        _config(
            isolated_skill_codes=("iso-skill",),
            cpu_second_rate=2.0,
            memory_gib_second_rate=1.0,
            sandbox_start_rate=0.05,
        ),
        isolated_runtime=runtime,
    )
    stub, server = await _start(service)
    try:
        response = await _exec(
            stub, skill="iso-skill", tenant_id="tenant-cost", request_id="cost-1"
        )
        assert response.status == studio_pb2.EXEC_STATUS_COMPLETED
        text = service.metrics.registry.render()
        assert 'sandbox_cpu_seconds_sum{profile="isolated"' in text
        assert 'sandbox_peak_memory_bytes_sum{profile="isolated"' in text
        cost_line = next(
            line
            for line in text.splitlines()
            if line.startswith(
                'studio_estimated_cost_total{tenant="tenant-cost",'
                'skill="iso-skill",profile="isolated"}'
            )
        )
        assert float(cost_line.rsplit(" ", 1)[1]) > 0.4
    finally:
        await server.stop(grace=1)


async def test_shared_admission_receives_tenant_and_releases_lease():
    """Fast and isolated requests both use the shared tenant quota plane."""
    admission = _FakeAdmission()
    service = StudioService(_config(), admission_runtime=admission)
    stub, server = await _start(service)
    try:
        response = await _exec(
            stub,
            skill="plain",
            tenant_id="tenant-blue",
            request_id="quota-ok",
        )
        assert response.status == studio_pb2.EXEC_STATUS_COMPLETED
        assert admission.acquired[0]["tenant_id"] == "tenant-blue"
        assert admission.acquired[0]["skill"] == "plain"
        assert admission.acquired[0]["cost"] >= 1
        assert admission.released == ["lease-1"]
    finally:
        await server.stop(grace=1)


async def test_shared_admission_denial_fails_closed():
    """A denied shared lease must not fall back to an unlimited execution."""
    admission = _FakeAdmission(deny=True)
    service = StudioService(_config(), admission_runtime=admission)
    stub, server = await _start(service)
    try:
        with pytest.raises(grpc.aio.AioRpcError) as excinfo:
            await _exec(stub, skill="plain", tenant_id="tenant-red")
        assert excinfo.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED
        assert "quota_exceeded" in excinfo.value.details()
        assert 'reason="quota_exceeded"' in service.metrics.registry.render()
    finally:
        await server.stop(grace=1)


# ------------------------------------------------------- 请求档位 / 回显 / 能力


async def test_requested_isolated_is_honoured_and_echoed():
    """请求 isolated → 真的走 isolated，并在响应里回显。"""
    runtime = _FakeIsolatedRuntime()
    service = StudioService(_config(), isolated_runtime=runtime)
    stub, server = await _start(service)
    try:
        resp = await _exec(
            stub,
            skill="plain-skill",
            profile=studio_pb2.EXEC_PROFILE_ISOLATED,
            request_id="p1",
        )
        assert resp.status == studio_pb2.EXEC_STATUS_COMPLETED
        assert resp.profile == studio_pb2.EXEC_PROFILE_ISOLATED
        assert len(runtime.calls) == 1
    finally:
        await server.stop(grace=1)


async def test_ops_list_can_upgrade_fast_to_isolated():
    """运维列表是**升级**覆盖：请求 fast、命中列表 → 实际 isolated。"""
    runtime = _FakeIsolatedRuntime()
    service = StudioService(
        _config(isolated_skill_codes=("iso-skill",)), isolated_runtime=runtime
    )
    stub, server = await _start(service)
    try:
        resp = await _exec(
            stub,
            skill="iso-skill",
            profile=studio_pb2.EXEC_PROFILE_FAST,
            request_id="p2",
        )
        assert resp.profile == studio_pb2.EXEC_PROFILE_ISOLATED
        assert len(runtime.calls) == 1
    finally:
        await server.stop(grace=1)


async def test_old_client_without_profile_uses_server_routing():
    """老客户端不带 profile → 沿用旧路由（实例默认档）。"""
    runtime = _FakeIsolatedRuntime()
    service = StudioService(_config(profile="isolated"), isolated_runtime=runtime)
    stub, server = await _start(service)
    try:
        resp = await _exec(stub, skill="any", request_id="p3")
        assert resp.profile == studio_pb2.EXEC_PROFILE_ISOLATED
    finally:
        await server.stop(grace=1)


async def test_requested_isolated_without_runtime_is_capability_missing():
    """请求 isolated、但本实例没有运行时 → FAILED_PRECONDITION，不改走 fast。"""
    service = StudioService(_config())
    stub, server = await _start(service)
    try:
        with pytest.raises(grpc.aio.AioRpcError) as excinfo:
            await _exec(
                stub,
                skill="plain-skill",
                profile=studio_pb2.EXEC_PROFILE_ISOLATED,
                request_id="p4",
            )
        assert excinfo.value.code() == grpc.StatusCode.FAILED_PRECONDITION
        assert "capability_missing" in excinfo.value.details()
    finally:
        await server.stop(grace=1)


async def test_health_reports_profiles_and_images():
    """Health 要按档位报能力，并列出镜像目录。"""
    service = StudioService(
        _config(
            isolated_images=(("ml", "registry/x@sha256:abc"),),
            isolated_startup_budget_ms=12345,
        ),
        isolated_runtime=_FakeIsolatedRuntime(),
    )
    stub, server = await _start(service)
    try:
        health = await stub.Health(studio_pb2.HealthRequest(), timeout=5)
        profiles = {item.name: item for item in health.profiles}
        assert profiles["fast"].ready is True
        assert profiles["isolated"].ready is True
        assert health.images[0].name == "ml"
        assert health.images[0].digest == "sha256:abc"
        assert health.isolated_startup_budget_ms == 12345
    finally:
        await server.stop(grace=1)


async def test_queue_wait_respects_deadline():
    """排队时间超过时限 → 直接 TIMEOUT，不无限等并发位。"""
    runtime = _FakeIsolatedRuntime(delay=1.0)
    service = StudioService(_config(), isolated_runtime=runtime)
    stub, server = await _start(service)
    try:
        first = asyncio.create_task(
            _exec(
                stub,
                skill="s",
                request_id="q1",
                timeout_ms=5000,
                profile=studio_pb2.EXEC_PROFILE_ISOLATED,
            )
        )
        await asyncio.sleep(0.1)
        started = time.monotonic()
        resp = await _exec(
            stub,
            skill="s",
            request_id="q2",
            timeout_ms=100,
            profile=studio_pb2.EXEC_PROFILE_ISOLATED,
        )
        cost = time.monotonic() - started
        assert resp.status == studio_pb2.EXEC_STATUS_TIMEOUT
        assert "排队" in resp.error
        assert cost < 0.9, f"排队没有按时限截断（{cost:.2f}s）"
        await first
    finally:
        await server.stop(grace=1)


async def test_failed_result_is_not_cached():
    """失败结果不进去重窗口——瞬时故障重试不能被静默吞掉。"""
    runtime = _FakeIsolatedRuntime(status="failed")
    service = StudioService(_config(), isolated_runtime=runtime)
    stub, server = await _start(service)
    try:
        first = await _exec(
            stub,
            skill="s",
            request_id="same",
            timeout_ms=5000,
            profile=studio_pb2.EXEC_PROFILE_ISOLATED,
        )
        second = await _exec(
            stub,
            skill="s",
            request_id="same",
            timeout_ms=5000,
            profile=studio_pb2.EXEC_PROFILE_ISOLATED,
        )
        assert first.status == studio_pb2.EXEC_STATUS_FAILED
        assert second.status == studio_pb2.EXEC_STATUS_FAILED
        assert len(runtime.calls) == 2
    finally:
        await server.stop(grace=1)
