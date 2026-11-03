"""studio 的**运行期防护**必须真的在位（设计文档 §22.8、§22.12）。

这两条都是**真容器部署里才暴露**的坑，本地测试全绿也照样中招：

1. **在跑 gRPC 的进程里 fork**——gRPC 的 C 核心为支持多进程注册了
   ``pthread_atfork`` 处理器，在「fork 时别的线程正在 gRPC 里」的场景下会放弃
   清理，子进程带着坏掉的 epoll 状态去 exec，exec 之前就被 glog 的断言
   ``abort()`` 掉。表现是：脚本根本没跑起来，调用方收到 ``exit_code=-6``
   （SIGABRT）、stdout/stderr 全空。

   studio 必须跑脚本就要 fork（``preexec_fn`` 设 RLIMIT + ``start_new_session``
   建独立进程组），所以只能**关掉 gRPC 的 fork 支持**。这个开关要在
   **import grpc 之前**设好——gRPC 的 C 核心只在首次初始化时读它。

2. **容器里没有 init 进程**——脚本被超时杀掉时，它自己起的子进程会被重新挂到
   PID 1（也就是 studio）底下。python 不是 init、不会回收孤儿，被杀死的孙进程
   会变成**僵尸**（``/proc/<pid>`` 还在、状态 ``Z``），PID 表被慢慢占满。

这两条都是「静态可查」的：一条查环境变量与源码顺序，一条查镜像的 ENTRYPOINT。
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

STUDIO_INIT = ROOT / "services" / "studio" / "__init__.py"
STUDIO_DOCKERFILE = ROOT / "services" / "studio" / "Dockerfile"

FORK_GUARD = "GRPC_ENABLE_FORK_SUPPORT"


def test_fork_guard_is_set_before_grpc_is_imported():
    """``GRPC_ENABLE_FORK_SUPPORT=0`` 必须在 import grpc **之前**设好。

    用子进程验：起手时 ``grpc`` 还没被拉进来，导入 ``services.studio`` 之后
    环境变量已经是 "0"。这样验的是**运行时**的事实，而不是源码里有没有这个
    字符串——位置错了照样会被这条抓住（值会被设上，但 grpc 已经初始化完了，
    开关不起作用）。
    """
    env = {**os.environ}
    # 把外层的值去掉，逼代码自己设——否则 setdefault 会认外层那个。
    env.pop(FORK_GUARD, None)
    code = (
        "import os, sys\n"
        "assert 'grpc' not in sys.modules, 'grpc 在导入前就被拉进来了'\n"
        "import services.studio\n"
        f"assert os.environ.get({FORK_GUARD!r}) == '0', "
        f"os.environ.get({FORK_GUARD!r})\n"
        "assert 'grpc' in sys.modules, 'grpc 压根没被导入，验不出顺序'\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"{FORK_GUARD} 没有在 import grpc 之前被设成 0：\n"
        f"{result.stdout}\n{result.stderr}"
    )


def test_fork_guard_precedes_the_server_import_in_source():
    """源码层面也要对：设开关的那行必须**早于**导入 server（server 才 import grpc）。

    这条是给上面那条兜底的：万一有人把 ``setdefault`` 挪到文件末尾，
    运行时的值还是 "0"（子进程照样能 import 到包），但 grpc 早就初始化完了，
    开关是死的——上面那条抓不到，**这条**能。
    """
    tree = ast.parse(STUDIO_INIT.read_text(encoding="utf-8"))
    guard_line: int | None = None
    server_import_line: int | None = None

    for node in ast.walk(tree):
        # os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "0")
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "setdefault"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "environ"
            ):
                if node.args and isinstance(node.args[0], ast.Constant):
                    if node.args[0].value == FORK_GUARD:
                        guard_line = node.lineno
        # from services.studio.server import ...
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.endswith("studio.server"):
                server_import_line = node.lineno

    assert guard_line is not None, f"{FORK_GUARD} 的开关没写在 {STUDIO_INIT} 里"
    assert server_import_line is not None, "找不到导入 server 的那行——源码结构变了"
    assert guard_line < server_import_line, (
        f"{FORK_GUARD} 的开关在第 {guard_line} 行，导入 server 在第 "
        f"{server_import_line} 行——顺序反了，gRPC 会先初始化，开关就是死的"
    )


def test_studio_image_runs_under_an_init_process():
    """studio 镜像必须用 init 进程（tini）当 PID 1。

    没有 init 的话，脚本被超时杀掉后它起的子进程会变成僵尸留在容器里，
    PID 表被慢慢占满。

    只认**指令行**（行首是 ``ENTRYPOINT``）——文件里的说明文字里也提到过
    ENTRYPOINT，按子串找会找到注释那一处。
    """
    lines = STUDIO_DOCKERFILE.read_text(encoding="utf-8").splitlines()
    entrypoints = [
        line.strip()
        for line in lines
        if line.strip().startswith("ENTRYPOINT")
    ]
    assert entrypoints, "services/studio/Dockerfile 里没有 ENTRYPOINT 指令"
    assert any("tini" in line for line in entrypoints), (
        f"ENTRYPOINT 不是 tini：{entrypoints}——它必须当 PID 1 才能回收孤儿"
    )


def test_studio_image_installs_tini():
    """镜像的 apt 步骤里必须真的装了 tini（不是只在注释里提到）。"""
    text = STUDIO_DOCKERFILE.read_text(encoding="utf-8")
    install_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("tini")
    ]
    assert install_lines, "services/studio/Dockerfile 的 apt 安装清单里没有 tini"
