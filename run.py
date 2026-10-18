"""本地开发启动器：一条命令把服务跑起来。

用法（在本文件所在目录执行）：

    python run.py                # 迁移 + 灌演示数据 + 启动（默认带热重载）
    python run.py --no-seed      # 跳过灌演示数据
    python run.py --no-reload    # 不开热重载，行为更接近生产
    python run.py --port 9000    # 换端口
    python run.py --open         # 启动成功后自动打开浏览器

Windows 下不想敲命令，直接双击 ``start.bat`` 即可（它会自动用 ``.venv`` 里的
解释器，并带上 ``--open``）。

为什么要单独写这个脚本，而不是让 README 列出四条命令：

1. **工作目录**。``app/config/settings.py`` 的 ``env_file=".env"`` 和
   ``alembic.ini`` 的 ``script_location=alembic`` 都是相对路径。如果从别的
   目录启动，``.env`` 会**静默读不到**——不报错，只是 api_key 变成空字符串，
   排查起来非常费劲。这里强制 ``chdir`` 到项目根，从根上消除这个坑。
2. **执行顺序**。新建库必须先 ``alembic upgrade head`` 再 ``python -m scripts.seed``。
   顺序反了不会立刻报错，而是留下"表已存在但 alembic_version 为空"的半吊子状态，
   下次迁移才炸。这里固定顺序。
3. **依赖自检**。忘记 ``pip install`` 时给一句人话提示，而不是甩一脸
   ``ModuleNotFoundError``。
4. **收尾信息**。把验证台 / Swagger / 健康检查三个地址直接打出来，省得猜。
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

# 项目根目录 = 本文件所在目录。先算出来，后面所有相对路径都以它为基准。
ROOT = Path(__file__).resolve().parent

# 缺了这些包就没法跑，直接告诉用户去装，而不是让 ImportError 冒到最外层。
REQUIRED_PACKAGES = ("fastapi", "uvicorn", "alembic", "sqlalchemy")


def check_dependencies() -> None:
    """确认关键依赖已安装，缺失则打印安装命令并退出。

    用 ``importlib.util.find_spec`` 而不是真的 import：这里只想知道"装没装"，
    不该在自检阶段就触发 ``app.config.settings`` 读 .env 等副作用。
    """
    import importlib.util

    missing = [name for name in REQUIRED_PACKAGES if importlib.util.find_spec(name) is None]
    if not missing:
        return
    print(f"❌ 缺少依赖：{', '.join(missing)}")
    print("   请先执行：")
    print(f"       {sys.executable} -m pip install -r {ROOT / 'requirements.txt'}")
    sys.exit(1)


def ensure_env_file() -> None:
    """确保 ``.env`` 存在，缺失时从 ``.env.example`` 复制一份。

    不复制会怎样？Settings 会静默套用默认值，其中 ``secret_key`` 是硬编码的
    开发值，``minimax_api_key`` 为空——服务能起，但调不通模型。与其让人对着
    "Agent 一直返回 401" 猜，不如在这里给一份可编辑的骨架。
    """
    env_file = ROOT / ".env"
    example = ROOT / ".env.example"
    if env_file.exists():
        return
    if not example.exists():
        print("⚠️  没找到 .env 和 .env.example，将使用内置默认配置启动。")
        return
    env_file.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    print("✅ 已从 .env.example 生成 .env")
    print("   ⚠️ 记得填入 MINIMAX_API_KEY，否则模型调用会失败。")


def run_step(title: str, argv: list[str]) -> None:
    """在项目根目录执行一条子进程命令，失败即退出。

    刻意用子进程而不是直接调用 ``alembic.command.upgrade`` / ``seed.main``：
    alembic 的 ``fileConfig()`` 会重配 root logger，在同一进程里接着起 uvicorn
    会让日志格式串味；seed 的 import 副作用也会拖进同一进程。隔离开最省心，
    顺便让输出和用户手敲命令时完全一致。

    Args:
        title: 打印给用户看的步骤名。
        argv: 传给 ``sys.executable`` 的参数列表。
    """
    print(f"\n▶ {title}")
    result = subprocess.run([sys.executable, *argv], cwd=ROOT)
    if result.returncode != 0:
        print(f"\n❌ 「{title}」失败（退出码 {result.returncode}），已中止启动。")
        if "alembic" in argv:
            print("   常见原因：库里的表结构和 alembic 版本对不上。")
            print("   本地 SQLite 可直接删库重建：")
            print(f"       del \"{ROOT / 'web_agent.db'}\"")
            print("   然后重新执行 python run.py")
        sys.exit(result.returncode)


def wait_until_up(host: str, port: int, timeout: float = 30.0) -> bool:
    """轮询健康检查接口，直到服务可访问或超时。

    Args:
        host: 服务监听地址。
        port: 服务监听端口。
        timeout: 最长等待秒数。

    Returns:
        True 表示在超时前连上了；False 表示超时（此时不该弹浏览器）。
    """
    url = f"http://{host}:{port}/api/health"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return True
        except (urllib.error.URLError, socket.timeout, OSError):
            time.sleep(0.3)
    return False


def open_browser_when_ready(host: str, port: int) -> None:
    """后台等健康检查通过后再打开浏览器。

    直接在启动前 open 的话，用户会看到一个还没起来的连接错误页。这里先轮询到
    服务真正就绪，再弹窗，体感干净得多。整个过程放在守护线程里，不阻塞 uvicorn。
    """
    if not wait_until_up(host, port):
        print(f"⚠️  等待 30 秒仍未连上 {host}:{port}，跳过自动打开浏览器。")
        return
    webbrowser.open(f"http://{host}:{port}/")


def print_banner(host: str, port: int) -> None:
    """打印启动完成后的关键地址，方便直接点开。

    Args:
        host: 实际监听地址。
        port: 实际监听端口。
    """
    # 0.0.0.0 不能直接点开，换成浏览器能理解的 127.0.0.1。
    clickable = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    base = f"http://{clickable}:{port}"
    print("\n" + "=" * 52)
    print("  web-agent 已启动")
    print("=" * 52)
    print(f"  功能验证台   {base}/        ← 从这里开始")
    print(f"  Swagger UI   {base}/docs")
    print(f"  健康检查     {base}/api/health")
    if host not in ("127.0.0.1", "localhost"):
        print(f"  （监听 {host}，局域网内其他设备可用本机 IP 访问）")
    print("  按 Ctrl+C 停止\n")


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="本地开发启动器：迁移 + 灌演示数据 + 启动服务",
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=8000, help="监听端口，默认 8000")
    parser.add_argument("--reload", dest="reload", action="store_true", help="开启热重载（默认开启）")
    parser.add_argument("--no-reload", dest="reload", action="store_false", help="关闭热重载")
    parser.add_argument("--seed", dest="seed", action="store_true", help="灌演示数据（默认执行）")
    parser.add_argument("--no-seed", dest="seed", action="store_false", help="跳过灌演示数据")
    parser.add_argument("--open", dest="open_browser", action="store_true", help="启动后自动打开浏览器")
    parser.add_argument("--no-open", dest="open_browser", action="store_false", help="不自动打开浏览器")
    # 同一 dest 上后写的参数覆盖先写的，所以 `--no-reload` 能压掉默认的 --reload，
    # 用户想临时关掉某个默认行为时不必关心 start.bat 传了什么。
    parser.set_defaults(reload=True, seed=True, open_browser=False)
    return parser.parse_args()


def force_line_buffering() -> None:
    """把 stdout / stderr 切到行缓冲，保证输出实时可见。

    stdout 连的是终端时 Python 默认就是行缓冲，看不出问题；但一旦被重定向到
    文件或管道（``python run.py > run.log``、任务调度器、CI）就变成块缓冲，
    缓冲区不满不 flush——表现是"服务早就起来了，控制台却一行都没有"。
    这里显式切成行缓冲，顺手把 Python 3.9+ 才有的 ``encoding`` 也固定成 UTF-8，
    免得 Windows GBK 控制台把中文横幅打成乱码。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # 被替换成了不认识的流对象，放弃即可
            continue
        try:
            reconfigure(line_buffering=True, encoding="utf-8")
        except (ValueError, OSError):
            pass


