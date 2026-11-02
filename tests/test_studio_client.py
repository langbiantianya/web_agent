"""studio gRPC 客户端（设计文档 §22.8、§22.8.5）。

钉住三件事：

1. **时限上限是硬的**——模型请求的时限只能在 ``studio_exec_max_timeout_ms``
   之内，模型改不了；
2. **gRPC 成功 ≠ 脚本成功**——脚本自己 ``exit 1`` 是 ``completed`` +
   ``exit_code=1``，不是错误（skill-service §4.1）；
3. **gRPC 错误码要翻译**——``UNAVAILABLE`` / ``DEADLINE_EXCEEDED`` 各自对应
   不同的异常，上层才知道该怎么回灌给模型。

这里用**假的桩**（不真起 studio）：本文件测的是**调用侧**的翻译逻辑，
真 studio 的端到端在 :mod:`tests.test_studio_e2e`。
"""

from __future__ import annotations

import grpc
import pytest

from app.config.settings import settings
from app.core import studio as studio_mod
from app.core.studio import (
    ExecResult,
    StudioClient,
    StudioError,
    StudioTimeoutError,
    StudioUnavailableError,
    close_studio_client,
    configure_studio,
    effective_timeout_ms,
    get_studio_client,
    studio_configured,
)
from webagent.studio.v1 import studio_pb2


class _FakeStub:
    """假的 StudioStub：按脚本内容返回预设的响应或抛错。"""

    def __init__(self, response=None, error: Exception | None = None) -> None:
        """记住要返回什么。"""
        self.response = response
        self.error = error
        self.calls: list[tuple[str, object, float | None]] = []

    async def Health(self, request, timeout=None):  # noqa: N802 - gRPC 桩的命名
        """假的 Health。"""
        self.calls.append(("Health", request, timeout))
        if self.error is not None:
            raise self.error
        return self.response

    async def Exec(self, request, timeout=None):  # noqa: N802 - gRPC 桩的命名
        """假的 Exec。"""
        self.calls.append(("Exec", request, timeout))
        if self.error is not None:
            raise self.error
        return self.response


def _rpc_error(code: grpc.StatusCode, details: str = "boom") -> grpc.aio.AioRpcError:
    """造一个 gRPC 异常。"""
    return grpc.aio.AioRpcError(code, grpc.aio.Metadata(), details, None)


# --------------------------------------------------------------- 时限上限


def test_timeout_is_capped_by_config(monkeypatch):
    """模型请求的时限被压到配置上限——上限写死在配置里，模型改不了。"""
    monkeypatch.setattr(settings, "studio_exec_timeout_ms", 30000)
    monkeypatch.setattr(settings, "studio_exec_max_timeout_ms", 60000)
    assert effective_timeout_ms(10000) == 10000
    assert effective_timeout_ms(None) == 30000
    # 请求超过上限 → 压到上限
    assert effective_timeout_ms(999999) == 60000


def test_timeout_never_becomes_zero(monkeypatch):
    """时限至少是 1 毫秒——0 会让 gRPC 立刻 deadline exceeded。"""
    monkeypatch.setattr(settings, "studio_exec_max_timeout_ms", 60000)
    assert effective_timeout_ms(0) == 1
    assert effective_timeout_ms(-5) == 1


# --------------------------------------------------------------- 结果语义


def test_exec_result_ok_semantics():
    """「gRPC 成功 ≠ 脚本成功」。

    脚本自己 exit 1 是 ``completed`` + ``exit_code=1``：对 studio 来说这次执行
    正常结束了，但对脚本来说它失败了。用 gRPC 错误码去报「脚本失败」的话，
    调用方只能当成服务不可用处理——语义完全错了。
    """
    ok = ExecResult("completed", 0, "out", "", False, 10, "", "digest")
    assert ok.ok is True
    failed = ExecResult("completed", 1, "", "boom", False, 10, "", "digest")
    assert failed.ok is False
    timed_out = ExecResult("timeout", -1, "", "", False, 10, "超时", "")
    assert timed_out.ok is False


# --------------------------------------------------------------- 错误翻译


@pytest.mark.parametrize(
    "code,expected",
    [
        (grpc.StatusCode.DEADLINE_EXCEEDED, StudioTimeoutError),
        (grpc.StatusCode.UNAVAILABLE, StudioUnavailableError),
        (grpc.StatusCode.INVALID_ARGUMENT, StudioError),
        (grpc.StatusCode.FAILED_PRECONDITION, StudioError),
        (grpc.StatusCode.RESOURCE_EXHAUSTED, StudioError),
        (grpc.StatusCode.INTERNAL, StudioError),
    ],
)
async def test_rpc_errors_are_translated(code, expected):
    """gRPC 错误码翻译成我们自己的异常类型。"""
    client = StudioClient("studio:9000")
    stub = _FakeStub(error=_rpc_error(code, "细节"))
    client._stub = stub  # noqa: SLF001 - 测试里直接塞假桩
    with pytest.raises(expected):
        await client.exec_script(
            skill="s", script="scripts/a.py", language="python", content="print(1)"
        )


