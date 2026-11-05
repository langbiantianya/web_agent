"""studio 的指标与结构化日志（skill-service §12.1、§12.2）。

## 这里钉住的命题

1. **指标的名字、类型、标签要对**（§12.1 的表逐条对齐）——面板与告警是照着它写的。
2. **exposition 格式要对**：Prometheus 的抓取器要能直接吃下去。
3. **一次执行要记全**：次数、耗时、排队、在跑数、失败原因。
4. **结构化日志是 JSON**，字段与 §12.2 的样本一致。
5. **日志里不能有脚本输出内容**——输出可能含敏感数据（主设计 §22.10 第 2 条），
   只记字节数与摘要。
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket

import grpc
import pytest

from services.studio.config import StudioConfig
from services.studio.exec_log import build_exec_log
from services.studio.metrics import (
    Counter,
    Gauge,
    Histogram,
    MetricsRegistry,
    build_metrics,
    serve_metrics,
)
from services.studio.server import StudioService
from webagent.studio.v1 import studio_pb2, studio_pb2_grpc


def _free_port() -> int:
    """向 OS 借一个空闲端口。

    Returns:
        端口号。
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def stub():
    """在测试进程里起一个**真的** studio 服务。

    Yields:
        ``(gRPC 客户端桩, 服务实例)``。
    """
    config = StudioConfig(
        languages=("python",),
        max_concurrency=2,
        max_queue=1,
        max_timeout_ms=5000,
    )
    service = StudioService(config)
    server = grpc.aio.server()
    studio_pb2_grpc.add_StudioServicer_to_server(service, server)
    port = server.add_insecure_port(f"127.0.0.1:{_free_port()}")
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    try:
        yield studio_pb2_grpc.StudioStub(channel), service
    finally:
        await channel.close()
        await server.stop(grace=1)


