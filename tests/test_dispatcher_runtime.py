"""studio → dispatcher 的内部 HTTP 客户端（skill-service §2.4）。"""

from __future__ import annotations

import socket

import pytest

from services.dispatcher.backend import SandboxError
from services.dispatcher.config import DispatcherConfig
from services.dispatcher.server import DispatcherServer
from services.sandbox.protocol import SandboxResult, SandboxSpec
from services.studio.dispatcher_runtime import DispatcherRuntime
from services.studio.launcher import ExecSpec


class _FakeBackend:
    """假的沙箱后端。"""

    def __init__(self, result: SandboxResult | None = None, error: Exception | None = None):
        """记住要返回什么。"""
        self.result = result or SandboxResult(
            status="completed",
            exit_code=0,
            stdout="hi",
            stderr="",
            duration_ms=9,
            startup_ms=4,
        )
        self.error = error
        self.specs: list[SandboxSpec] = []

    async def run(self, spec: SandboxSpec) -> SandboxResult:
        """假的执行。"""
        self.specs.append(spec)
        if self.error is not None:
            raise self.error
        return self.result

    async def health(self) -> bool:
        """假的探活。"""
        return True

    async def close(self) -> None:
        """假的收尾。"""
        return None


async def _start(backend: _FakeBackend):
    """起一个 dispatcher，返回 ``(server, addr)``。"""
    server = DispatcherServer(
        DispatcherConfig(
            host="127.0.0.1",
            port=0,
            images=(("default", "img:1"),),
            require_digest=False,
        ),
        backend,
    )
    await server.start()
    return server, f"127.0.0.1:{server.port}"


def _spec(**overrides) -> ExecSpec:
    """一个合法的执行输入。"""
    params = {
        "skill": "demo",
        "script": "scripts/a.py",
        "language": "python",
        "content": "print(1)",
        "argv": ("--x",),
        "stdin": "",
        "timeout_ms": 1000,
        "image": "default",
        "request_id": "req-1",
    }
    params.update(overrides)
    return ExecSpec(**params)


async def test_runtime_health_reports_ready():
    """健康检查直接用 GET /health。"""
    server, addr = await _start(_FakeBackend())
    try:
        assert await DispatcherRuntime(addr).health() is True
    finally:
        await server.close()


async def test_runtime_health_is_false_when_down():
    """不可达 → False，不抛。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert await DispatcherRuntime(f"127.0.0.1:{port}").health() is False


async def test_runtime_maps_result_and_forwards_spec():
    """结果字段要映射全；request_id/镜像要带过去。"""
    backend = _FakeBackend()
    server, addr = await _start(backend)
    try:
        runtime = DispatcherRuntime(addr)
        outcome = await runtime.run(_spec())
        assert outcome.status == "completed"
        assert outcome.exit_code == 0
        assert outcome.stdout == "hi"
        assert outcome.startup_ms == 4
        assert outcome.duration_ms == 9
        spec = backend.specs[0]
        assert spec.request_id == "req-1"
        assert spec.image == "default"
        assert spec.argv == ("--x",)
    finally:
        await server.close()


async def test_runtime_maps_http_error_to_failed_outcome():
    """dispatcher 的 400 要变成 failed 结果，而不是抛异常。"""
    backend = _FakeBackend(error=SandboxError("镜像没了", code="image_unknown"))
    server, addr = await _start(backend)
    try:
        outcome = await DispatcherRuntime(addr).run(_spec())
        assert outcome.status == "failed"
        assert outcome.error_code == "image_unknown"
        assert "镜像没了" in outcome.error
    finally:
        await server.close()


async def test_runtime_down_returns_failed_outcome():
    """dispatcher 不可达 → failed + sandbox_unavailable，不炸上层。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    outcome = await DispatcherRuntime(f"127.0.0.1:{port}").run(_spec())
    assert outcome.status == "failed"
    assert outcome.error_code == "sandbox_unavailable"


async def test_runtime_bad_addr_returns_failed_outcome():
    """地址不合法 → 明确失败，不去连一个随机的 host。"""
    outcome = await DispatcherRuntime("no-port").run(_spec())
    assert outcome.status == "failed"
    assert outcome.error_code == "sandbox_unavailable"
