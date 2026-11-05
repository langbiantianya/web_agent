"""容器级 OOM 的判定与自保（skill-service §13.2）。

## 这里钉住的命题

1. **被 SIGKILL 杀掉 ≠ 脚本跑完了**。退出码是 ``-9``（POSIX 的信号表示）或
   ``137``（``128+9`` 的容器惯例）时，这次执行是 ``LIMITED``，不是 ``COMPLETED``
   ——不然模型看到的就是一句没头没尾的 ``exit_code: -9``。
2. **正常退出的非零码不能被误判**：脚本自己 ``exit 3`` 仍然是 ``COMPLETED``。
3. **超时不能被误判成 OOM**：超时是我们自己杀的，状态是 ``TIMEOUT``。
4. **输出超限有自己的话术**：它是 ``LIMITED``，但原因不是内存。
5. **cgroup 的 ``oom_kill`` 计数是佐证**：计数涨了就敢说「确实超了内存」。

## 为什么 SIGKILL 那条在 Windows 上跳过

POSIX 上「被信号杀死」的退出码是**负数**（``-9``）；Windows 的
``TerminateProcess`` 给的是普通退出码（``9``），没有 OOM killer 这回事。
所以那条集成用例只在 POSIX 上跑——生产部署是 Linux 容器。
"""

from __future__ import annotations

import os
import socket

import grpc
import pytest

from services.studio.config import StudioConfig
from services.studio.executor import ScriptRunner
from services.studio.oom import (
    SIGKILL_EXIT_CODE,
    is_sigkilled,
    oom_killed_since,
    read_oom_kill_count,
    read_oom_score_adj,
    set_oom_score_adj,
)
from services.studio.server import StudioService
from webagent.studio.v1 import studio_pb2, studio_pb2_grpc

IS_POSIX = os.name == "posix"

# 自杀式脚本：给自己发 SIGKILL。父进程看到的就是 ``-9``。
_SIGKILL_SCRIPT = (
    "import os, signal\n"
    "os.kill(os.getpid(), signal.SIGKILL)\n"
)


