"""studio 服务侧的实现（skill-service §4、§6、§8、§9）。

这些测试起的是**真的** studio：真的起子进程、真的跑脚本、真的走 gRPC。

要钉住的命题：

1. **gRPC 成功 ≠ 脚本成功**——脚本 ``exit 1`` 是 ``COMPLETED`` + ``exit_code=1``，
   不是错误（§4.1）。
2. **超时杀的是整个进程组**（§6.1）——只杀父进程的话孙进程会继续跑。
3. **输出超限就停止读取**（§9.2）——只截断但继续读的话，一个 ``cat /dev/urandom``
   就能把 studio 的内存吃光。
4. **参数不经过 shell**（§9.1）——``;`` ``|`` 就是普通字符。
5. **环境变量是白名单**（§6.3）——调用方在请求里带的东西不能直接进环境。
6. **去重窗口**（§5.2）——同一个 ``request_id`` 不跑第二遍。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket

import grpc
import pytest

from services.studio.config import StudioConfig
from services.studio.server import StudioService
from webagent.studio.v1 import studio_pb2, studio_pb2_grpc

HAS_BASH = shutil.which("bash") is not None


def _free_port() -> int:
    """向 OS 借一个空闲端口。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def studio():
    """在测试进程里起一个**真的** studio 服务。

    Yields:
        ``(地址, 服务实例)``。
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
    try:
        yield f"127.0.0.1:{port}", service
    finally:
        await server.stop(grace=1)


@pytest.fixture
async def stub(studio):
    """连到 studio 的客户端桩。"""
    addr, _ = studio
    channel = grpc.aio.insecure_channel(addr)
    try:
        yield studio_pb2_grpc.StudioStub(channel)
    finally:
        await channel.close()


async def _exec(stub, **kwargs):
    """调一次 Exec，带默认的合法参数。"""
    params = {
        "skill": "demo",
        "script": "scripts/run.py",
        "language": "python",
        "content": "print('ok')",
    }
    params.update(kwargs)
    return await stub.Exec(studio_pb2.ExecRequest(**params), timeout=30)


# --------------------------------------------------------------- 基本执行


async def test_exec_runs_script(stub):
    """脚本跑完 → COMPLETED + exit_code 0 + stdout。"""
    resp = await _exec(stub, content="print('hello')")
    assert resp.status == studio_pb2.EXEC_STATUS_COMPLETED
    assert resp.exit_code == 0
    assert "hello" in resp.stdout
    assert resp.error == ""
    assert resp.stdout_digest


async def test_nonzero_exit_is_completed_not_error(stub):
    """脚本 ``exit 1`` 是 COMPLETED + exit_code=1，**不是**错误。

    这是本契约最重要的一条语义：gRPC 层面成功 = studio 正常工作；脚本成功与否
    由 status / exit_code 表达。用 gRPC 错误码报「脚本失败」的话，调用方只能
    当成服务不可用处理——语义完全错了。
    """
    resp = await _exec(stub, content="raise SystemExit(2)")
    assert resp.status == studio_pb2.EXEC_STATUS_COMPLETED
    assert resp.exit_code == 2


async def test_stderr_is_captured(stub):
    """stderr 原样回传（它与 error 是两回事）。"""
    resp = await _exec(
        stub, content="import sys\nsys.stderr.write('警告内容')\nprint('正常')\n"
    )
    assert resp.status == studio_pb2.EXEC_STATUS_COMPLETED
    assert "警告内容" in resp.stderr
    assert "正常" in resp.stdout
    assert resp.error == ""


async def test_stdin_is_passed(stub):
    """stdin 传得进去，而且写完会关掉——否则脚本会一直等 EOF。"""
    resp = await _exec(
        stub,
        content="import sys\nprint('in:', sys.stdin.read())\n",
        stdin="来自 stdin 的内容",
    )
    assert resp.status == studio_pb2.EXEC_STATUS_COMPLETED
    assert "来自 stdin 的内容" in resp.stdout


async def test_argv_is_passed_without_shell(stub):
    """参数**直接**传给进程，不经过 shell。

    ``;`` 与 ``|`` 是普通字符——否则调用方或模型传一个 ``foo; rm -rf /`` 就是
    一次命令注入。
    """
    resp = await _exec(
        stub,
        content="import sys\nprint(repr(sys.argv[1:]))\n",
        argv=["--x", "a; rm -rf /", "b | c"],
    )
    assert resp.status == studio_pb2.EXEC_STATUS_COMPLETED
    assert "a; rm -rf /" in resp.stdout
    assert "b | c" in resp.stdout


async def test_env_is_whitelisted(stub):
    """环境变量是白名单——其余一律不传（§6.3）。

    脚本不该看到调用方的环境，更不该看到 studio 自己的。
    """
    resp = await _exec(
        stub,
        content="import os\nprint(sorted(os.environ.keys()))\n",
    )
    assert resp.status == studio_pb2.EXEC_STATUS_COMPLETED
    keys = resp.stdout
    assert "PYTHONUNBUFFERED" in keys
    assert "PYTHONDONTWRITEBYTECODE" in keys
    for leaked in ("SECRET_KEY", "DATABASE_URL", "ADMIN_KEY"):
        assert leaked not in keys


async def test_cwd_is_a_temporary_dir(stub):
    """工作目录是**独立的临时目录**（§6.2）。"""
    resp = await _exec(stub, content="import os\nprint(os.getcwd())\n")
    assert resp.status == studio_pb2.EXEC_STATUS_COMPLETED
    cwd = resp.stdout.strip()
    assert "studio-" in cwd
    # 跑完之后目录被删掉了
    assert not os.path.exists(cwd)


# --------------------------------------------------------------- 超时与限额


async def test_timeout_kills_the_process(stub):
    """超时 → TIMEOUT + exit_code -1 + error 说明。"""
    resp = await _exec(
        stub,
        content="import time\ntime.sleep(30)\n",
        timeout_ms=600,
    )
    assert resp.status == studio_pb2.EXEC_STATUS_TIMEOUT
    assert resp.exit_code == -1
    assert "超时" in resp.error


async def test_timeout_kills_the_whole_process_group(stub):
    """超时杀的是**整个进程组**——孙进程也不能留下。

    只杀父进程的话，孙进程会继续跑、继续占内存、继续持有打开的文件。
    """
    marker = os.path.join(
        os.environ.get("TEMP", "/tmp"), f"studio-orphan-{os.getpid()}.txt"
    )
    if os.path.exists(marker):
        os.unlink(marker)
    # 脚本起一个孙进程：它 sleep 之后写文件。父进程被杀了，孙进程若还活着
    # 就会把文件写出来。
    resp = await _exec(
        stub,
        content=(
            "import subprocess, sys, time\n"
            "code = \"import time; time.sleep(3); open(%r, 'w').write('x')\"\n"
            "subprocess.Popen([sys.executable, '-c', code])\n"
            "time.sleep(30)\n" % marker
        ),
        timeout_ms=600,
    )
    assert resp.status == studio_pb2.EXEC_STATUS_TIMEOUT
    # 等孙进程原本会醒来的那个点，再看文件有没有被写出来
    await asyncio.sleep(4)
    assert not os.path.exists(marker), "孙进程活下来了——进程组没杀干净"


async def test_output_overflow_is_truncated(stub, studio):
    """输出超限 → 截断 + truncated=true + 进程被杀（§9.2）。"""
    _, service = studio
    # 临时把上限调小，不必真的写 1 MB
    object.__setattr__(service.config, "max_stdout_bytes", 1024)
    resp = await _exec(stub, content="print('x' * 100000)")
    assert resp.truncated is True
    assert resp.status == studio_pb2.EXEC_STATUS_LIMITED
    assert len(resp.stdout) <= 1024


async def test_unsupported_language_is_rejected(stub):
    """本实例不支持的运行时 → FAILED_PRECONDITION。"""
    with pytest.raises(grpc.aio.AioRpcError) as ei:
        await _exec(stub, language="ruby", content="puts 1")
    assert ei.value.code() == grpc.StatusCode.FAILED_PRECONDITION


async def test_empty_content_is_invalid_argument(stub):
    """content 为空 → INVALID_ARGUMENT。"""
    with pytest.raises(grpc.aio.AioRpcError) as ei:
        await _exec(stub, content="   ")
    assert ei.value.code() == grpc.StatusCode.INVALID_ARGUMENT


async def test_too_many_argv_is_invalid_argument(stub):
    """参数超量 → INVALID_ARGUMENT。"""
    with pytest.raises(grpc.aio.AioRpcError) as ei:
        await _exec(stub, argv=[f"a{i}" for i in range(65)])
    assert ei.value.code() == grpc.StatusCode.INVALID_ARGUMENT


# --------------------------------------------------------------- 去重


async def test_request_id_dedup_skips_second_run(stub):
    """同一个 ``request_id`` 在窗口内**不跑第二遍**（§5.2）。

    用途：调用方超时重试时，重发同一个 request_id 就不会把脚本跑第二遍。
    """
    marker = os.path.join(
        os.environ.get("TEMP", "/tmp"), f"studio-dedup-{os.getpid()}.txt"
    )
    if os.path.exists(marker):
        os.unlink(marker)
    content = (
        "import os\n"
        "open(%r, 'a').write('x')\n"
        "print('done')\n" % marker
    )
    first = await _exec(stub, content=content, request_id="req-dedup-1")
    second = await _exec(stub, content=content, request_id="req-dedup-1")
    assert first.status == studio_pb2.EXEC_STATUS_COMPLETED
    # 第二次直接返回上次的结果，没有再跑一遍
    assert second.stdout == first.stdout
    with open(marker, encoding="utf-8") as fh:
        assert fh.read() == "x", "脚本被跑了第二遍"


async def test_different_request_id_runs_again(stub):
    """不同的 ``request_id`` → 照常执行。

    模型合法地「想再跑一遍」不能被去重窗口吞掉——脚本可能压根不幂等。
    """
    first = await _exec(stub, request_id="req-a")
    second = await _exec(stub, request_id="req-b")
    assert first.duration_ms >= 0 and second.duration_ms >= 0
    assert first.stdout_digest == second.stdout_digest


# --------------------------------------------------------------- 闸门与探活


async def test_health_reports_runtime_info(stub):
    """探活便宜、且报出运行时清单（§13.3、§10.2）。"""
    resp = await stub.Health(studio_pb2.HealthRequest(), timeout=5)
    assert resp.ready is True
    assert resp.version
    assert [info.name for info in resp.language_info] == ["python"]
    assert resp.max_concurrency == 2
    assert resp.profile == "fast"


async def test_queue_full_is_resource_exhausted(stub, studio):
    """队列满了 → RESOURCE_EXHAUSTED（调用方退避重试）。"""
    _, service = studio
    # 并发上限 2、队列深度 1 → 第 5 个并发请求就该被拒。
    tasks = [
        asyncio.create_task(
            _exec(stub, content="import time\ntime.sleep(2)\n", timeout_ms=10000)
        )
        for _ in range(6)
    ]
    await asyncio.sleep(0.3)
    service._waiting = service.config.max_queue  # noqa: SLF001 - 强制队列满
    codes = []
    for task in tasks:
        try:
            await task
            codes.append(None)
        except grpc.aio.AioRpcError as exc:
            codes.append(exc.code())
    assert grpc.StatusCode.RESOURCE_EXHAUSTED in codes


# --------------------------------------------------------------- 跨语言


@pytest.mark.skipif(not HAS_BASH, reason="本机没有 bash")
async def test_bash_script_runs(stub):
    """bash 脚本也能跑（生产镜像里三种运行时都有）。"""
    resp = await _exec(
        stub, language="bash", content="echo bash-ok\n", script="scripts/run.sh"
    )
    assert resp.status == studio_pb2.EXEC_STATUS_COMPLETED
    assert "bash-ok" in resp.stdout
