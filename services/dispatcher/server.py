"""dispatcher 的 HTTP 服务（skill-service §2.4）。

API 很小，而且是**内部契约**（不属于 proto）：

- ``POST /v1/exec``：跑一次沙箱，同步返回结果；
- ``GET /health``：Docker 是否可达。

用标准库 ``asyncio.start_server`` 而不是 Web 框架：dispatcher 的镜像是「不跑用户
代码的控制面」，但它也不需要为两个端点引入一整套依赖。HTTP 只支持我们自己的
客户端会发的形状（Content-Length、Connection: close）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from dataclasses import asdict
from typing import Any

from services.dispatcher.backend import SandboxBackend, SandboxError
from services.dispatcher.config import DispatcherConfig
from services.dispatcher.quota import QuotaExceeded, QuotaManager
from services.sandbox.protocol import (
    MAX_ARGV_ITEMS,
    MAX_ARGV_ITEM_CHARS,
    MAX_CONTENT_CHARS,
    MAX_STDIN_CHARS,
    SandboxResult,
    SandboxSpec,
)

logger = logging.getLogger(__name__)

# 一次请求头最多多少行。
_MAX_HEADERS = 64

# 等待队列的时间上限（秒）。dispatcher 的并发满了就让调用方退避。
_QUEUE_WAIT_SEC = 1.0


class _HttpError(Exception):
    """带 HTTP 状态的业务错误。"""

    def __init__(self, status: int, code: str, message: str) -> None:
        """记住状态、错误码与说明。"""
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _validate_spec(data: dict[str, Any], config: DispatcherConfig) -> SandboxSpec:
    """校验请求 JSON 并转成 :class:`SandboxSpec`。

    Args:
        data: 请求体。
        config: dispatcher 配置。

    Returns:
        ``SandboxSpec``。

    Raises:
        _HttpError: 字段非法 / 镜像不在目录里。
    """
    if not isinstance(data, dict):
        raise _HttpError(400, "invalid_request", "请求体必须是 JSON 对象")
    content = data.get("content")
    if not isinstance(content, str) or not content.strip():
        raise _HttpError(400, "invalid_request", "content 不能为空")
    if len(content) > MAX_CONTENT_CHARS:
        raise _HttpError(400, "invalid_request", "content 超过上限")
    language = str(data.get("language") or "python")
    if language not in ("python", "bash", "node"):
        raise _HttpError(400, "unknown_language", f"不支持的运行时：{language}")
    stdin = data.get("stdin") or ""
    if not isinstance(stdin, str) or len(stdin) > MAX_STDIN_CHARS:
        raise _HttpError(400, "invalid_request", "stdin 非法或超长")
    argv = data.get("argv") or []
    if not isinstance(argv, list) or len(argv) > MAX_ARGV_ITEMS:
        raise _HttpError(400, "invalid_request", "argv 非法或超长")
    for item in argv:
        if not isinstance(item, str) or len(item) > MAX_ARGV_ITEM_CHARS:
            raise _HttpError(400, "invalid_request", "argv 单项非法或超长")
    timeout_ms = data.get("timeout_ms") or config.max_timeout_ms
    try:
        timeout_ms = int(timeout_ms)
    except (TypeError, ValueError) as exc:
        raise _HttpError(400, "invalid_request", "timeout_ms 必须是整数") from exc
    timeout_ms = max(1, min(timeout_ms, config.max_timeout_ms))
    image = str(data.get("image") or "default")
    if config.image_ref(image) is None:
        raise _HttpError(400, "image_unknown", f"镜像目录里没有「{image}」")
    return SandboxSpec(
        skill=str(data.get("skill") or ""),
        tenant_id=str(data.get("tenant_id") or "default")[:64],
        script=str(data.get("script") or ""),
        language=language,
        content=content,
        argv=tuple(argv),
        stdin=stdin,
        timeout_ms=timeout_ms,
        image=image,
        request_id=str(data.get("request_id") or ""),
        traceparent=str(data.get("traceparent") or ""),
    )


class DispatcherServer:
    """dispatcher 的 HTTP 服务。"""

    def __init__(self, config: DispatcherConfig, backend: SandboxBackend) -> None:
        """记住配置与后端。

        Args:
            config: dispatcher 配置。
            backend: 沙箱后端。
        """
        self.config = config
        self.backend = backend
        self._server: asyncio.AbstractServer | None = None
        self._semaphore = asyncio.Semaphore(config.max_inflight)
        self.quota = QuotaManager(
            tenant_quotas=dict(config.tenant_quotas),
            skill_quotas=dict(config.skill_quotas),
            tenant_budgets=dict(config.tenant_budgets),
            skill_budgets=dict(config.skill_budgets),
        )

    async def start(self) -> asyncio.AbstractServer:
        """开始监听。

        Returns:
            ``asyncio.Server``；测试可以从它拿实际端口。
        """
        self._server = await asyncio.start_server(
            self._handle, self.config.host, self.config.port
        )
        return self._server

    @property
    def port(self) -> int:
        """实际监听端口（配置为 0 时由系统分配）。"""
        if self._server is None or not self._server.sockets:
            return self.config.port
        return int(self._server.sockets[0].getsockname()[1])

    async def close(self) -> None:
        """关闭监听并释放后端。"""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        await self.backend.close()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """处理一个连接。"""
        try:
            request = await self._read_request(reader)
            status, payload = await self._route(request)
        except _HttpError as exc:
            status, payload = exc.status, {
                "error": exc.message,
                "error_code": exc.code,
            }
        except Exception:  # noqa: BLE001
            logger.exception("dispatcher 内部错误")
            status, payload = 500, {
                "error": "dispatcher 内部错误",
                "error_code": "internal",
            }
        await self._write_response(writer, status, payload)
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass

    async def _read_request(
        self, reader: asyncio.StreamReader
    ) -> tuple[str, str, dict[str, str], bytes]:
        """读一个 HTTP 请求（单请求，Connection: close）。"""
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
        except asyncio.TimeoutError as exc:
            raise _HttpError(408, "timeout", "读请求头超时") from exc
        if not line:
            raise _HttpError(400, "invalid_request", "空请求")
        try:
            method, target, _version = line.decode("latin-1").strip().split(" ", 2)
        except ValueError as exc:
            raise _HttpError(400, "invalid_request", "请求行格式不对") from exc
        headers: dict[str, str] = {}
        for _ in range(_MAX_HEADERS):
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            if line in (b"\r\n", b"\n", b""):
                break
            name, sep, value = line.decode("latin-1").partition(":")
            if not sep:
                raise _HttpError(400, "invalid_request", "请求头格式不对")
            headers[name.strip().lower()] = value.strip()
        else:
            raise _HttpError(400, "invalid_request", "请求头行数过多")

        raw_length = headers.get("content-length", "0")
        try:
            length = int(raw_length or 0)
        except ValueError as exc:
            raise _HttpError(400, "invalid_request", "Content-Length 非法") from exc
        if length < 0 or length > self.config.max_body_bytes:
            raise _HttpError(413, "too_large", "请求体超过上限")
        body = await asyncio.wait_for(reader.readexactly(length), timeout=30) if length else b""
        return method.upper(), target, headers, body

    async def _route(
        self, request: tuple[str, str, dict[str, str], bytes]
    ) -> tuple[int, dict[str, Any]]:
        """按方法/路径分发。"""
        method, target, _headers, body = request
        path = target.split("?", 1)[0]
        if method == "GET" and path == "/health":
            ready = await self.backend.health()
            return 200, {
                "ready": bool(ready),
                "version": self.config.version,
                "max_inflight": self.config.max_inflight,
                "quota_active": self.quota.active,
                "quota_active_cost": self.quota.active_cost,
            }
        if method == "POST" and path == "/v1/quota/acquire":
            try:
                data = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise _HttpError(400, "invalid_request", f"非法 JSON：{exc}") from exc
            if not isinstance(data, dict):
                raise _HttpError(400, "invalid_request", "请求体必须是 JSON 对象")
            try:
                cost = float(data.get("cost") or 1.0)
                lease_ms = int(data.get("lease_ms") or 180_000)
            except (TypeError, ValueError) as exc:
                raise _HttpError(400, "invalid_request", "cost/lease_ms 非法") from exc
            try:
                lease = self.quota.acquire(
                    tenant_id=str(data.get("tenant_id") or "default"),
                    skill=str(data.get("skill") or "unknown"),
                    cost=cost,
                    lease_ms=lease_ms,
                )
            except QuotaExceeded as exc:
                raise _HttpError(429, "quota_exceeded", str(exc)) from exc
            return 200, {
                "lease_id": lease.lease_id,
                "expires_in_ms": max(
                    0, int((lease.expires_at - time.monotonic()) * 1000)
                ),
                "cost": lease.cost,
            }
        if method == "POST" and path == "/v1/quota/release":
            try:
                data = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise _HttpError(400, "invalid_request", f"非法 JSON：{exc}") from exc
            lease_id = str(data.get("lease_id") or "") if isinstance(data, dict) else ""
            if not lease_id:
                raise _HttpError(400, "invalid_request", "缺少 lease_id")
            return 200, {
                "released": self.quota.release(lease_id),
                "lease_id": lease_id,
            }
        if method == "POST" and path.startswith("/v1/exec/") and path.endswith("/cancel"):
            execution_id = path[len("/v1/exec/"):-len("/cancel")]
            if not execution_id:
                raise _HttpError(400, "invalid_request", "缺少执行 id")
            cancel = getattr(self.backend, "cancel", None)
            cancelled = bool(await cancel(execution_id)) if cancel else False
            return 200, {"cancelled": cancelled, "execution_id": execution_id}
        if method != "POST" or path != "/v1/exec":
            raise _HttpError(404, "not_found", f"未知路径：{method} {path}")

        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _HttpError(400, "invalid_request", f"请求体不是合法 JSON：{exc}") from exc
        spec = _validate_spec(data, self.config)

        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=_QUEUE_WAIT_SEC)
        except asyncio.TimeoutError as exc:
            raise _HttpError(503, "sandbox_capacity", "沙箱并发已满，请退避重试") from exc
        try:
            guard = (
                spec.timeout_ms + self.config.startup_budget_ms
            ) / 1000.0 + 5.0
            try:
                result = await asyncio.wait_for(self.backend.run(spec), timeout=guard)
            except asyncio.TimeoutError as exc:
                cancel = getattr(self.backend, "cancel", None)
                if cancel and spec.request_id:
                    await cancel(spec.request_id)
                raise _HttpError(504, "sandbox_timeout", "沙箱执行超时") from exc
            except SandboxError as exc:
                status = 400 if exc.code == "image_unknown" else 503
                raise _HttpError(status, exc.code, str(exc)) from exc
            return 200, _result_json(result)
        finally:
            self._semaphore.release()

    @staticmethod
    async def _write_response(
        writer: asyncio.StreamWriter, status: int, payload: dict[str, Any]
    ) -> None:
        """写 JSON 响应。"""
        reasons = {
            200: "OK",
            400: "Bad Request",
            404: "Not Found",
            408: "Request Timeout",
            413: "Payload Too Large",
            429: "Too Many Requests",
            500: "Internal Server Error",
            503: "Service Unavailable",
            504: "Gateway Timeout",
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        head = (
            f"HTTP/1.1 {status} {reasons.get(status, 'Error')}\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("latin-1")
        writer.write(head + body)
        try:
            await writer.drain()
        except (ConnectionError, OSError):
            pass


def _result_json(result: SandboxResult) -> dict[str, Any]:
    """``SandboxResult`` → JSON。"""
    return asdict(result)


async def serve(
    config: DispatcherConfig | None = None,
    backend: SandboxBackend | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    """启动并一直跑，直到收到停机信号。

    Args:
        config: 配置；不给就从环境变量读。
        backend: 后端；不给就用 Docker。
        stop_event: 外部停机信号（测试用）。
    """
    from services.dispatcher.docker_backend import DockerBackend

    cfg = config or DispatcherConfig.from_env()
    cfg.validate()
    be = backend if backend is not None else DockerBackend(cfg)
    prepare = getattr(be, "prepare", None)
    if prepare is not None:
        await prepare()
    server = DispatcherServer(cfg, be)
    await server.start()
    logger.info(
        "dispatcher 已启动：%s:%s（镜像目录 %s，并发 %s）",
        cfg.host,
        server.port,
        "、".join(name for name, _ in cfg.images) or "（空）",
        cfg.max_inflight,
    )

    stopping = stop_event if stop_event is not None else asyncio.Event()
    if stop_event is None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stopping.set)
            except (NotImplementedError, RuntimeError, ValueError):
                pass
    try:
        await stopping.wait()
    finally:
        await server.close()
        logger.info("dispatcher 已关闭")