async def _exec(stub, **kwargs):
    """调一次 Exec，带默认的合法参数。

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
        "request_id": "metrics-test",
    }
    params.update(kwargs)
    client, _ = stub
    return await client.Exec(studio_pb2.ExecRequest(**params), timeout=30)


# ------------------------------------------------------------- exposition 格式


def test_counter_renders_help_type_and_sample():
    """计数器要渲染 HELP / TYPE 与样本行。"""
    registry = MetricsRegistry()
    counter = registry.counter("demo_total", "演示", ("status",))
    counter.inc(status="ok")
    text = registry.render()
    assert "# HELP demo_total 演示" in text
    assert "# TYPE demo_total counter" in text
    assert 'demo_total{status="ok"} 1' in text


def test_gauge_can_go_up_and_down():
    """仪表能加能减。"""
    registry = MetricsRegistry()
    gauge = registry.gauge("demo_active", "在跑数")
    gauge.inc()
    gauge.inc()
    gauge.dec()
    assert "demo_active 1" in registry.render()


def test_histogram_renders_buckets_sum_and_count():
    """直方图要有各桶、``+Inf``、sum、count。"""
    registry = MetricsRegistry()
    hist = registry.histogram(
        "demo_seconds", "耗时", ("language",), buckets=(0.1, 1.0)
    )
    # 0.25 与 0.5 相加在二进制浮点下是**精确**的 0.75——别挑那种加出来
    # 0.55000000000000004 的数，断言会变成在测浮点误差。
    hist.observe(0.25, language="python")
    hist.observe(0.5, language="python")
    text = registry.render()
    assert 'demo_seconds_bucket{language="python",le="0.1"} 0' in text
    assert 'demo_seconds_bucket{language="python",le="1.0"} 2' in text
    assert 'demo_seconds_bucket{language="python",le="+Inf"} 2' in text
    assert 'demo_seconds_count{language="python"} 2' in text
    assert 'demo_seconds_sum{language="python"} 0.75' in text


def test_label_values_are_escaped():
    """标签值里的引号、反斜杠、换行要转义——不然 exposition 会被解析坏。"""
    registry = MetricsRegistry()
    counter = registry.counter("demo_total", "演示", ("path",))
    counter.inc(path='a"b\\c\nd')
    line = [x for x in registry.render().splitlines() if x.startswith("demo_total{")][0]
    assert 'path="a\\"b\\\\c\\nd"' in line


def test_build_metrics_registers_the_whole_table():
    """执行、队列与沙箱阶段指标一个都不能少。"""
    metrics = build_metrics()
    text = metrics.registry.render()
    for name in (
        "studio_exec_total",
        "studio_exec_duration_seconds",
        "studio_exec_active",
        "studio_exec_queue_wait_seconds",
        "studio_exec_timeout_total",
        "studio_exec_output_truncated_total",
        "studio_exec_failed_total",
        "studio_orphan_process_total",
        "sandbox_start_seconds",
        "sandbox_total",
        "sandbox_duration_seconds",
        "sandbox_cpu_seconds",
        "sandbox_peak_memory_bytes",
        "studio_estimated_cost_total",
    ):
        assert f"# TYPE {name} " in text, name


# --------------------------------------------------------------- HTTP 端点


async def _http_get(port: int, path: str) -> tuple[int, str]:
    """发一个 HTTP GET。

    **必须用异步流**——在 async 测试里做阻塞的 HTTP 调用会把事件循环堵死：
    服务端要等这个循环转起来才能回话，而客户端在等它回话。

    Args:
        port: 端口。
        path: 路径。

    Returns:
        ``(状态码, 响应体)``。
    """
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(65536), timeout=10)
    finally:
        writer.close()
    text = raw.decode("latin-1")
    head, _, body = text.partition("\r\n\r\n")
    status = int(head.split(" ")[1])
    return status, body.encode("latin-1").decode("utf-8", errors="replace")


async def test_metrics_endpoint_serves_exposition():
    """``/metrics`` 要能真的被 HTTP 抓下来。"""
    metrics = build_metrics()
    metrics.exec_total.inc(status="completed", language="python", profile="fast")
    port = _free_port()
    server = await serve_metrics(metrics.registry, "127.0.0.1", port)
    try:
        status, body = await _http_get(port, "/metrics")
        assert status == 200
        assert 'studio_exec_total{status="completed"' in body
        assert "# TYPE studio_exec_total counter" in body
    finally:
        server.close()
        await server.wait_closed()


async def test_metrics_endpoint_404s_on_other_paths():
    """别的路径返回 404——别让人以为那也是指标。"""
    metrics = build_metrics()
    port = _free_port()
    server = await serve_metrics(metrics.registry, "127.0.0.1", port)
    try:
        status, _ = await _http_get(port, "/whatever")
        assert status == 404
    finally:
        server.close()
        await server.wait_closed()


# --------------------------------------------------------------- 一次执行记了什么


async def test_exec_records_the_metrics(stub):
    """跑一次执行，指标里要有它。"""
    client, service = stub
    resp = await _exec(stub, content="print('hello metrics')")
    assert resp.status == studio_pb2.EXEC_STATUS_COMPLETED
    text = service.metrics.registry.render()
    assert (
        'studio_exec_total{status="completed",language="python",profile="fast"} 1'
        in text
    )
    assert (
        'studio_exec_duration_seconds_count{language="python",profile="fast"} 1'
        in text
    )
    assert 'studio_exec_queue_wait_seconds_count{profile="fast"} 1' in text
    # 执行完了，在跑数必须回到 0。
    assert "studio_exec_active 0" in text


async def test_timeout_is_counted(stub):
    """超时要进 ``studio_exec_timeout_total``。"""
    client, service = stub
    await _exec(
        stub, content="import time\ntime.sleep(30)\n", timeout_ms=800
    )
    text = service.metrics.registry.render()
    assert 'studio_exec_timeout_total{language="python"} 1' in text


async def test_invalid_request_is_counted(stub):
    """请求非法要进 ``studio_exec_failed_total{reason="invalid_request"}``。"""
    client, service = stub
    with pytest.raises(grpc.aio.AioRpcError):
        await _exec(stub, content="   ")
    text = service.metrics.registry.render()
    assert 'studio_exec_failed_total{reason="invalid_request"} 1' in text


async def test_unknown_language_is_counted(stub):
    """运行时不支持要进 ``studio_exec_failed_total{reason="unknown_language"}``。"""
    client, service = stub
    with pytest.raises(grpc.aio.AioRpcError):
        await _exec(stub, language="cobol", content="print(1)\n")
    text = service.metrics.registry.render()
    assert 'studio_exec_failed_total{reason="unknown_language"} 1' in text


# --------------------------------------------------------------- 结构化日志


def test_exec_log_shape_matches_the_sample():
    """日志字段要与 §12.2 的样本一致。"""
    line = build_exec_log(
        request_id="req-1",
        run_id="run-1",
        node_id="node-1",
        skill="financial-report",
        script="scripts/extract.py",
        language="python",
        status="completed",
        exit_code=0,
        duration_ms=143,
        stdout_bytes=2048,
        stderr_bytes=0,
        truncated=False,
        stdout_digest="abc123",
    )
    payload = json.loads(line)
    assert payload["event"] == "exec.done"
    assert payload["level"] == "info"
    assert payload["status"] == "COMPLETED"
    assert payload["request_id"] == "req-1"
    assert payload["run_id"] == "run-1"
    assert payload["node_id"] == "node-1"
    assert payload["skill"] == "financial-report"
    assert payload["script"] == "scripts/extract.py"
    assert payload["language"] == "python"
    assert payload["exit_code"] == 0
    assert payload["duration_ms"] == 143
    assert payload["stdout_bytes"] == 2048
    assert payload["stderr_bytes"] == 0
    assert payload["truncated"] is False
    assert payload["stdout_digest"] == "sha256:abc123"
    assert "ts" in payload


def test_exec_log_never_contains_output_content():
    """日志里不能出现脚本输出内容——只记字节数与摘要。

    这是安全边界：脚本输出可能带 key，它会被回灌进 prompt（主设计 §22.10）。
    """
    line = build_exec_log(
        request_id="r",
        run_id="",
        node_id="",
        skill="s",
        script="scripts/a.py",
        language="python",
        status="completed",
        exit_code=0,
        duration_ms=1,
        stdout_bytes=17,
        stderr_bytes=0,
        truncated=False,
        stdout_digest="deadbeef",
    )
    assert "sk-secret-value" not in line
    assert "17" in line


async def test_exec_emits_one_structured_log_line(stub):
    """一次执行一条 JSON 汇总日志。

    这个 logger **不往根 logger 冒泡**（它有自己的 handler，格式是纯 JSON），
    所以测试要自己挂一个 handler 去收，不能用 caplog。
    """
    client, service = stub
    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        """把日志记录攒起来。"""

        def emit(self, record: logging.LogRecord) -> None:
            """收下这条记录。

            Args:
                record: 日志记录。
            """
            captured.append(record)

    target = logging.getLogger("services.studio.exec")
    handler = _Capture()
    target.addHandler(handler)
    try:
        await _exec(stub, content="print('secret-value-123')")
    finally:
        target.removeHandler(handler)

    lines = [record.getMessage() for record in captured]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["event"] == "exec.done"
    assert payload["status"] == "COMPLETED"
    # 字节数只跟「输出有多长」有关——**平台会改换行**（Windows 上是 \r\n），
    # 所以断言一个下界，不去钉死具体数字。
    assert payload["stdout_bytes"] >= len("secret-value-123")
    # 输出内容不能进日志。
    assert "secret-value-123" not in lines[0]


def test_exec_log_line_is_pure_json():
    """汇总行是**一行纯 JSON**——不是被普通日志格式包着的 JSON。

    采集侧要能直接一行一个对象地解析；带前缀就得正则抠。
    """
    line = build_exec_log(
        request_id="r",
        run_id="",
        node_id="",
        skill="s",
        script="scripts/a.py",
        language="python",
        status="completed",
        exit_code=0,
        duration_ms=1,
        stdout_bytes=0,
        stderr_bytes=0,
        truncated=False,
        stdout_digest="",
    )
    assert line.startswith("{")
    assert line.endswith("}")
    assert "\n" not in line
    json.loads(line)