async def test_unavailable_error_message_mentions_address():
    """连不上时的错误信息要带地址——否则运维不知道该看哪台机器。"""
    client = StudioClient("studio:9000")
    client._stub = _FakeStub(error=_rpc_error(grpc.StatusCode.UNAVAILABLE, "拒绝连接"))  # noqa: SLF001
    with pytest.raises(StudioUnavailableError) as ei:
        await client.health()
    assert "studio:9000" in str(ei.value)


# --------------------------------------------------------------- 调用形状


async def test_exec_sends_expected_request():
    """请求体字段与契约一致。"""
    client = StudioClient("studio:9000")
    stub = _FakeStub(
        response=studio_pb2.ExecResponse(
            status=studio_pb2.EXEC_STATUS_COMPLETED,
            exit_code=0,
            stdout="hi",
            stderr="",
            duration_ms=12,
            stdout_digest="d",
        )
    )
    client._stub = stub  # noqa: SLF001
    result = await client.exec_script(
        skill="demo",
        script="scripts/extract.py",
        language="python",
        content="print('hi')",
        argv=["--a", "b"],
        stdin="in",
        timeout_ms=5000,
        run_id="7",
        node_id="n1",
        request_id="req-1",
    )
    assert result.ok is True
    assert result.stdout == "hi"
    assert result.duration_ms == 12

    method, request, timeout = stub.calls[0]
    assert method == "Exec"
    assert request.skill == "demo"
    assert request.script == "scripts/extract.py"
    assert request.language == "python"
    assert request.content == "print('hi')"
    assert list(request.argv) == ["--a", "b"]
    assert request.stdin == "in"
    assert request.timeout_ms == 5000
    assert request.run_id == "7"
    assert request.node_id == "n1"
    assert request.request_id == "req-1"
    # gRPC deadline = 脚本时限 + 宽限
    assert timeout is not None and timeout > 5.0


async def test_exec_translates_response_status():
    """proto 枚举翻译成状态名。"""
    client = StudioClient("studio:9000")
    client._stub = _FakeStub(  # noqa: SLF001
        response=studio_pb2.ExecResponse(
            status=studio_pb2.EXEC_STATUS_TIMEOUT, exit_code=-1, error="超时"
        )
    )
    result = await client.exec_script(
        skill="s", script="s.py", language="python", content="x"
    )
    assert result.status == "timeout"
    assert result.error == "超时"
    assert result.ok is False


async def test_health_maps_language_info():
    """探活把运行时清单报出来。"""
    client = StudioClient("studio:9000")
    client._stub = _FakeStub(  # noqa: SLF001
        response=studio_pb2.HealthResponse(
            ready=True,
            version="1.0.0",
            language_info=[
                studio_pb2.LanguageInfo(name="python", version="3.11.9", extensions=[".py"]),
                studio_pb2.LanguageInfo(name="bash", version="5.2"),
            ],
            max_concurrency=8,
            profile="fast",
        )
    )
    health = await client.health()
    assert health.ready is True
    assert health.version == "1.0.0"
    assert health.languages == ("python", "bash")
    assert health.max_concurrency == 8
    assert health.profile == "fast"


# --------------------------------------------------------------- 全局单例


async def test_configure_studio_with_empty_addr_disables():
    """地址为空 = 未启用脚本执行（§22.8.8）。"""
    try:
        assert configure_studio("") is None
        assert get_studio_client() is None
    finally:
        await close_studio_client()


async def test_configure_studio_creates_client():
    """配置地址后拿到客户端。"""
    try:
        client = configure_studio("studio:9000")
        assert client is not None
        assert client.addr == "studio:9000"
        assert get_studio_client() is client
        # 再配一次同样的地址 → 复用同一个客户端（不重连）
        assert configure_studio("studio:9000") is client
    finally:
        await close_studio_client()


def test_studio_configured_follows_settings(monkeypatch):
    """「有没有配置」只看地址是否非空。"""
    monkeypatch.setattr(settings, "studio_exec_addr", "")
    assert studio_configured() is False
    monkeypatch.setattr(settings, "studio_exec_addr", "studio:9000")
    assert studio_configured() is True
