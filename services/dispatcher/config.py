"""dispatcher 的配置（skill-service §2.4、§13）。

dispatcher 是**只调度、不执行**的控制面：它持有 Docker socket，但从不执行
``content``。所有沙箱参数都来自这里的固定模板，调用方只能按**目录名**请求镜像。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    """读整数环境变量；坏了用默认值。"""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_pairs(name: str) -> tuple[tuple[str, str], ...]:
    """读 ``name=ref,name=ref`` 形式的环境变量。"""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return ()
    out: list[tuple[str, str]] = []
    for item in raw.split(","):
        key, sep, value = item.strip().partition("=")
        if sep and key.strip() and value.strip():
            out.append((key.strip(), value.strip()))
    return tuple(out)


def _env_bool(name: str, default: bool) -> bool:
    """读取常见布尔环境变量。"""
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


class DispatcherConfigError(Exception):
    """配置不自洽——启动期直接失败。"""


@dataclass(frozen=True, slots=True)
class DispatcherConfig:
    """dispatcher 的运行参数。

    Attributes:
        host: 监听地址。
        port: 监听端口。
        max_inflight: 同时存在的沙箱数上限。
        max_timeout_ms: 单次执行时限上限（调用方给的时限会被压到它）。
        startup_budget_ms: 沙箱启动预算（调度 + 拉镜像 + 启动）。
        images: 镜像目录 ``((目录名, image@digest), ...)``。
        docker_socket: Docker Engine 的地址。
        payload_dir: 写 payload 的目录。它必须**对 Docker daemon 可见**（lab 里
            从宿主机 bind mount 进来），沙箱以只读方式挂到 ``/run/sandbox``。
        version: 服务版本。
        max_body_bytes: HTTP 请求体上限。
    """

    host: str = "0.0.0.0"
    port: int = 9100
    max_inflight: int = 2
    max_timeout_ms: int = 60_000
    startup_budget_ms: int = 15_000
    images: tuple[tuple[str, str], ...] = ()
    docker_socket: str = "unix:///var/run/docker.sock"
    payload_dir: str = "/var/lib/studio-payloads"
    version: str = "1.0.0"
    max_body_bytes: int = 2 * 1024 * 1024
    # 生产默认要求镜像钉到 sha256 digest；本地刚 build 的 tag 可显式关闭。
    require_digest: bool = True

    def image_ref(self, name: str) -> str | None:
        """按目录名取完整镜像地址。"""
        for key, ref in self.images:
            if key == name:
                return ref
        return None

    def validate(self) -> None:
        """启动期校验。

        Raises:
            DispatcherConfigError: 没有镜像目录，或参数非法。
        """
        if self.port < 0 or self.port > 65535:
            raise DispatcherConfigError(f"端口不合法：{self.port}")
        if self.max_inflight <= 0:
            raise DispatcherConfigError("DISPATCHER_MAX_INFLIGHT 必须大于 0")
        if not self.images:
            raise DispatcherConfigError(
                "DISPATCHER_IMAGES 不能为空——没有任何镜像目录，isolated 起不来"
            )
        digest_re = re.compile(r"@sha256:[0-9a-f]{64}$")
        for name, ref in self.images:
            if not name or not ref:
                raise DispatcherConfigError("DISPATCHER_IMAGES 里有空的目录名或镜像地址")
            if self.require_digest and not digest_re.search(ref):
                raise DispatcherConfigError(
                    f"镜像「{name}」未固定 digest：{ref}。生产必须使用 "
                    "image@sha256:<64 hex>；仅本地构建可显式设 "
                    "DISPATCHER_REQUIRE_DIGEST=false。"
                )

    @classmethod
    def from_env(cls) -> "DispatcherConfig":
        """从环境变量构造。"""
        return cls(
            host=os.environ.get("DISPATCHER_HOST", "0.0.0.0").strip() or "0.0.0.0",
            port=_env_int("DISPATCHER_PORT", 9100),
            max_inflight=_env_int("DISPATCHER_MAX_INFLIGHT", 2),
            max_timeout_ms=_env_int("DISPATCHER_MAX_TIMEOUT_MS", 60_000),
            startup_budget_ms=_env_int("DISPATCHER_STARTUP_BUDGET_MS", 15_000),
            images=_env_pairs("DISPATCHER_IMAGES"),
            docker_socket=(
                os.environ.get(
                    "DISPATCHER_DOCKER_SOCKET", "unix:///var/run/docker.sock"
                ).strip()
                or "unix:///var/run/docker.sock"
            ),
            payload_dir=(
                os.environ.get(
                    "DISPATCHER_PAYLOAD_DIR", "/var/lib/studio-payloads"
                ).strip()
                or "/var/lib/studio-payloads"
            ),
            version=os.environ.get("DISPATCHER_VERSION", "1.0.0").strip() or "1.0.0",
            max_body_bytes=_env_int("DISPATCHER_MAX_BODY_BYTES", 2 * 1024 * 1024),
            require_digest=_env_bool("DISPATCHER_REQUIRE_DIGEST", True),
        )
