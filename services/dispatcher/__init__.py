"""studio-dispatcher：只调度、不执行的沙箱控制面（skill-service §2.4）。

它持有 Docker socket，但从不执行 ``content``；沙箱本身零凭据、禁出网、只读
rootfs、非 root。详见 ``docs/superpowers/plans/2026-11-03-isolated-exec.md``。
"""

from __future__ import annotations

from services.dispatcher.config import DispatcherConfig, DispatcherConfigError
from services.dispatcher.server import DispatcherServer, serve

__all__ = [
    "DispatcherConfig",
    "DispatcherConfigError",
    "DispatcherServer",
    "serve",
]
