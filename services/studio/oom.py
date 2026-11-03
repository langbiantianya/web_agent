"""OOM 的识别与自保（skill-service §13.2）。

## 为什么单独一个模块

「脚本被 OOM killer 杀掉」这件事有**两半**，必须放在一起看：

1. **判定**：退出码是 ``-9`` 或 ``137`` 时，这次执行不是「脚本跑完了」，而是
   「被资源限制杀掉了」——契约里是 ``LIMITED``，而不是 ``COMPLETED``。
2. **自保**：让 OOM killer **优先杀脚本**，而不是 studio 自己。

第 2 件事不做，第 1 件事基本不会发生——被杀的会是 studio 容器本身，调用方拿到的
是「连不上 studio」，而不是「这个脚本超了内存」。两件事是一件事的两面。

（**实测补充**：即使什么都不做，内核也是按内存占用挑进程的——吃内存的脚本本来就是
首选，实测脚本被杀、studio 活着。``oom_score_adj`` 是把这件「碰巧」的事变成
「必然」的事，不是唯一的防线。）

## 退出码的两种形状

同一个事实有两种表示，两种都要认：

| 表示 | 出处 |
| --- | --- |
| ``-9`` | POSIX 上 Python 的 ``subprocess``：被信号 N 杀死时 ``returncode`` 是 ``-N`` |
| ``137`` | 容器运行时与 shell 的惯例：``128 + 信号号``（脚本里再套一层 shell 时常见） |

容器运行时（``docker inspect``）报的 ``ExitCode=137`` 就是后一种。

## 佐证信号：cgroup 的 ``oom_kill`` 计数

退出码只能说明「被 SIGKILL 了」，说明不了「为什么」。cgroup v2 的
``/sys/fs/cgroup/memory.events`` 里有 ``oom_kill`` 计数——**执行期间它涨了**，
就说明确实是 OOM killer 干的。

这个信号是**佐证**，不是前提：读不到（没有 cgroup v2、权限不够）时照样按退出码
判定，只是说明文字里不带「确实超了内存」这层确信。
"""

from __future__ import annotations

import ctypes
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# SIGKILL 的两种表示（见模块文档）。
SIGKILL_SIGNAL = 9
SIGKILL_EXIT_CODE = 128 + SIGKILL_SIGNAL  # 137

# cgroup v2 的内存事件计数。``oom_kill`` = 被 OOM killer 杀掉的进程数。
_CGROUP_MEMORY_EVENTS = Path("/sys/fs/cgroup/memory.events")

# 进程自己的 ``oom_score_adj``。
_PROC_OOM_SCORE_ADJ = Path("/proc/self/oom_score_adj")

# ``prctl(PR_SET_OOM_SCORE_ADJ, ...)`` 的选项号。
_PR_SET_OOM_SCORE_ADJ = 13

# 脚本进程的 ``oom_score_adj``：调到最高（1000 = 优先被 OOM killer 挑中）。
#
# **实测**：内核只允许把值往「更易被杀」的方向调（往下调要 ``CAP_SYS_RESOURCE``），
# 所以这个值非特权进程也能设上去——**但要写 ``/proc/self/oom_score_adj``**，
# ``prctl`` 那条路在容器里是死的（返回 0 却什么都不做）。见
# :func:`set_oom_score_adj`。
DEFAULT_OOM_SCORE_ADJ = 1000


def is_sigkilled(returncode: int) -> bool:
    """退出码是不是「被 SIGKILL 杀掉」的形状。

    Args:
        returncode: 进程的退出码。

    Returns:
        True 表示这个退出码只可能来自 SIGKILL。
    """
    return returncode in (-SIGKILL_SIGNAL, SIGKILL_EXIT_CODE)


def read_oom_kill_count() -> int | None:
    """读 cgroup v2 的 ``memory.events`` 里的 ``oom_kill`` 计数。

    读不到就返回 ``None``——不是所有环境都有 cgroup v2，权限也可能不够。
    判定不依赖它（§13.2 的判定依据是退出码）。

    Returns:
        ``oom_kill`` 计数；读不到时是 ``None``。
    """
    try:
        raw = _CGROUP_MEMORY_EVENTS.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for line in raw.splitlines():
        key, _, value = line.partition(" ")
        if key.strip() == "oom_kill":
            try:
                return int(value.strip())
            except ValueError:
                return None
    return None


