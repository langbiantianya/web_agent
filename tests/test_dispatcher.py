"""dispatcher 的 HTTP 服务（skill-service §2.4）。"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from services.dispatcher.backend import SandboxError
from services.dispatcher.config import DispatcherConfig
from services.dispatcher.server import DispatcherServer
from services.sandbox.protocol import SandboxResult, SandboxSpec


class _FakeBackend:
    """假的沙箱后端：记下 spec，按预设返回/抛错。"""

    def __init__(
        self,
        result: SandboxResult | None = None,
        error: Exception | None = None,
        delay: float = 0.0,
        health: bool = True,
    ) -> None:
        """记住要返回什么。"""
        self.result = result or SandboxResult(
            status="completed", exit_code=0, stdout="ok", duration_ms=3
        )
        self.error = error
        self.delay = delay
        self._health = health
        self.specs: list[SandboxSpec] = []
        self.cancelled: list[str] = []
        self.closed = False

    async def run(self, spec: SandboxSpec) -> SandboxResult:
        """假的执行。"""
        self.specs.append(spec)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.result

    async def health(self) -> bool:
        """假的探活。"""
        return self._health

    async def close(self) -> None:
        """假的收尾。"""
        self.closed = True

    async def cancel(self, execution_id: str) -> bool:
        """记录取消请求。"""
        self.cancelled.append(execution_id)
        return True


def _config(**overrides) -> DispatcherConfig:
    """造一份测试配置。"""
    params = {
        "host": "127.0.0.1",
        "port": 0,
        "images": (("default", "img:1"),),
        "max_inflight": 2,
        "max_timeout_ms": 5000,
        "require_digest": False,
    }
    params.update(overrides)
    return DispatcherConfig(**params)


async def _start(backend: _FakeBackend, **overrides):
    """起一个 dispatcher，返回 ``(server, client)``。"""
    server = DispatcherServer(_config(**overrides), backend)
    await server.start()
    client = httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{server.port}", timeout=10
    )
    return server, client


def _payload(**overrides) -> dict:
    """一个合法的执行请求。"""
    data = {
        "skill": "demo",
        "script": "scripts/a.py",
        "language": "python",
        "content": "print(1)",
        "argv": ["--x"],
        "stdin": "",
        "timeout_ms": 1000,
        "image": "default",
        "request_id": "req-1",
    }
    data.update(overrides)
    return data


async def test_health_reports_backend_state():
    """探活要真的问后端。"""
    backend = _FakeBackend()
    server, client = await _start(backend)
    try:
        resp = await client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ready"] is True
        assert body["version"] == "1.0.0"
    finally:
        await client.aclose()
        await server.close()


async def test_exec_forwards_spec_and_returns_result():
    """执行请求要原样转发（时限压到上限），结果原样返回。"""
    backend = _FakeBackend(
        result=SandboxResult(
            status="completed",
            exit_code=0,
            stdout="hello",
            stderr="",
            truncated=False,
            duration_ms=7,
            startup_ms=12,
            sandbox_id="abc123",
        )
    )
    server, client = await _start(backend)
    try:
        resp = await client.post("/v1/exec", json=_payload(timeout_ms=999_999))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "completed"
        assert body["stdout"] == "hello"
        assert body["startup_ms"] == 12
        spec = backend.specs[0]
        assert spec.skill == "demo"
        assert spec.argv == ("--x",)
        assert spec.timeout_ms == 5000  # 被 max_timeout_ms 压住
        assert spec.image == "default"
    finally:
        await client.aclose()
        await server.close()


async def test_unknown_image_is_rejected_before_backend():
    """镜像不在目录里 → 400，且不碰后端。"""
    backend = _FakeBackend()
    server, client = await _start(backend)
    try:
        resp = await client.post("/v1/exec", json=_payload(image="missing"))
        assert resp.status_code == 400
        assert resp.json()["error_code"] == "image_unknown"
        assert backend.specs == []
    finally:
        await client.aclose()
        await server.close()


async def test_empty_content_is_rejected():
    """空 content → 400。"""
    backend = _FakeBackend()
    server, client = await _start(backend)
    try:
        resp = await client.post("/v1/exec", json=_payload(content="  "))
        assert resp.status_code == 400
        assert resp.json()["error_code"] == "invalid_request"
    finally:
        await client.aclose()
        await server.close()


async def test_backend_error_maps_to_http():
    """后端错误按 code 映射：镜像 400，其余 503。"""
    backend = _FakeBackend(error=SandboxError("镜像没了", code="image_unknown"))
    server, client = await _start(backend)
    try:
        resp = await client.post("/v1/exec", json=_payload())
        assert resp.status_code == 400
        assert resp.json()["error_code"] == "image_unknown"
    finally:
        await client.aclose()
        await server.close()


async def test_saturation_returns_503():
    """并发满了 → 503，让调用方退避。"""
    backend = _FakeBackend(delay=1.5)
    server, client = await _start(backend, max_inflight=1)
    try:
        first = asyncio.create_task(client.post("/v1/exec", json=_payload()))
        await asyncio.sleep(0.1)
        second = await client.post("/v1/exec", json=_payload())
        assert second.status_code == 503
        assert second.json()["error_code"] == "sandbox_capacity"
        response = await first
        assert response.status_code == 200
    finally:
        await client.aclose()
        await server.close()


async def test_unknown_path_is_404():
    """别的路径 404。"""
    server, client = await _start(_FakeBackend())
    try:
        resp = await client.get("/nope")
        assert resp.status_code == 404
    finally:
        await client.aclose()
        await server.close()


async def test_close_closes_backend():
    """关服务要把后端一起收掉。"""
    backend = _FakeBackend()
    server, client = await _start(backend)
    await client.aclose()
    await server.close()
    assert backend.closed is True


async def test_cancel_route_propagates_to_backend():
    """取消路由把 request_id 传给后端并返回取消结果。"""
    backend = _FakeBackend()
    server, client = await _start(backend)
    try:
        response = await client.post("/v1/exec/req-1/cancel")
        assert response.status_code == 200
        assert response.json()["cancelled"] is True
        assert backend.cancelled == ["req-1"]
    finally:
        await client.aclose()
        await server.close()


def test_image_digest_is_required_by_default():
    """生产配置拒绝浮动 tag，固定 digest 才通过。"""
    from services.dispatcher.config import DispatcherConfigError

    with pytest.raises(DispatcherConfigError):
        DispatcherConfig(images=(("default", "image:latest"),)).validate()
    DispatcherConfig(
        images=(("default", "image@sha256:" + "a" * 64),)
    ).validate()
    DispatcherConfig(
        images=(("default", "image:latest"),), require_digest=False
    ).validate()
