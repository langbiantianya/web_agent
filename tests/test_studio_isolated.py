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

    def __init__(self, delay: float = 0.0) -> None:
        """记下模拟的耗时。

        Args:
            delay: 每次执行模拟的耗时（秒）。
        """
        self.delay = delay
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
            return ExecOutcome(
                status="completed",
                exit_code=0,
                stdout="isolated ok",
                stderr="",
                truncated=False,
                duration_ms=1,
                error="",
                stdout_digest="deadbeef",
            )
        finally:
            self._active -= 1

    async def close(self) -> None:
        """没有要收的尾。"""
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
