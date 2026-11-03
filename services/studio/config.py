"""studio 的服务侧配置（skill-service §8、§9）。

配置全部来自环境变量——studio 容器里**不注入任何凭据**，但它自己的运行参数
（并发上限、时限、输出上限）必须能配。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field


def _env_int(name: str, default: int) -> int:
    """读一个整数环境变量，坏了就用默认值。

    Args:
        name: 变量名。
        default: 默认值。

    Returns:
        整数取值。
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    """读一个浮点环境变量，坏了就用默认值。

    Args:
        name: 变量名。
        default: 默认值。

    Returns:
        浮点取值。
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def detect_languages() -> tuple[str, ...]:
    """探测本实例**真的**能跑哪些运行时（skill-service §10.1、§10.2）。

    只报 ``LanguageInfo.name``——版本会随镜像升级而漂移，调用方不应依赖具体
    版本号（§10.3）。

    Returns:
        可用运行时的名字，已排序。
    """
    found: list[str] = []
    if shutil.which("python3") or shutil.which("python"):
        found.append("python")
    if shutil.which("bash"):
        found.append("bash")
    if shutil.which("node"):
        found.append("node")
    return tuple(sorted(found))


@dataclass(frozen=True, slots=True)
class StudioConfig:
    """studio 的运行参数。

    Attributes:
        host: 监听地址。
        port: 监听端口。
        max_concurrency: 同时在跑的脚本数上限（§8.1）。
        max_queue: 等待队列深度；满了就拒绝（§8.1）。
        max_timeout_ms: 单次执行的时限上限——与调用方给的时限**取较小值**（§8.2）。
        max_stdout_bytes: stdout 上限；超限截断并**停止读取**（§9.2）。
        max_stderr_bytes: stderr 上限；同上。
        max_content_chars: 脚本正文上限（§9.1）。
        max_argv_items: 参数项数上限（§9.1）。
        max_argv_item_chars: 单个参数长度上限（§9.1）。
        max_stdin_chars: stdin 长度上限（§9.1）。
        dedup_window_sec: ``request_id`` 去重窗口（§5.2）。
        drain_timeout_sec: 优雅停机时等在跑的执行结束的秒数（§13.5）。
        oom_score_adj: 脚本进程的 ``oom_score_adj``（§13.2）。越大越容易被
            OOM killer 挑中——设了它，容器内存被打爆时被杀的是**脚本**而不是
            studio 自己。没权限设（没有 ``CAP_SYS_RESOURCE``）也不致命。
        profile: 隔离档（§2.4）。当前只实现 ``fast``。
        version: 服务版本，报在 ``HealthResponse`` 里。
        languages: 本实例支持的运行时。
    """

    host: str = "0.0.0.0"
    port: int = 9000
    max_concurrency: int = 4
    max_queue: int = 16
    max_timeout_ms: int = 60000
    max_stdout_bytes: int = 1024 * 1024
    max_stderr_bytes: int = 256 * 1024
    max_content_chars: int = 200 * 1024
    max_argv_items: int = 64
    max_argv_item_chars: int = 4096
    max_stdin_chars: int = 256 * 1024
    dedup_window_sec: float = 60.0
    drain_timeout_sec: float = 30.0
    oom_score_adj: int = 1000
    profile: str = "fast"
    version: str = "1.0.0"
    languages: tuple[str, ...] = field(default_factory=detect_languages)

    @classmethod
    def from_env(cls) -> "StudioConfig":
        """从环境变量构造。

        Returns:
            配置实例。
        """
        return cls(
            host=os.environ.get("STUDIO_HOST", "0.0.0.0").strip() or "0.0.0.0",
            port=_env_int("STUDIO_PORT", 9000),
            max_concurrency=_env_int("STUDIO_MAX_CONCURRENCY", 4),
            max_queue=_env_int("STUDIO_MAX_QUEUE", 16),
            max_timeout_ms=_env_int("STUDIO_MAX_TIMEOUT_MS", 60000),
            max_stdout_bytes=_env_int("STUDIO_MAX_STDOUT_BYTES", 1024 * 1024),
            max_stderr_bytes=_env_int("STUDIO_MAX_STDERR_BYTES", 256 * 1024),
            max_content_chars=_env_int("STUDIO_MAX_CONTENT_CHARS", 200 * 1024),
            max_argv_items=_env_int("STUDIO_MAX_ARGV_ITEMS", 64),
            max_argv_item_chars=_env_int("STUDIO_MAX_ARGV_ITEM_CHARS", 4096),
            max_stdin_chars=_env_int("STUDIO_MAX_STDIN_CHARS", 256 * 1024),
            dedup_window_sec=_env_float("STUDIO_DEDUP_WINDOW_SEC", 60.0),
            drain_timeout_sec=_env_float("STUDIO_DRAIN_TIMEOUT_SEC", 30.0),
            oom_score_adj=_env_int("STUDIO_OOM_SCORE_ADJ", 1000),
            profile=os.environ.get("STUDIO_PROFILE", "fast").strip() or "fast",
            version=os.environ.get("STUDIO_VERSION", "1.0.0").strip() or "1.0.0",
        )
