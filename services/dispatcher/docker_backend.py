"""Docker 后端：每个请求起一个一次性容器（skill-service §2.4、§7、§9）。

**危险的东西只在这里**：dispatcher 持有 Docker socket，但从不执行 ``content``——
它只按固定模板 create / start / logs / remove。做决定的不是调用方：
镜像、网络、capabilities、资源上限全部来自服务端模板。

沙箱镜像的入口是 ``python -m services.sandbox.runner``；payload 由 dispatcher 写在
一个对 Docker daemon 可见的目录里，再以**只读 bind mount** 挂到
``/run/sandbox/payload.json``（容器 rootfs 是只读的，所以不能用 put_archive）。
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import tempfile
import threading
import time
from pathlib import Path

from services.dispatcher.backend import SandboxError
from services.dispatcher.config import DispatcherConfig
from services.sandbox.protocol import (
    MAX_FRAME_BYTES,
    SandboxResult,
    SandboxSpec,
    build_payload,
    parse_frames,
)

# 容器内的 payload 路径（与 runner 的默认值一致）。
_PAYLOAD_DIR = "/run/sandbox"
_PAYLOAD_NAME = "payload.json"

# 一次沙箱的固定资源上限（§13.1）。请求不能改这些值。
_MEMORY_LIMIT = "512m"
_CPU_LIMIT = 1_000_000_000  # nano_cpus = 1 CPU
_PIDS_LIMIT = 256
_TMPFS_SIZE = "64m"

# 容器环境变量白名单（§6.3）——不放任何调用方给的东西。
_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/tmp",
    "LANG": "C.UTF-8",
    "TZ": "UTC",
    "PYTHONUNBUFFERED": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
}


class DockerBackend:
    """用 Docker 起一次性沙箱。"""

    def __init__(self, config: DispatcherConfig) -> None:
        """记住配置；Docker 客户端懒创建。

        Args:
            config: dispatcher 配置。
        """
        self.config = config
        self._client = None
        self._lock = threading.Lock()

    def _docker(self):  # noqa: ANN202 - 返回 docker.DockerClient
        """懒创建 Docker 客户端。

        Raises:
            SandboxError: 没装 docker SDK，或连不上 Docker。
        """
        try:
            import docker
        except ImportError as exc:  # pragma: no cover - 镜像里一定装了
            raise SandboxError(
                "dispatcher 镜像里没有 docker SDK", code="sandbox_unavailable"
            ) from exc
        with self._lock:
            if self._client is None:
                try:
                    self._client = docker.DockerClient(
                        base_url=self.config.docker_socket, version="auto"
                    )
                except Exception as exc:  # noqa: BLE001
                    raise SandboxError(
                        f"连不上 Docker（{self.config.docker_socket}）：{exc}",
                        code="sandbox_unavailable",
                    ) from exc
            return self._client

    async def health(self) -> bool:
        """Docker 是否可达。"""
        try:
            return bool(await asyncio.to_thread(lambda: self._docker().ping()))
        except Exception:  # noqa: BLE001
            return False

    async def close(self) -> None:
        """关掉客户端。"""
        client = self._client
        self._client = None
        if client is not None:
            try:
                await asyncio.to_thread(client.close)
            except Exception:  # noqa: BLE001
                pass

    async def run(self, spec: SandboxSpec) -> SandboxResult:
        """在一次性容器里跑一次。

        Args:
            spec: 执行输入。

        Returns:
            :class:`~services.sandbox.protocol.SandboxResult`。

        Raises:
            SandboxError: 镜像不在目录里 / Docker 不可用 / 起不来。
        """
        return await asyncio.to_thread(self._run_sync, spec)

    def _run_sync(self, spec: SandboxSpec) -> SandboxResult:
        """同步实现（在线程里跑，避免堵事件循环）。"""
        image_name = (spec.image or "default").strip()
        image_ref = self.config.image_ref(image_name)
        if image_ref is None:
            raise SandboxError(
                f"镜像目录里没有「{image_name}」", code="image_unknown"
            )
        timeout_ms = min(
            int(spec.timeout_ms or self.config.max_timeout_ms),
            self.config.max_timeout_ms,
        )
        timeout_sec = max(1.0, timeout_ms / 1000.0)
        nonce = secrets.token_hex(16)
        payload = build_payload(spec, nonce)
        client = self._docker()
        container = None
        exec_dir: Path | None = None
        started_at = time.monotonic()
        startup_ms = 0
        timed_out = False
        try:
            # payload 写在 dispatcher 的目录里，再以**只读 bind mount** 挂进沙箱。
            # 这个目录必须对 Docker daemon 可见（lab 里从宿主机 bind mount 进来）；
            # 为什么不用 put_archive：read-only rootfs 下写容器文件系统会被 Docker 拒。
            payload_dir = Path(self.config.payload_dir)
            payload_dir.mkdir(parents=True, exist_ok=True)
            exec_dir = Path(tempfile.mkdtemp(prefix="sbx-", dir=payload_dir))
            (exec_dir / _PAYLOAD_NAME).write_bytes(payload)
            os.chmod(exec_dir / _PAYLOAD_NAME, 0o644)

            try:
                container = client.containers.create(
                    image_ref,
                    command=["python", "-m", "services.sandbox.runner"],
                    network_disabled=True,
                    read_only=True,
                    user="1000:1000",
                    working_dir="/tmp",
                    cap_drop=["ALL"],
                    security_opt=["no-new-privileges"],
                    pids_limit=_PIDS_LIMIT,
                    mem_limit=_MEMORY_LIMIT,
                    nano_cpus=_CPU_LIMIT,
                    tmpfs={"/tmp": f"rw,size={_TMPFS_SIZE}"},
                    volumes={
                        str(exec_dir): {"bind": _PAYLOAD_DIR, "mode": "ro"}
                    },
                    environment=dict(_ENV),
                    labels={
                        "studio.io/owner": "dispatcher",
                        "studio.io/request_id": (spec.request_id or "")[:64],
                    },
                )
            except Exception as exc:  # noqa: BLE001
                raise SandboxError(
                    f"创建沙箱失败：{exc}", code="sandbox_start_failed"
                ) from exc

            try:
                container.start()
            except Exception as exc:  # noqa: BLE001
                raise SandboxError(
                    f"启动沙箱失败：{exc}", code="sandbox_start_failed"
                ) from exc
            startup_ms = int((time.monotonic() - started_at) * 1000)

            # 启动时间也算在调用方的时限里（§8.1「从发出算起」）。
            remaining = max(1.0, timeout_sec - startup_ms / 1000.0)
            try:
                container.wait(timeout=remaining)
            except Exception as exc:  # noqa: BLE001 - 可能是超时，也可能是 API 错误
                elapsed = time.monotonic() - started_at
                if elapsed >= timeout_sec - 0.5:
                    timed_out = True
                else:
                    raise SandboxError(
                        f"等待沙箱失败：{exc}", code="sandbox_wait_failed"
                    ) from exc

            state = (container.attrs or {}).get("State", {}) or {}
            logs = b""
            try:
                logs = container.logs(stdout=True, stderr=False)
            except Exception:  # noqa: BLE001
                logs = b""
            if len(logs) > MAX_FRAME_BYTES:
                raise SandboxError(
                    "沙箱控制帧超过上限", code="sandbox_result_lost"
                )

            exit_code = int(state.get("ExitCode", -1))
            oom = bool(state.get("OOMKilled", False))
            elapsed_ms = int((time.monotonic() - started_at) * 1000)

            if timed_out:
                return SandboxResult(
                    status="timeout",
                    exit_code=-1,
                    error=f"沙箱执行超时（{timeout_ms} 毫秒），已删除容器",
                    error_code="sandbox_timeout",
                    duration_ms=elapsed_ms,
                    startup_ms=startup_ms,
                    sandbox_id=(container.id or "")[:12],
                )
            if oom:
                return SandboxResult(
                    status="limited",
                    exit_code=-1,
                    error="沙箱内存超出限额（OOMKilled）",
                    error_code="sandbox_oom",
                    duration_ms=elapsed_ms,
                    startup_ms=startup_ms,
                    sandbox_id=(container.id or "")[:12],
                )
            try:
                parsed = parse_frames(logs.decode("utf-8", errors="replace"))
            except Exception as exc:  # noqa: BLE001
                try:
                    stderr_logs = container.logs(stdout=False, stderr=True)
                except Exception:  # noqa: BLE001
                    stderr_logs = b""
                return SandboxResult(
                    status="failed",
                    exit_code=exit_code,
                    stdout="",
                    stderr=(stderr_logs or logs)[:2000].decode(
                        "utf-8", errors="replace"
                    ),
                    error=f"沙箱没有返回可解析的结果：{exc}",
                    error_code="sandbox_result_lost",
                    duration_ms=elapsed_ms,
                    startup_ms=startup_ms,
                    sandbox_id=(container.id or "")[:12],
                )

            status = str(parsed.get("status") or "completed")
            error_code = str(parsed.get("error_code") or "")
            exit_code = int(parsed.get("exit_code", exit_code))
            if status == "completed" and exit_code in (-9, 137):
                status = "limited"
                error_code = error_code or "sandbox_signal"
            return SandboxResult(
                status=status,
                exit_code=exit_code,
                stdout=parsed["stdout"].decode("utf-8", errors="replace"),
                stderr=parsed["stderr"].decode("utf-8", errors="replace"),
                truncated=bool(parsed.get("truncated")),
                duration_ms=int(parsed.get("duration_ms") or elapsed_ms),
                error=(
                    "沙箱执行超时"
                    if status == "timeout"
                    else "沙箱输出超过上限"
                    if status == "limited" and error_code == "output_limit"
                    else "沙箱被信号杀掉"
                    if status == "limited" and error_code == "sandbox_signal"
                    else ""
                ),
                error_code=error_code,
                startup_ms=startup_ms,
                sandbox_id=(container.id or "")[:12],
            )
        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception:  # noqa: BLE001
                    pass
            if exec_dir is not None:
                shutil.rmtree(exec_dir, ignore_errors=True)