def oom_killed_since(before: int | None, after: int | None) -> bool:
    """本次执行期间，cgroup 的 ``oom_kill`` 计数有没有涨。

    注意这是**cgroup 级别**的计数：同一时刻另一个脚本被 OOM 杀掉，也会让这个
    计数涨。并发执行的场景下归因可能不准——但「这个容器的内存被打爆了」这个
    结论本身没错，够用。

    Args:
        before: 执行前读到的计数；``None`` 表示当时就读不到。
        after: 执行后读到的计数；``None`` 表示事后读不到。

    Returns:
        True 表示执行期间确实有进程被 OOM killer 杀掉。
    """
    if before is None or after is None:
        return False
    return after > before


def describe_sigkill(returncode: int, oom_confirmed: bool) -> str:
    """拼一句给模型看的说明（skill-service §13.2）。

    Args:
        returncode: 原始退出码。
        oom_confirmed: cgroup 是否记录到了 OOM kill。

    Returns:
        说明文字。
    """
    if returncode == SIGKILL_EXIT_CODE:
        shape = f"退出码 {SIGKILL_EXIT_CODE}（128+{SIGKILL_SIGNAL}）"
    else:
        shape = f"退出码 {returncode}（信号 {SIGKILL_SIGNAL}）"
    if oom_confirmed:
        return (
            f"脚本被 OOM killer 杀掉（{shape}）：内存超出容器限额。"
            "请减少内存占用、分批处理，或改成流式处理。"
        )
    return (
        f"脚本被 SIGKILL 杀掉（{shape}）：多半是内存超出容器限额，"
        "也可能是脚本自己 kill 了自己。请减少内存占用或分批处理。"
    )


def read_oom_score_adj() -> int | None:
    """读当前进程的 ``oom_score_adj``。

    Returns:
        取值；读不到时是 ``None``。
    """
    try:
        raw = _PROC_OOM_SCORE_ADJ.read_text(encoding="utf-8").strip()
        return int(raw)
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def set_oom_score_adj(value: int) -> bool:
    """把当前进程的 ``oom_score_adj`` 调成 ``value``。

    取值 ``-1000`` ~ ``1000``，**越大越容易被 OOM killer 挑中**。

    ## 实测出来的两件事（不是猜的）

    1. ``prctl(PR_SET_OOM_SCORE_ADJ, 1000)`` 在容器里**返回 0 却什么都不做**——
       两种调用形态（带/不带 ``argtypes``）、uid 1000 与 uid 0 都一样。原因未查清，
       所以**不能信它的返回值**。
    2. **写 ``/proc/self/oom_score_adj`` 是有效的**：uid 1000 就能写，fork 出来的
       子进程里写也生效。

    所以这里走 ``/proc`` 路径，并且**回读验证**——不信任何人的返回值，包括
    ``prctl`` 的。

    另外实测：内核只允许把值往「更易被杀」的方向调（写 ``-500`` 会被拒，EPERM），
    往下调需要 ``CAP_SYS_RESOURCE``。这正好是我们要的语义。

    Args:
        value: ``oom_score_adj`` 取值。

    Returns:
        True 表示值确实被设成了 ``value``。
    """
    if os.name == "nt":
        # Windows 没有这个概念。
        return False
    if value == 0:
        return True

    # 主路径：写 proc 文件。
    try:
        with open(_PROC_OOM_SCORE_ADJ, "w", encoding="utf-8") as fh:
            fh.write(str(value))
    except OSError:
        logger.debug("写 %s 失败", _PROC_OOM_SCORE_ADJ, exc_info=True)
    else:
        if read_oom_score_adj() == value:
            return True

    # 兜底：prctl。实测它在容器里不生效，但万一某个环境里 /proc 没挂而 prctl 能用，
    # 留条路。照样要回读验证。
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl.argtypes = [
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        libc.prctl.restype = ctypes.c_int
        libc.prctl(
            ctypes.c_int(_PR_SET_OOM_SCORE_ADJ),
            ctypes.c_ulong(value),
            ctypes.c_ulong(0),
            ctypes.c_ulong(0),
            ctypes.c_ulong(0),
        )
    except (OSError, AttributeError, ValueError):
        logger.debug("prctl 设置 oom_score_adj 失败", exc_info=True)
        return False
    return read_oom_score_adj() == value
