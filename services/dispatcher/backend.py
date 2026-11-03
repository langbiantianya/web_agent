"""dispatcher 的沙箱后端接口（skill-service §2.4）。

后端只做「起一个沙箱、等结果、清理」；它**不执行 content**。v1 实现是 Docker
（``docker_backend.py``），测试用假后端。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from services.sandbox.protocol import SandboxResult, SandboxSpec


class SandboxError(Exception):
    """沙箱起不来 / 镜像不在目录里 / 后端不可用。

    ``code`` 是机器可读的失败码（``image_unknown`` / ``sandbox_unavailable`` …），
    由 HTTP 层放进 JSON 响应的 ``error_code``。
    """

    def __init__(self, message: str, code: str = "sandbox_error") -> None:
        """记住说明与失败码。"""
        super().__init__(message)
        self.code = code


@runtime_checkable
class SandboxBackend(Protocol):
    """一次性沙箱后端。"""

    async def run(self, spec: SandboxSpec) -> SandboxResult:
        """在沙箱里跑一次。

        Args:
            spec: 执行输入。

        Returns:
            :class:`~services.sandbox.protocol.SandboxResult`。

        Raises:
            SandboxError: 镜像不在目录里、Docker 不可用等。
        """
        ...

    async def health(self) -> bool:
        """后端是否就绪。"""
        ...

    async def close(self) -> None:
        """释放后端资源。"""
        ...
