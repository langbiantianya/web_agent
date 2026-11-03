"""脚本执行器（skill-service §6、§8、§9）。

## 一次执行的完整流程

```text
建临时目录 → 写脚本 → 建独立进程组 → 起进程
    ↓
并发闸门（§8.1）在**起进程之前**拿到
    ↓
读 stdout/stderr（各有上限，超限**停止读取**并杀掉进程组，§9.2）
    ↓
等进程退出 / 墙钟超时（到点杀进程组，§8.2）
    ↓
清理：杀进程组 → 删临时目录（**所有终态都做**，§4）
```

## 几条不能省的隔离

| 措施 | 挡什么 | 位置 |
| --- | --- | --- |
| 独立进程组（新 session） | 脚本再起子进程时，一网打尽 | §6.1 |
| 杀进程组而不是杀进程 | 孙进程继续跑、占内存、持有文件 | §6.1 |
| 独立临时工作目录 | 两次执行之间不留状态 | §6.2 |
| 环境变量白名单 | 调用方在请求里带的东西直接进环境 | §6.3 |
| ``RLIMIT_*`` | 写爆磁盘、fork 炸弹、core dump 泄露数据 | §6.4 |
| 输出上限 + 停止读取 | ``cat /dev/urandom`` 把内存吃光 | §9.2 |
| ``oom_score_adj`` 调高 | OOM killer 优先杀**脚本**而不是 studio | §13.2 |
| SIGKILL 退出码判定 | 被资源限制杀掉 ≠ 脚本跑完了 | §13.2 |

**容器级的加固（只读 rootfs、非 root、禁出网、零凭据）不在代码里**——那是在编排
平台的容器配置里做的（skill-service §7）。代码里的这些只是第二道。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from services.studio.config import StudioConfig
from services.studio.oom import (
    describe_sigkill,
    is_sigkilled,
    oom_killed_since,
    read_oom_kill_count,
    set_oom_score_adj,
)

logger = logging.getLogger(__name__)

# 执行结果的状态。与 proto 的 ExecStatus 一一对应，但用小写字符串——
# 服务内部不直接用 proto 枚举，免得把契约的数值写死在逻辑里。
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_TIMEOUT = "timeout"
STATUS_LIMITED = "limited"
STATUS_CANCELLED = "cancelled"

# 语言 → (解释器, 脚本文件名)。
_INTERPRETERS: dict[str, tuple[str, str]] = {
    "python": (sys.executable, "main.py"),
    "bash": ("bash", "main.sh"),
    "node": ("node", "main.js"),
}

# 启动失败的原因分类（skill-service §12.1 的 ``reason`` 取值）。
# 服务端的两个取值（``invalid_request`` / ``unknown_language``）在
# :mod:`services.studio.server` 里——那里才是判它们的地方。
FAILURE_RUNTIME_MISSING = "runtime_missing"
FAILURE_SPAWN_FAILED = "spawn_failed"
FAILURE_UNKNOWN_LANGUAGE = "unknown_language"

# 环境变量白名单（§6.3）。**其余一律不传**——studio 容器里本来也没有凭据，
# 但调用方在请求里带的东西也不能直接进环境。
_ENV_ALLOWLIST = ("PATH", "LANG", "TZ")


class OutputOverflow(Exception):
    """输出超过上限——**停止读取**并杀掉进程组（§9.2）。

    Attributes:
        data: 已经读到的（被截断的）字节。异常抛出去时不能把已读的部分丢掉——
            那正是模型最需要看到的那部分。
    """

    def __init__(self, data: bytes) -> None:
        """记住已读到的部分。

        Args:
            data: 已读到的字节。
        """
        super().__init__("脚本输出超过上限")
        self.data = data


@dataclass(frozen=True, slots=True)
class ExecOutcome:
    """一次执行的结果。

    Attributes:
        status: 终态（``completed`` / ``failed`` / ``timeout`` / ``limited`` /
            ``cancelled``）。
        exit_code: 脚本的退出码；非 ``completed`` 状态按契约是 ``-1``。
        stdout: 脚本的标准输出（**不写进日志**，只记字节数与摘要）。
        stderr: 脚本的标准错误（同上）。
        truncated: 输出是否被截断。
        duration_ms: 耗时（毫秒）。
        error: 服务级错误说明（不是脚本的 stderr）。
        stdout_digest: stdout 的 sha256 摘要（十六进制，不带算法前缀）。
        stdout_bytes: stdout 的**字节数**——日志只记它，不记内容（§12.2）。
        stderr_bytes: stderr 的**字节数**。
        failure_reason: 启动失败的原因分类（``runtime_missing`` /
            ``spawn_failed`` / ``unknown_language``）；没失败是空串。
        startup_ms: 沙箱启动耗时（毫秒）；fast 档恒为 0。
        error_code: 机器可读的失败码（``timeout`` / ``limited`` / ...），
            没有失败是空串。调用方按它分支，不靠匹配人话。
    """

    status: str
    exit_code: int
    stdout: str
    stderr: str
    truncated: bool
    duration_ms: int
    error: str
    stdout_digest: str
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    failure_reason: str = ""
    startup_ms: int = 0
    error_code: str = ""


def _build_env(cwd: str) -> dict[str, str]:
    """按白名单构造子进程的环境变量（§6.3）。

    Args:
        cwd: 临时工作目录（``HOME`` 指向它）。

    Returns:
        环境变量字典。
    """
    env: dict[str, str] = {}
    for key in _ENV_ALLOWLIST:
        value = os.environ.get(key)
        if value:
            env[key] = value
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    # 解释器自己的目录要在 PATH 里——否则 node 之类的可能找不到。
    env["PATH"] = os.pathsep.join(
        [str(Path(sys.executable).parent), env.get("PATH", "")]
    ).strip(os.pathsep)
    env["HOME"] = cwd
    env.setdefault("LANG", "C.UTF-8")
    env.setdefault("TZ", "UTC")
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _make_preexec(cpu_seconds: int, oom_score_adj: int):
    """造一个 ``preexec_fn``：在子进程里设资源限额（§6.4）。

    只有 POSIX 有 ``resource`` 模块——Windows 上的进程内限额做不到，容器级的
    cgroup 限额才是那道防线。

    Args:
        cpu_seconds: ``RLIMIT_CPU`` 的秒数。
        oom_score_adj: 脚本进程的 ``oom_score_adj``（§13.2）；越大越容易被
            OOM killer 挑中。设不上去也不致命——内核在同分时按内存占用挑，
            吃内存的脚本本来就是首选。

    Returns:
        ``preexec_fn``；不支持时返回 ``None``。
    """
    try:
        import resource
    except ImportError:  # pragma: no cover - Windows
        return None

    def _apply() -> None:  # pragma: no cover - 只在子进程里跑
        """在子进程里设限额。"""
        limits = (
            (resource.RLIMIT_FSIZE, 64 * 1024 * 1024),
            (resource.RLIMIT_NOFILE, 256),
            (resource.RLIMIT_CORE, 0),
            (resource.RLIMIT_CPU, cpu_seconds),
        )
        for what, value in limits:
            try:
                resource.setrlimit(what, (value, value))
            except (ValueError, OSError):
                pass
        try:
            resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
        except (ValueError, OSError):
            pass
        # 容器内存被打爆时，让内核**优先杀这个脚本**而不是 studio（§13.2）。
        set_oom_score_adj(oom_score_adj)

    return _apply


def _bytes_of(task) -> bytes:
    """从一个读取任务里取字节，异常时不炸。

    Args:
        task: 读取任务。

    Returns:
        读到的字节；任务没完成、已取消或抛了别的异常时返回空。
    """
    if not task.done() or task.cancelled():
        return b""
    exc = task.exception()
    if exc is None:
        return task.result()
    if isinstance(exc, OutputOverflow):
        return exc.data
    return b""


class ScriptRunner:
    """执行一段脚本。"""

    def __init__(self, config: StudioConfig) -> None:
        """记住运行参数。

        Args:
            config: 服务配置。
        """
        self.config = config

    async def run(
        self,
        *,
        language: str,
        content: str,
        argv: list[str] | None = None,
        stdin: str = "",
        timeout_ms: int | None = None,
    ) -> ExecOutcome:
        """跑一段脚本。

        Args:
            language: ``python`` / ``bash`` / ``node``。
            content: 脚本正文。
            argv: 参数数组——**直接传给进程，不经过 shell**（§9.1）。
            stdin: 标准输入（文本）。
            timeout_ms: 执行时限（毫秒）；不给就用服务端的上限。

        Returns:
            :class:`ExecOutcome`。
        """
        started = time.monotonic()
        if language not in _INTERPRETERS:
            return ExecOutcome(
                status=STATUS_FAILED,
                exit_code=-1,
                stdout="",
                stderr="",
                truncated=False,
                duration_ms=0,
                error=f"不支持的运行时：{language}",
                stdout_digest="",
                failure_reason=FAILURE_UNKNOWN_LANGUAGE,
            )
        interpreter, filename = _INTERPRETERS[language]
        timeout_ms = int(
            min(timeout_ms or self.config.max_timeout_ms, self.config.max_timeout_ms)
        )

        workdir = tempfile.mkdtemp(prefix="studio-")
        try:
            script_path = Path(workdir) / filename
            script_path.write_text(content, encoding="utf-8")
            env = _build_env(workdir)
            # **起进程之前**先记一次 OOM 计数——脚本可能一起来就把内存打爆，
            # 记晚了就分不清「这次被 OOM 杀的」和「之前就杀过」（§13.2）。
            oom_kill_before = read_oom_kill_count()
            proc = await self._spawn(
                interpreter, str(script_path), list(argv or []), env, timeout_ms
            )
            stdin_task = asyncio.create_task(self._write_stdin(proc, stdin))
            try:
                return await self._collect(
                    proc, timeout_ms, started, oom_kill_before
                )
            finally:
                if not stdin_task.done():
                    stdin_task.cancel()
                await self._close_stdin(proc)
        except FileNotFoundError as exc:
            # 运行时缺失（§4.1）：容器里没装 bash / node 之类。
            logger.warning("运行时不可用：%s", language)
            return ExecOutcome(
                status=STATUS_FAILED,
                exit_code=-1,
                stdout="",
                stderr="",
                truncated=False,
                duration_ms=int((time.monotonic() - started) * 1000),
                error=f"运行时 {language} 不可用：{exc}",
                stdout_digest="",
                failure_reason=FAILURE_RUNTIME_MISSING,
            )
        except Exception as exc:  # noqa: BLE001 - 外部边界
            logger.exception("执行脚本失败")
            return ExecOutcome(
                status=STATUS_FAILED,
                exit_code=-1,
                stdout="",
                stderr="",
                truncated=False,
                duration_ms=int((time.monotonic() - started) * 1000),
                error=f"{type(exc).__name__}: {exc}",
                stdout_digest="",
                failure_reason=FAILURE_SPAWN_FAILED,
            )
        finally:
            # 清理**必须对所有终态都做**（§4）——漏一次就漏一个孤儿进程。
            shutil.rmtree(workdir, ignore_errors=True)

    async def _spawn(
        self,
        interpreter: str,
        script_path: str,
        argv: list[str],
        env: dict[str, str],
        timeout_ms: int,
    ):
        """起子进程，并让它成为**独立进程组**的组长（§6.1）。

        Args:
            interpreter: 解释器路径。
            script_path: 脚本文件路径。
            argv: 参数数组。
            env: 环境变量。
            timeout_ms: 执行时限（用于 ``RLIMIT_CPU``）。

        Returns:
            ``asyncio.subprocess.Process``。
        """
        kwargs: dict = {
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "cwd": os.path.dirname(script_path),
            "env": env,
        }
        if os.name == "nt":
            # Windows 没有 setsid，但有「新建进程组」，效果接近。
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
            preexec = _make_preexec(
                max(1, timeout_ms // 1000), self.config.oom_score_adj
            )
            if preexec is not None:
                kwargs["preexec_fn"] = preexec
        return await asyncio.create_subprocess_exec(
            interpreter, script_path, *argv, **kwargs
        )

    async def _collect(
        self,
        proc,
        timeout_ms: int,
        started: float,
        oom_kill_before: int | None = None,
    ) -> ExecOutcome:
        """读输出、等退出、超时则杀进程组。

        Args:
            proc: 子进程。
            timeout_ms: 执行时限。
            started: 开始时刻（``time.monotonic``）。
            oom_kill_before: 起进程前读到的 cgroup ``oom_kill`` 计数；用来判断
                本次执行期间是不是真的发生了 OOM（§13.2）。

        Returns:
            :class:`ExecOutcome`。
        """
        stdout_task = asyncio.create_task(
            self._drain(proc.stdout, self.config.max_stdout_bytes)
        )
        stderr_task = asyncio.create_task(
            self._drain(proc.stderr, self.config.max_stderr_bytes)
        )
        wait_task = asyncio.create_task(proc.wait())
        tasks = [wait_task, stdout_task, stderr_task]

        status = STATUS_COMPLETED
        error = ""
        truncated = False
        exit_code = -1
        try:
            # FIRST_EXCEPTION：正常时等价于「全部完成」；而输出超限时读管道的
            # 任务会立刻抛异常——不等它的话，子进程会卡在写满的管道上，
            # 一直等到超时才被发现。
            done, pending = await asyncio.wait(
                tasks,
                timeout=timeout_ms / 1000.0,
                return_when=asyncio.FIRST_EXCEPTION,
            )
        except asyncio.CancelledError:
            # 调用方取消（§8.3）：停止等待输出、杀进程组。
            await self._terminate_tree(proc)
            for task in tasks:
                task.cancel()
            raise

        # 先看有没有异常，再看有没有超时——**顺序反了会把「输出超限」误判成
        # 「超时」**：输出超限时读管道的任务已经抛异常了，但等进程的任务还在
        # pending，两个分支的表象一样（都有 pending 的任务）。
        overflow = False
        failure: Exception | None = None
        for task in tasks:
            if not task.done() or task.cancelled():
                continue
            exc = task.exception()
            if exc is None:
                continue
            if isinstance(exc, OutputOverflow):
                overflow = True
            else:
                failure = exc
        if failure is not None:
            raise failure

        if overflow:
            status = STATUS_LIMITED
            truncated = True
            error = "脚本输出超过上限，已终止"
            await self._terminate_tree(proc)
        elif pending:
            status = STATUS_TIMEOUT
            error = f"脚本执行超时（{timeout_ms} 毫秒），已杀掉进程组"
            await self._terminate_tree(proc)

        # 收尾：等剩下的任务结束，别留悬挂的协程。
        await asyncio.wait(tasks, timeout=5)
        for task in tasks:
            if not task.done():
                task.cancel()

        if status == STATUS_COMPLETED:
            exit_code = proc.returncode if proc.returncode is not None else -1
            if is_sigkilled(exit_code):
                # §13.2：被 SIGKILL 杀掉 = 被资源限制干掉，**不是**「脚本跑完了」。
                # 契约里是 LIMITED，退出码按契约是 -1，原因写进 error。
                raw_code = exit_code
                status = STATUS_LIMITED
                exit_code = -1
                error = describe_sigkill(
                    raw_code,
                    oom_killed_since(oom_kill_before, read_oom_kill_count()),
                )
                logger.warning("脚本被 SIGKILL 杀掉：%s", error)
        stdout_bytes = _bytes_of(stdout_task)
        stderr_bytes = _bytes_of(stderr_task)
        # UTF-8 解码，失败用 replace（§9.3）。
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        return ExecOutcome(
            status=status,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            truncated=truncated,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=error,
            stdout_digest=hashlib.sha256(stdout_bytes).hexdigest(),
            stdout_bytes=len(stdout_bytes),
            stderr_bytes=len(stderr_bytes),
        )

    @staticmethod
    async def _write_stdin(proc, data: str) -> None:
        """把 stdin 写给子进程，**然后关掉它**。

        关掉是关键：脚本读 stdin 会一直等到 EOF，不关的话它永远等下去，
        于是每次带 stdin 的执行都会走到超时。

        Args:
            proc: 子进程。
            data: 标准输入（文本）。
        """
        try:
            if data and proc.stdin is not None:
                proc.stdin.write(data.encode("utf-8"))
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError, AttributeError):
            # 脚本没读 stdin 就退出了——管道写不进去是正常的。
            pass
        finally:
            try:
                if proc.stdin is not None and not proc.stdin.is_closing():
                    proc.stdin.close()
            except (OSError, AttributeError):
                pass

    @staticmethod
    async def _close_stdin(proc) -> None:
        """关掉子进程的 stdin。

        不关的话，脚本读 stdin 会一直等着——哪怕调用方根本没给内容。

        Args:
            proc: 子进程。
        """
        try:
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()
        except (OSError, AttributeError):
            pass

    @staticmethod
    async def _drain(stream, limit: int) -> bytes:
        """读一个流，超过上限就**停止读取**（§9.2）。

        只截断但继续读的话，一个 ``cat /dev/urandom`` 就能把 studio 的内存吃光。

        Args:
            stream: 子进程的输出流。
            limit: 字节上限。

        Returns:
            读到的字节。

        Raises:
            OutputOverflow: 超过上限（异常里带着已读到的部分）。
        """
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                chunks.append(chunk[: limit - (total - len(chunk))])
                raise OutputOverflow(b"".join(chunks))
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    async def _terminate_tree(proc, grace: float = 2.0) -> None:
        """杀掉**整个进程组**（§6.1）。

        只杀父进程的话，孙进程会继续跑、继续占内存、继续持有打开的文件——而容器
        不会立刻回收它。

        顺序：先 ``SIGTERM`` 给 2 秒优雅退出，还活着就 ``SIGKILL``。

        Args:
            proc: 子进程。
            grace: 优雅退出的等待秒数。
        """
        if proc.returncode is not None:
            return
        try:
            if os.name == "nt":
                proc.terminate()
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=grace)
            return
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        try:
            if os.name == "nt":
                # /T 连子进程一起杀。
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    timeout=15,
                )
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError, subprocess.SubprocessError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