def main() -> int:
    """执行完整启动流程，返回进程退出码。

    顺序是：切行缓冲 → 自检依赖 → 兜底 .env → 切到项目根 → 迁移 → 灌数据
    → 起 uvicorn。chdir 必须在读配置之前，因为 .env 是相对路径。
    """
    args = parse_args()

    force_line_buffering()

    print("=" * 52)
    print("  web-agent 启动器")
    print("=" * 52)
    print(f"  解释器：{sys.executable}")

    check_dependencies()

    # 尽早切目录：后面 subprocess 用 cwd=ROOT，这里是给用户直接
    # `python -m scripts.seed` 之类的旁路命令兜底。
    chdir_to_root()

    ensure_env_file()

    if args.seed:
        run_step("应用数据库迁移", ["-m", "alembic", "upgrade", "head"])
        run_step("灌入演示数据", ["-m", "scripts.seed"])
    else:
        # 跳过 seed 时仍然要迁移：否则新库上服务会因为缺表直接 500。
        run_step("应用数据库迁移", ["-m", "alembic", "upgrade", "head"])
        print("\n▶ 已跳过演示数据（--no-seed）")

    if args.open_browser:
        threading.Thread(
            target=open_browser_when_ready,
            args=(args.host, args.port),
            daemon=True,
        ).start()

    print_banner(args.host, args.port)

    # 放在最后：只有前面都成功了才占住端口，失败时不会留下一个起不来的残留进程。
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
    return 0


def chdir_to_root() -> None:
    """把当前工作目录切到项目根。

    单独抽出来是为了让意图显式：``.env`` 和 ``alembic.ini`` 都是相对路径解析的，
    这是"换个目录启动就静默读不到配置"的根因。
    """
    if Path.cwd() != ROOT:
        print(f"  工作目录：{ROOT}")
        os.chdir(ROOT)


if __name__ == "__main__":
    sys.exit(main())
