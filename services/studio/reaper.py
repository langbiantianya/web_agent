"""启动巡检：杀掉上次运行残留的脚本进程（skill-service §6.5、§19）。

## 为什么需要它

studio 执行脚本时会在临时目录里建工作目录，脚本的 ``cwd`` 就指向那里。
正常路径上，执行器在**所有终态**都会删掉这个目录（:mod:`services.studio.executor`）。

但有一种情况会漏：进程被**硬杀**（OOM killer、``SIGKILL``、节点掉电）。此时
``finally`` 不会跑，脚本的子进程可能还活着、工作目录也还在。

在容器里这基本不会发生（容器重启会清掉整个 PID 命名空间）；在裸机上跑
studio 时就会真的攒下孤儿进程——它们占着内存、持有打开的文件。

所以启动时扫一遍：把**工作目录还在**的那些残留进程杀掉，把目录清掉。

## 边界

- 只看 ``/proc``——非 Linux 上没有统一的「进程 cwd」接口，这道巡检在那些
  平台上**不做任何事**（而不是猜一个可能杀错进程的接口）；
- 只杀 **cwd 在临时目录里**的进程，不按进程名匹配——按名字杀会误伤同名但
  不相干的进程；
- 读 ``/proc/<pid>/cwd`` 需要同 uid 的权限；读不到就跳过那个进程。
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# 执行器建工作目录用的前缀（:mod:`services.studio.executor`）。
STALE_DIR_PREFIX = "studio-"

# 进程目录。
PROC_DIR = Path("/proc")


def _pid_dirs() -> list[Path]:
    """列出 ``/proc`` 下所有数字 pid 的目录。

    Returns:
        pid 目录列表；``/proc`` 不存在时返回空列表。
    """
    if not PROC_DIR.is_dir():
        return []
    out: list[Path] = []
    try:
        for entry in PROC_DIR.iterdir():
            if entry.name.isdigit():
                out.append(entry)
    except OSError:  # pragma: no cover - /proc 读不到时只能放弃
        return []
    return out


def _processes_in_dir(target: Path) -> list[int]:
    """找出 cwd 在 ``target`` 里的进程。

    Args:
        target: 目录。

    Returns:
        pid 列表。
    """
    pids: list[int] = []
    try:
        resolved_target = target.resolve()
    except OSError:  # pragma: no cover - 目录已经没了
        return pids
    for pid_dir in _pid_dirs():
        cwd_link = pid_dir / "cwd"
        try:
            actual = cwd_link.resolve()
        except OSError:
            # 权限不足、进程已退出、cwd 已被删——都跳过。
            continue
        try:
            actual.relative_to(resolved_target)
        except ValueError:
            continue
        pids.append(int(pid_dir.name))
    return pids


def _kill(pids: list[int], grace: float = 2.0) -> int:
    """杀掉这些进程：先 ``SIGTERM``，还活着就 ``SIGKILL``。

    Args:
        pids: 进程号列表。
        grace: 优雅退出的等待秒数。

    Returns:
        实际杀掉的进程数。
    """
    accepted: set[int] = set()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            accepted.add(pid)
        except (ProcessLookupError, PermissionError, OSError):
            continue
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        alive = []
        for pid in pids:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            except OSError:
                continue
            alive.append(pid)
        if not alive:
            break
        time.sleep(0.05)
    for pid in pids:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, OSError):
            continue
        try:
            os.kill(pid, signal.SIGKILL)
            accepted.add(pid)
        except (ProcessLookupError, PermissionError, OSError):
            continue
    # SIGTERM 已让进程退出的也要计入。旧实现只在补发 SIGKILL 时累加，
    # 会把“正常响应 SIGTERM”的成功清理误报成 0 个。
    return len(accepted)


def sweep_stale_processes(prefix: str = STALE_DIR_PREFIX) -> tuple[int, int]:
    """巡检并清理残留。

    Args:
        prefix: 临时工作目录的前缀。

    Returns:
        ``(杀掉的进程数, 清掉的目录数)``。非 Linux 平台上恒为 ``(0, 0)``。
    """
    if not PROC_DIR.is_dir():
        logger.info("当前平台没有 /proc，跳过启动巡检")
        return 0, 0
    tmp = Path(tempfile.gettempdir())
    try:
        candidates = sorted(tmp.glob(f"{prefix}*"))
    except OSError:  # pragma: no cover - 临时目录读不到
        return 0, 0
    killed_total = 0
    removed_total = 0
    for directory in candidates:
        if not directory.is_dir():
            continue
        pids = _processes_in_dir(directory)
        if pids:
            logger.warning(
                "发现残留进程 %s（工作目录 %s），启动巡检将它们杀掉",
                pids,
                directory,
            )
            killed_total += _kill(pids)
        shutil.rmtree(directory, ignore_errors=True)
        removed_total += 1
    if killed_total or removed_total:
        logger.info(
            "启动巡检完成：杀掉 %s 个残留进程，清掉 %s 个残留目录",
            killed_total,
            removed_total,
        )
    return killed_total, removed_total
