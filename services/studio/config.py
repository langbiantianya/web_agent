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


def _env_list(name: str) -> tuple[str, ...]:
    """读一个逗号分隔的环境变量，当成一组字符串。

    Args:
        name: 变量名。

    Returns:
        去空、去重后的元组。
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return ()
    seen: list[str] = []
    for item in raw.split(","):
        cleaned = item.strip()
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return tuple(seen)


def _env_pairs(name: str) -> tuple[tuple[str, str], ...]:
    """读 ``name=ref,name=ref`` 形式的环境变量。

    镜像目录用这个名字对形式：``default=registry/x@sha256:...``。名字是调用方
    能请求的**目录名**，ref 才是真正给容器运行时看的完整地址——调用方永远
    拿不到完整 ref，也就不能自己指定镜像。

    Args:
        name: 变量名。

    Returns:
        ``((name, ref), ...)``；格式不合法的项直接丢掉，不会把坏配置带进目录。
    """
    out: list[tuple[str, str]] = []
    for item in _env_list(name):
        key, sep, value = item.partition("=")
        if sep and key.strip() and value.strip():
            out.append((key.strip(), value.strip()))
    return tuple(out)


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


class StudioConfigError(Exception):
    """配置不自洽——启动期直接失败，不要等第一次调用才炸。"""


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
        metrics_port: 指标端点的端口（§12.1）；``0`` 表示不启用。
        oom_score_adj: 脚本进程的 ``oom_score_adj``（§13.2）。越大越容易被
            OOM killer 挑中——设了它，容器内存被打爆时被杀的是**脚本**而不是
            studio 自己。没权限设（没有 ``CAP_SYS_RESOURCE``）也不致命。
        profile: 本实例的**默认**隔离档（§2.4）。当前只完整实现了 ``fast``。
        isolated_skill_codes: 走 ``isolated`` 档的技能 code（§2.4「按 skill
            code 列表」）。这些技能有**自己的**并发池与等待队列（§8.4）。
        isolated_max_concurrency: ``isolated`` 档自己的并发上限（§8.4）。
        isolated_max_queue: ``isolated`` 档自己的等待队列深度（§8.4）。
        isolated_dispatcher_addr: 沙箱调度器的地址；空 = 本实例没有 isolated
            运行时。没有它时 isolated 档必须**明确不可用**，不能悄悄退回 fast。
        isolated_images: 镜像目录 ``((目录名, image@digest), ...)``。调用方只能
            按目录名请求，不能自己指定完整 ref。
        isolated_startup_budget_ms: 沙箱启动预算（毫秒）。启动慢于这个值就报
            ``sandbox_start_timeout``，不要让它把脚本时限耗光。
        version: 服务版本，报在 ``HealthResponse`` 里。请求带档位、响应回显档位
            是 2.0.0 起的语义，``STUDIO_EXEC_MIN_VERSION`` 据此卡。
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
    metrics_port: int = 9090
    oom_score_adj: int = 1000
    profile: str = "fast"
    isolated_skill_codes: tuple[str, ...] = ()
    isolated_max_concurrency: int = 2
    isolated_max_queue: int = 4
    isolated_dispatcher_addr: str = ""
    isolated_images: tuple[tuple[str, str], ...] = ()
    isolated_startup_budget_ms: int = 15000
    version: str = "2.0.0"
    languages: tuple[str, ...] = field(default_factory=detect_languages)

    def image_ref(self, name: str) -> str | None:
        """按目录名取完整镜像地址。

        Args:
            name: 目录名（调用方请求里带的那个）。

        Returns:
            完整 ref；目录里没有这个名字时返回 ``None``。
        """
        for key, ref in self.isolated_images:
            if key == name:
                return ref
        return None

    def validate(self) -> None:
        """启动期校验配置，不自洽就**直接失败**。

        为什么不在每次调用时报错：配置错了是部署问题，应该在启动时暴露
        （与 §22.8.6 的口径一致）。悄悄把 isolated 请求跑成 fast 是最坏的
        降级——调用方以为拿到了强隔离，实际没有，而且没有痕迹。

        Raises:
            StudioConfigError: 档位名不认识，或配了 isolated 却没配运行时。
        """
        if self.profile not in ("fast", "isolated"):
            raise StudioConfigError(
                f"STUDIO_PROFILE 只能是 fast 或 isolated（当前 {self.profile!r}）"
            )
        if (self.isolated_skill_codes or self.isolated_images) and not (
            self.isolated_dispatcher_addr
        ):
            raise StudioConfigError(
                "配了 isolated 技能/镜像，但没有配 STUDIO_ISOLATED_DISPATCHER_ADDR。"
                "这会让 isolated 请求全部失败——要么补上调度器地址，要么清空这两个配置。"
            )
        for name, ref in self.isolated_images:
            if not name or not ref:
                raise StudioConfigError("STUDIO_ISOLATED_IMAGES 里有空的目录名或镜像地址")

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
            metrics_port=_env_int("STUDIO_METRICS_PORT", 9090),
            oom_score_adj=_env_int("STUDIO_OOM_SCORE_ADJ", 1000),
            profile=os.environ.get("STUDIO_PROFILE", "fast").strip() or "fast",
            isolated_skill_codes=_env_list("STUDIO_ISOLATED_SKILL_CODES"),
            isolated_max_concurrency=_env_int("STUDIO_ISOLATED_MAX_CONCURRENCY", 2),
            isolated_max_queue=_env_int("STUDIO_ISOLATED_MAX_QUEUE", 4),
            isolated_dispatcher_addr=(
                os.environ.get("STUDIO_ISOLATED_DISPATCHER_ADDR", "").strip()
            ),
            isolated_images=_env_pairs("STUDIO_ISOLATED_IMAGES"),
            isolated_startup_budget_ms=_env_int(
                "STUDIO_ISOLATED_STARTUP_BUDGET_MS", 15000
            ),
            version=os.environ.get("STUDIO_VERSION", "2.0.0").strip() or "2.0.0",
        )
