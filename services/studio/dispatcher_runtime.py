"""studio → dispatcher 的客户端（skill-service §2.4）。

为什么不用 httpx：studio 镜像**只装 grpcio + protobuf**（§12.1 的原则）。为了一个
内部 POST 引入一个 HTTP 客户端不划算，所以这里用标准库的 ``asyncio.open_connection``
手写一个很小的 HTTP/1.1 客户端——请求只有一种形状，响应也只有 JSON。

dispatcher 不可达时返回 ``status=failed`` 的结果（而不是抛 gRPC 错误）：这次执行
已经被接受了，模型应该看到「强隔离暂不可用」，而不是整个工具调用变成服务故障。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import quote

from services.studio.executor import (
    STATUS_FAILED,
    ExecOutcome,
)
from services.studio.launcher import ExecSpec

logger = logging.getLogger(__name__)

# 连 dispatcher 的建连超时（秒）。
_CONNECT_TIMEOUT = 5.0

# 响应头最多多少行。
_MAX_HEADERS = 64


class DispatcherRuntime:
    """把一次执行 POST 给 dispatcher。"""

    def __init__(self, addr: str) -> None:
        """记住地址。

        Args:
            addr: ``host:port``，如 ``studio-dispatcher:9100``。
        """
        self.addr = (addr or "").strip()
        host, _, port = self.addr.rpartition(":")
        self.host = host
        try:
            self.port = int(port)
        except ValueError:
            self.port = 0

    async def run(self, spec: ExecSpec) -> ExecOutcome:
        """在一次性沙箱里跑一次。

        Args:
            spec: 执行输入。

        Returns:
            :class:`~services.studio.executor.ExecOutcome`。
        """
        if not self.host or not self.port:
            return _failed(
                f"沙箱调度器地址不合法：{self.addr!r}",
                "sandbox_unavailable",
            )
        payload = {
            "skill": spec.skill,
            "tenant_id": spec.tenant_id,
            "script": spec.script,
            "language": spec.language,
            "content": spec.content,
            "argv": list(spec.argv),
            "stdin": spec.stdin,
            "timeout_ms": spec.timeout_ms,
            "image": spec.image,
            "request_id": spec.request_id,
            "traceparent": spec.traceparent,
        }
        timeout = max(5.0, (spec.timeout_ms or 60_000) / 1000.0 + 30.0)
        try:
            status, data = await self._post("/v1/exec", payload, timeout)
        except (OSError, asyncio.TimeoutError) as exc:
            logger.warning("连不上沙箱调度器（%s）：%s", self.addr, exc)
            return _failed(
                f"连不上沙箱调度器（{self.addr}）：{exc}",
                "sandbox_unavailable",
            )
        except Exception as exc:  # noqa: BLE001 - 协议坏了也要回灌，不炸节点
            logger.warning("沙箱调度器响应异常（%s）：%s", self.addr, exc)
            return _failed(f"沙箱调度器响应异常：{exc}", "sandbox_protocol")

        if status != 200:
            return _failed(
                str(data.get("error") or f"dispatcher 返回 {status}"),
                str(data.get("error_code") or "sandbox_error"),
            )
        return _to_outcome(data)

    async def close(self) -> None:
        """没有持久连接要关。"""
        return None

    async def cancel(self, request_id: str) -> bool:
        """通知 dispatcher 立即强删对应容器。"""
        if not request_id:
            return False
        try:
            status, data = await self._request(
                "POST",
                f"/v1/exec/{quote(request_id, safe='')}/cancel",
                {},
                5.0,
            )
        except Exception:
            logger.warning("取消沙箱请求失败：%s", request_id, exc_info=True)
            return False
        return status == 200 and bool(data.get("cancelled"))

    async def acquire_quota(
        self,
        *,
        tenant_id: str,
        skill: str,
        cost: float,
        lease_ms: int,
    ) -> str:
        """向共享 dispatcher 申请跨实例租约。

        Returns:
            ``lease_id``；调用方必须在执行结束后 release。

        Raises:
            RuntimeError: dispatcher 拒绝或不可达。
        """
        status, data = await self._post(
            "/v1/quota/acquire",
            {
                "tenant_id": tenant_id or "default",
                "skill": skill or "unknown",
                "cost": max(0.001, float(cost)),
                "lease_ms": max(1000, int(lease_ms)),
            },
            timeout=5.0,
        )
        if status != 200:
            code = str(data.get("error_code") or "quota_unavailable")
            detail = str(data.get("error") or f"dispatcher 返回 {status}")
            raise RuntimeError(f"{code}: {detail}")
        lease_id = str(data.get("lease_id") or "")
        if not lease_id:
            raise RuntimeError("quota_protocol: dispatcher 没有返回 lease_id")
        return lease_id

    async def release_quota(self, lease_id: str) -> bool:
        """释放共享租约。"""
        if not lease_id:
            return False
        try:
            status, data = await self._post(
                "/v1/quota/release", {"lease_id": lease_id}, timeout=5.0
            )
        except Exception:
            logger.warning("释放 dispatcher 配额失败：%s", lease_id, exc_info=True)
            return False
        return status == 200 and bool(data.get("released"))

    async def health(self, timeout: float = 2.0) -> bool:
        """dispatcher 是否就绪（启动期用）。

        Args:
            timeout: 超时（秒）。

        Returns:
            ``GET /health`` 返回 ready=True 时为 True；连不上/解析失败为 False。
        """
        try:
            status, data = await self._request("GET", "/health", None, timeout)
        except Exception:  # noqa: BLE001 - 探活失败就是不可用
            return False
        return status == 200 and bool(data.get("ready"))

    async def _post(
        self, path: str, payload: dict[str, Any], timeout: float
    ) -> tuple[int, dict[str, Any]]:
        """发一个 POST，返回 ``(状态码, JSON)``。"""
        return await self._request("POST", path, payload, timeout)

    async def _request(
        self, method: str, path: str, payload: dict[str, Any] | None, timeout: float
    ) -> tuple[int, dict[str, Any]]:
        """发一个 HTTP 请求，返回 ``(状态码, JSON)``。"""
        body = (
            b""
            if payload is None
            else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )
        headers = bytearray()
        headers.extend(f"{method} {path} HTTP/1.1\r\n".encode("latin-1"))
        headers.extend(f"Host: {self.addr}\r\n".encode("latin-1"))
        if payload is not None:
            headers.extend(
                b"Content-Type: application/json; charset=utf-8\r\n"
            )
        headers.extend(f"Content-Length: {len(body)}\r\n".encode("latin-1"))
        if isinstance(payload, dict) and payload.get("traceparent"):
            headers.extend(
                f"traceparent: {payload['traceparent']}\r\n".encode("latin-1")
            )
        headers.extend(b"Connection: close\r\n\r\n")
        request = bytes(headers) + body

        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=_CONNECT_TIMEOUT
        )
        try:
            writer.write(request)
            await writer.drain()

            status_line = await asyncio.wait_for(reader.readline(), timeout=timeout)
            if not status_line:
                raise ValueError("dispatcher 没有返回状态行")
            parts = status_line.decode("latin-1").strip().split(" ", 2)
            status = int(parts[1])

            headers: dict[str, str] = {}
            for _ in range(_MAX_HEADERS):
                line = await asyncio.wait_for(reader.readline(), timeout=timeout)
                if line in (b"\r\n", b"\n", b""):
                    break
                name, sep, value = line.decode("latin-1").partition(":")
                if sep:
                    headers[name.strip().lower()] = value.strip()
            length = int(headers.get("content-length", "0") or 0)
            raw = await asyncio.wait_for(reader.readexactly(length), timeout=timeout) if length else b""
            data = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(data, dict):
                raise ValueError("dispatcher 的响应不是 JSON 对象")
            return status, data
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass


def _failed(message: str, code: str) -> ExecOutcome:
    """造一个「没跑成」的结果。"""
    return ExecOutcome(
        status=STATUS_FAILED,
        exit_code=-1,
        stdout="",
        stderr="",
        truncated=False,
        duration_ms=0,
        error=message,
        stdout_digest="",
        error_code=code,
    )


def _to_outcome(data: dict[str, Any]) -> ExecOutcome:
    """dispatcher 的 JSON → ``ExecOutcome``。"""
    status = str(data.get("status") or "failed")
    if status not in ("completed", "failed", "timeout", "limited", "cancelled"):
        status = "failed"
    return ExecOutcome(
        status=status,
        exit_code=int(data.get("exit_code", -1)),
        stdout=str(data.get("stdout") or ""),
        stderr=str(data.get("stderr") or ""),
        truncated=bool(data.get("truncated", False)),
        duration_ms=int(data.get("duration_ms", 0)),
        error=str(data.get("error") or ""),
        stdout_digest=str(data.get("stdout_digest") or ""),
        startup_ms=int(data.get("startup_ms", 0)),
        image_startup_ms=int(data.get("image_startup_ms", 0)),
        cpu_seconds=float(data.get("cpu_seconds") or 0),
        peak_memory_bytes=int(data.get("peak_memory_bytes") or 0),
        error_code=str(data.get("error_code") or ""),
    )
