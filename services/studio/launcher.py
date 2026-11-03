"""执行档位的抽象（skill-service §2.4、§8.4）。

## 两个档位

| 档位 | 隔离强度 | 一次执行的开销 |
| --- | --- | --- |
| `fast` | 容器内**独立进程组** + 独立临时工作目录 + 只读 rootfs | 几十~几百毫秒 |
| `isolated` | 每次执行起一个**一次性容器** | 秒级 |

对外的契约（proto）**不区分档位**——调用方看到的是同一个 ``Exec`` / ``ExecResponse``，
请求里也没有「档位」这个字段（§2.4：档位由**服务侧配置**决定，按 skill code 列表）。
所以两者的差别收在 :class:`Launcher` 这一层。

## 为什么 `isolated` 现在是骨架

`isolated` 档按设计要**每次执行起一个一次性容器**。但 studio 的三条硬约束
（§7：零凭据、禁出网、**无宿主挂载**）与它直接冲突：起容器要挂 Docker socket，
等于把宿主 root 等价权限交给一个「跑模型指定的代码」的进程。

所以这一层先做**接口 + 独立队列 + 明确的失败语义**：

- 路由按**服务侧配置**（§2.4 的「按 skill code 列表」），不加 proto 字段、不改契约；
- `isolated` 档有**自己的并发池与等待队列**（§8.4：「它必须单独排队，不能和 fast
  档抢同一个并发池——否则几个慢脚本能把快脚本全堵死」）；
- 容器运行时**没部署**时，调用方拿到的是 ``FAILED_PRECONDITION`` +
  「本实例的 isolated 档没有部署容器运行时」。

## 绝不悄悄退回 `fast` 档

悄悄退回是最坏的一种降级：调用方以为拿到的是强隔离，实际不是，而且**没有任何
痕迹**。所以档位不可用必须**明确说出来**（§12 的失败模式表也是这个口径：
「studio 不可用」要回灌给模型，「isolated 档不可用」同理）。

## 留给下一个实现的接缝

:class:`IsolatedRuntime` 就是那个接缝：谁来实现它、用什么容器运行时，是**部署
形态**的选择，不是契约的一部分。参考实现这一版**不提供实现**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from services.studio.executor import ExecOutcome, ScriptRunner


@dataclass(frozen=True, slots=True)
class ExecSpec:
    """一次执行的输入——两个档位共用同一份。

    Attributes:
        skill: 技能 code（审计用）。
        script: 脚本相对路径（审计用）。
        language: 运行时（``python`` / ``bash`` / ``node``）。
        content: 脚本正文。
        argv: 参数数组。
        stdin: 标准输入。
        timeout_ms: 执行时限（毫秒）。
        image: 镜像**目录名**（不是完整 ref）；空 = 默认镜像。只有 isolated
            档会用它，fast 档忽略。
        request_id: 调用方的追踪键；dispatcher 用它做标签/幂等（审计用）。
    """

    skill: str = ""
    script: str = ""
    language: str = "python"
    content: str = ""
    argv: tuple[str, ...] = field(default_factory=tuple)
    stdin: str = ""
    timeout_ms: int = 0
    image: str = ""
    request_id: str = ""


class LauncherUnavailableError(Exception):
    """这个档位的运行时**没部署**。

    服务层把它转成 gRPC 的 ``FAILED_PRECONDITION``——与「语言运行时缺失」
    同一类问题（skill-service §10.2：配置期就该拒）。
    """


@runtime_checkable
class Launcher(Protocol):
    """执行档位的接口。"""

    @property
    def available(self) -> bool:
        """这个档位现在能不能用。

        Returns:
            False 表示运行时没部署——调用方会拿到 ``FAILED_PRECONDITION``。
        """
        ...

    async def run(self, spec: ExecSpec) -> ExecOutcome:
        """跑一次。

        Args:
            spec: 执行输入。

        Returns:
            :class:`~services.studio.executor.ExecOutcome`。
        """
        ...

    async def close(self) -> None:
        """收尾（释放运行时资源）。"""
        ...


@runtime_checkable
class IsolatedRuntime(Protocol):
    """一次性容器执行器——``isolated`` 档真正落地时要实现的东西。

    参考实现这一版**不提供实现**：起容器要挂 Docker socket，与 §7 的
    「无宿主挂载」直接冲突。留这个接口是为了把边界画清楚。
    """

    async def run(self, spec: ExecSpec) -> ExecOutcome:
        """在一次性容器里跑一次。

        Args:
            spec: 执行输入。

        Returns:
            :class:`~services.studio.executor.ExecOutcome`。
        """
        ...

    async def close(self) -> None:
        """收尾。"""
        ...


class InProcessLauncher:
    """``fast`` 档：在 studio 进程内起一个独立进程组。

    这一档就是原来的执行器（:class:`~services.studio.executor.ScriptRunner`），
    外面套一层只是为了让它跟 ``isolated`` 档长得一样。
    """

    def __init__(self, runner: ScriptRunner) -> None:
        """记住执行器。

        Args:
            runner: 进程内执行器。
        """
        self._runner = runner

    @property
    def available(self) -> bool:
        """``fast`` 档永远可用——它不需要任何外部运行时。

        Returns:
            True。
        """
        return True

    async def run(self, spec: ExecSpec) -> ExecOutcome:
        """跑一次。

        Args:
            spec: 执行输入。

        Returns:
            :class:`~services.studio.executor.ExecOutcome`。
        """
        return await self._runner.run(
            language=spec.language,
            content=spec.content,
            argv=list(spec.argv),
            stdin=spec.stdin,
            timeout_ms=spec.timeout_ms,
        )

    async def close(self) -> None:
        """没有要收的尾。"""
        return None


class IsolatedLauncher:
    """``isolated`` 档：每次执行起一个一次性容器（**骨架**）。

    容器运行时没部署时：``available`` 是 False，调用方拿到明确的
    ``FAILED_PRECONDITION``，**不会**悄悄退回 ``fast`` 档。
    """

    def __init__(self, runtime: IsolatedRuntime | None = None) -> None:
        """记下容器运行时。

        Args:
            runtime: 一次性容器执行器；没部署时给 ``None``。
        """
        self._runtime = runtime

    @property
    def available(self) -> bool:
        """运行时部署了才可用。

        Returns:
            True 表示有容器运行时。
        """
        return self._runtime is not None

    async def run(self, spec: ExecSpec) -> ExecOutcome:
        """在一次性容器里跑一次。

        Args:
            spec: 执行输入。

        Returns:
            :class:`~services.studio.executor.ExecOutcome`。

        Raises:
            LauncherUnavailableError: 容器运行时没部署。
        """
        if self._runtime is None:
            raise LauncherUnavailableError(
                "本实例的 isolated 档没有部署容器运行时"
                "（需要一次性容器执行器），无法执行该技能"
            )
        return await self._runtime.run(spec)

    async def close(self) -> None:
        """收尾。"""
        if self._runtime is not None:
            await self._runtime.close()