def _free_port() -> int:
    """向 OS 借一个空闲端口。

    Returns:
        端口号。
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def stub():
    """在测试进程里起一个**真的** studio 服务。

    Yields:
        gRPC 客户端桩。
    """
    config = StudioConfig(
        languages=("python",),
        max_concurrency=2,
        max_queue=1,
        max_timeout_ms=5000,
    )
    service = StudioService(config)
    server = grpc.aio.server()
    studio_pb2_grpc.add_StudioServicer_to_server(service, server)
    port = server.add_insecure_port(f"127.0.0.1:{_free_port()}")
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    try:
        yield studio_pb2_grpc.StudioStub(channel)
    finally:
        await channel.close()
        await server.stop(grace=1)


async def _exec(stub, **kwargs):
    """调一次 Exec，带默认的合法参数。

    Args:
        stub: gRPC 客户端桩。
        **kwargs: 覆盖默认请求字段。

    Returns:
        ``ExecResponse``。
    """
    params = {
        "skill": "demo",
        "script": "scripts/run.py",
        "language": "python",
        "content": "print('ok')",
    }
    params.update(kwargs)
    return await stub.Exec(studio_pb2.ExecRequest(**params), timeout=30)


# --------------------------------------------------------------- 退出码的形状


def test_sigkill_exit_code_shapes():
    """SIGKILL 有两种表示，都要认（§13.2）。"""
    assert is_sigkilled(-9) is True
    assert is_sigkilled(137) is True
    assert SIGKILL_EXIT_CODE == 137
    # 别的退出码不能被误判。
    assert is_sigkilled(0) is False
    assert is_sigkilled(1) is False
    assert is_sigkilled(9) is False
    assert is_sigkilled(137 - 1) is False


def test_oom_killed_since_handles_missing_counter():
    """读不到 cgroup 计数时不能瞎猜（§13.2）。"""
    assert oom_killed_since(None, None) is False
    assert oom_killed_since(None, 3) is False
    assert oom_killed_since(3, None) is False
    assert oom_killed_since(1, 2) is True
    assert oom_killed_since(2, 2) is False


def test_read_oom_kill_count_returns_int_or_none():
    """计数要么是整数，要么是 None（读不到）。"""
    count = read_oom_kill_count()
    assert count is None or isinstance(count, int)


def test_set_oom_score_adj_never_raises():
    """设 ``oom_score_adj`` 失败也不能把执行搞崩——它是「尽力而为」的加分项。"""
    # 不管成没成功，都不能抛。
    set_oom_score_adj(0)
    if IS_POSIX:
        set_oom_score_adj(1000)


@pytest.mark.skipif(not IS_POSIX, reason="oom_score_adj 是 Linux 的东西")
def test_set_oom_score_adj_really_takes_effect():
    """调高要**真的生效**——不信 ``prctl`` 的返回值，只信回读。

    实测踩过的坑：``prctl(PR_SET_OOM_SCORE_ADJ, 1000)`` 在容器里返回 0 却什么
    都不做；写 ``/proc/self/oom_score_adj`` 才有效。所以在**子进程**里做——
    顺便验证改的是子进程自己的值，父进程不受影响。
    """
    pid = os.fork()
    if pid == 0:  # pragma: no cover - 子进程
        ok = set_oom_score_adj(1000)
        value = read_oom_score_adj()
        os._exit(0 if (ok and value == 1000) else 1)
        parent_before = read_oom_score_adj()
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
        # 父进程的值不受子进程影响。测试容器本身可能已经是 1000，
        # 因此比较前后值，而不是假定父进程一定从 0 开始。
        assert read_oom_score_adj() == parent_before


# --------------------------------------------------------------- 判定本身


async def test_exit_137_is_limited(stub):
    """退出码 137（128+9）→ ``LIMITED``，不是 ``COMPLETED``。

    这条**在所有平台都成立**：``exit 137`` 就是退出码 137。而脚本自己
    ``exit 137`` 在容器里通常意味着「里面那层被 SIGKILL 了」（shell 把
    ``128+信号号`` 传了上来）。
    """
    resp = await _exec(stub, content="raise SystemExit(137)")
    assert resp.status == studio_pb2.EXEC_STATUS_LIMITED
    assert resp.exit_code == -1
    assert "SIGKILL" in resp.error


@pytest.mark.skipif(not IS_POSIX, reason="信号语义是 POSIX 的")
async def test_sigkilled_script_is_limited(stub):
    """脚本被 SIGKILL 杀掉 → ``LIMITED`` + 说明文字。

    判定之前这里是 ``COMPLETED`` + ``exit_code=-9``：模型完全看不出
    「这个脚本是被资源限制干掉的」，只会当成脚本自己崩了。
    """
    resp = await _exec(stub, content=_SIGKILL_SCRIPT)
    assert resp.status == studio_pb2.EXEC_STATUS_LIMITED
    assert resp.exit_code == -1
    assert "SIGKILL" in resp.error


async def test_normal_nonzero_exit_is_still_completed(stub):
    """正常退出的非零码**不能**被误判成 LIMITED。"""
    resp = await _exec(stub, content="raise SystemExit(3)")
    assert resp.status == studio_pb2.EXEC_STATUS_COMPLETED
    assert resp.exit_code == 3
    assert resp.error == ""


async def test_timeout_is_not_reported_as_oom(stub):
    """超时是我们自己杀的进程，状态是 ``TIMEOUT``，不能变成 ``LIMITED``。"""
    resp = await _exec(
        stub,
        content="import time\ntime.sleep(30)\n",
        timeout_ms=1000,
    )
    assert resp.status == studio_pb2.EXEC_STATUS_TIMEOUT
    assert resp.exit_code == -1
    assert "超时" in resp.error


async def test_output_overflow_keeps_its_own_reason(stub):
    """输出超限也是 ``LIMITED``，但说的是「输出超限」不是「内存超限」。

    默认 stdout 上限是 1 MB——写 2 MB 就够了。
    """
    resp = await _exec(stub, content="print('x' * 2000000)")
    assert resp.status == studio_pb2.EXEC_STATUS_LIMITED
    assert "输出" in resp.error
    assert "OOM" not in resp.error


async def test_oom_counter_turns_the_message_certain(monkeypatch):
    """cgroup 的 ``oom_kill`` 计数涨了 → 说明文字里敢写「确实超了内存」。

    这里把计数读出来模拟成「涨了」——真的 OOM 要在容器里才测得到
    （主设计 §22.14.4 的实测记录）。
    """
    import services.studio.executor as executor_mod

    values = iter([0, 1])
    monkeypatch.setattr(
        executor_mod, "read_oom_kill_count", lambda: next(values, 1)
    )
    runner = ScriptRunner(StudioConfig(languages=("python",)))
    outcome = await runner.run(
        language="python", content="raise SystemExit(137)"
    )
    assert outcome.status == "limited"
    assert "OOM killer" in outcome.error


async def test_without_counter_the_message_stays_hedged(monkeypatch):
    """读不到计数时，说明文字要留余地——不能把「脚本自杀」说成「超了内存」。"""
    import services.studio.executor as executor_mod

    monkeypatch.setattr(
        executor_mod, "read_oom_kill_count", lambda: None
    )
    runner = ScriptRunner(StudioConfig(languages=("python",)))
    outcome = await runner.run(
        language="python", content="raise SystemExit(137)"
    )
    assert outcome.status == "limited"
    assert "多半是内存超出容器限额" in outcome.error
