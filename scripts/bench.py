"""并发压测：量出「一台机器能扛多少并发 SSE 流」，并据此推算 4c4g 的容量。

## 为什么要有这个脚本

「这服务能扛多少并发」本来是个只能靠感觉回答的问题。这个脚本把它变成**可测量的
量**：起一个真实的 uvicorn 子进程，用真实的 HTTP 客户端并发压 SSE 流，同时采集
服务端进程的 **CPU 时间**与 **常驻内存**，最后用「每条流消耗多少 CPU / 内存」反推
目标规格能带多少条流。

## 测的是什么、没测什么

**测了**：服务端自己那部分的开销——SSE 扇出、事件落库、上下文装配、编排调度。
这是「4c4g 能开多少条流」的决定因素。

**没测（且测不出来）**：真实模型的延迟与限流。本脚本用 `tests/fake_llm.py` 当模型
后端，它在本机跑，所以「模型耗时」是模拟出来的；生产里真正的天花板往往是模型
供应商的并发配额，不是本服务。

**因此结论要这么读**：本脚本给出的是**本服务的容量上界**。真实并发 =
`min(本服务容量, 模型配额, 带宽)`。

## 用法

```powershell
# 饱和压测（chunk_delay=0，服务端成为瓶颈，测出每 token 的 CPU 成本）
python scripts/bench.py --label saturated --chunk-delay 0

# 真实节奏（每 20ms 一个 token ≈ 50 token/s，接近真实模型）
python scripts/bench.py --label realistic --chunk-delay 0.02

# 自定义阶梯
python scripts/bench.py --levels 1,8,16,32,64,128 --tokens 200
```

结果打印成表格，并写一份 JSON 到 ``--out``（默认 ``%TEMP%/web_agent_bench.json``）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from tests.fake_llm import FakeLLM  # noqa: E402


# ------------------------------------------------------- 进程指标（免 psutil）


def _child_pids(pid: int) -> list[int]:
    """找出 ``pid`` 的**直接子进程**。

    ## 为什么需要它

    Windows 上 ``.venv\\Scripts\\python.exe`` 是个**启动器 shim**：它自己只占几 MB，
    真正的解释器（也就是服务本体）是它的子进程。``subprocess.Popen().pid`` 拿到的是
    shim 的 pid——直接拿它测 CPU/内存，测到的是一个几乎不干活的空壳。

    Linux 上没有这层 shim，``Popen().pid`` 就是服务本体；这里的实现让两边行为一致。

    Args:
        pid: 父进程号。

    Returns:
        直接子进程的 pid 列表。
    """
    if sys.platform == "win32":
        return _child_pids_windows(pid)
    return _child_pids_linux(pid)


def _child_pids_windows(pid: int) -> list[int]:
    """Windows 版：用 ``CreateToolhelp32Snapshot`` 扫进程表。

    刻意不用 ``wmic`` / PowerShell：那是子进程调用，每次 ~1 秒，压测里既慢又脏。

    Args:
        pid: 父进程号。

    Returns:
        直接子进程的 pid 列表。
    """
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32W(ctypes.Structure):
        """Win32 进程表条目。"""

        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)
    ]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)
    ]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == -1:
        return []
    children: list[int] = []
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    try:
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.th32ParentProcessID == pid:
                children.append(int(entry.th32ProcessID))
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return children


def _child_pids_linux(pid: int) -> list[int]:
    """Linux 版：扫 ``/proc`` 找父进程号匹配的。

    Args:
        pid: 父进程号。

    Returns:
        直接子进程的 pid 列表。
    """
    children: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:  # pragma: no cover
        return children
    for name in entries:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", encoding="utf-8") as f:
                fields = f.read().rsplit(")", 1)[1].split()
            if int(fields[1]) == pid:
                children.append(int(name))
        except (OSError, IndexError, ValueError):
            continue
    return children


def _resolve_worker_pid(pid: int) -> int:
    """找到**真正干活的**那个进程号。

    有 shim 就往下找一层；找到的那层里挑常驻内存最大的那个（服务本体）。

    Args:
        pid: 启动进程号。

    Returns:
        应当被测量的进程号。
    """
    for _ in range(3):  # 最多往下找三层，防御性地防着更深的多进程结构
        children = _child_pids(pid)
        if not children:
            break
        pid = max(children, key=lambda c: _proc_stat(c)[1] or 0)
    return pid


def _proc_stat(pid: int) -> tuple[float, int]:
    """读一个进程的 **CPU 秒数**与 **常驻内存字节**。

    不引入 psutil：这个脚本要能在一个只装了项目依赖的机器上直接跑。Windows 走
    ``GetProcessTimes`` / ``GetProcessMemoryInfo``，Linux 读 ``/proc``。

    Args:
        pid: 进程号。

    Returns:
        ``(累计 CPU 秒数, 当前 RSS 字节)``。读不到时返回 ``(0.0, 0)``。
    """
    if sys.platform == "win32":
        return _proc_stat_windows(pid)
    return _proc_stat_linux(pid)


def _proc_stat_windows(pid: int) -> tuple[float, int]:
    """Windows 版的进程指标采集。

    Args:
        pid: 进程号。

    Returns:
        ``(CPU 秒, RSS 字节)``。
    """
    import ctypes
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        """Win32 的 64 位时间戳（两个 DWORD 拼起来）。"""

        _fields_ = [
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        ]

    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        """``GetProcessMemoryInfo`` 的返回结构。"""

        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)

    # **必须显式声明 argtypes**：不声明的话 ctypes 会把每个参数按 c_int 转换，
    # 64 位指针被截断，函数读的是垃圾地址——返回的 CPU 时间恒为 0、RSS 恒为几 MB。
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
        wintypes.DWORD,
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    PROCESS_VM_READ = 0x0010
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ, False, pid
    )
    if not handle:
        return 0.0, 0

    try:
        creation, exit_, kernel, user = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
        # FILETIME 是 100ns 单位
        cpu = (kernel.dwHighDateTime << 32 | kernel.dwLowDateTime) / 1e7
        cpu += (user.dwHighDateTime << 32 | user.dwLowDateTime) / 1e7

        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        psapi.GetProcessMemoryInfo(
            handle, ctypes.byref(counters), counters.cb
        )
        return cpu, int(counters.WorkingSetSize)
    finally:
        kernel32.CloseHandle(handle)


def _proc_stat_linux(pid: int) -> tuple[float, int]:
    """Linux 版：从 ``/proc`` 读。

    Args:
        pid: 进程号。

    Returns:
        ``(CPU 秒, RSS 字节)``。
    """
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            fields = f.read().rsplit(")", 1)[1].split()
        # /proc/<pid>/stat 的第 14、15 项是 utime、stime（单位是 clock tick）
        hz = os.sysconf("SC_CLK_TCK")
        cpu = (int(fields[11]) + int(fields[12])) / hz
        rss = 0
        with open(f"/proc/{pid}/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1]) * 1024
                    break
        return cpu, rss
    except (OSError, IndexError, ValueError):
        return 0.0, 0


# ------------------------------------------------------------------- 小工具


def _free_port() -> int:
    """向 OS 借一个空闲端口。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _pct(values: list[float], p: float) -> float:
    """求分位数（线性插值）。

    Args:
        values: 样本（不要求有序）。
        p: 百分位，如 95。

    Returns:
        分位值；样本为空时返回 0.0。
    """
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


# ----------------------------------------------------------------- 压测主体


async def _one_stream(
    client: httpx.AsyncClient, conv_id: int, timeout: float
) -> dict:
    """跑一个完整的「发消息 → 订阅 SSE → 收到 done」。

    Args:
        client: HTTP 客户端。
        conv_id: 会话 id。
        timeout: 单条流的超时（秒）。

    Returns:
        这次流的指标：首 token 延迟、总耗时、token 数、事件数；失败时带
        ``error`` 字段（**不抛异常**——压测要的是「能扛到哪档」，某一档里
        挂掉几条本身就是结论）。
    """
    t0 = time.perf_counter()
    try:
        r = await client.post(
            f"/api/conversations/{conv_id}/messages", json={"content": "压测"}
        )
        r.raise_for_status()
        run_id = r.json()["run_id"]

        tk = await client.post("/api/auth/sse-ticket", json={"user_id": "bench"})
        tk.raise_for_status()
        ticket = tk.json()["ticket"]
    except Exception as exc:  # noqa: BLE001 - 失败也要计入统计
        return {
            "ttfb": -1.0,
            "total": time.perf_counter() - t0,
            "tokens": 0,
            "events": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }

    first_token: float | None = None
    tokens = 0
    events = 0
    ev = ""
    try:
        async with client.stream(
            "GET", f"/api/runs/{run_id}/stream?ticket={ticket}&last_seq=0",
            timeout=timeout,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                line = line.rstrip("\r")
                if line.startswith("event:"):
                    ev = line[6:].strip()
                elif line.startswith("data:"):
                    events += 1
                    if ev == "token":
                        tokens += 1
                        if first_token is None:
                            first_token = time.perf_counter() - t0
                    elif ev in ("done", "error"):
                        break
    except Exception as exc:  # noqa: BLE001 - 同上，失败也计入统计
        return {
            "ttfb": first_token if first_token is not None else -1.0,
            "total": time.perf_counter() - t0,
            "tokens": tokens,
            "events": events,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "ttfb": first_token if first_token is not None else -1.0,
        "total": time.perf_counter() - t0,
        "tokens": tokens,
        "events": events,
    }


async def _level(
    client: httpx.AsyncClient,
    base: str,
    wf_code: str,
    level: int,
    server_pid: int,
    timeout: float,
) -> dict:
    """跑一个并发档位，返回该档位的全部指标。

    会话在**计时窗口之外**先建好：建会话是 DB 写，与「同时开多少条流」是两件事，
    混在一起测会把 DB 的写入延迟算进流延迟里。

    Args:
        client: HTTP 客户端。
        base: 服务地址。
        wf_code: 工作流 code。
        level: 并发档位。
        server_pid: 服务端进程号（用来采 CPU/内存）。
        timeout: 单条流超时（秒）。

    Returns:
        该档位的指标字典。
    """
    convs: list[int] = []
    for _ in range(level):
        c = await client.post(
            "/api/conversations", json={"workflow_code": wf_code, "user_id": "bench"}
        )
        c.raise_for_status()
        convs.append(c.json()["id"])

    # 服务端空闲时的基线：用来算「每条流多占了多少内存」
    cpu0, rss0 = _proc_stat(server_pid)
    t_start = time.perf_counter()
    results = await asyncio.gather(
        *(_one_stream(client, cid, timeout) for cid in convs)
    )
    wall = time.perf_counter() - t_start
    cpu1, rss1 = _proc_stat(server_pid)

    ttfb = [r["ttfb"] for r in results if r["ttfb"] >= 0]
    totals = [r["total"] for r in results]
    tokens = sum(r["tokens"] for r in results)
    events = sum(r["events"] for r in results)
    errors = [r["error"] for r in results if r.get("error")]

    return {
        "level": level,
        "wall_s": round(wall, 3),
        "runs": len(results),
        "failed": len(errors),
        "errors": errors[:3],
        "tokens": tokens,
        "events": events,
        "tok_per_s": round(tokens / wall, 1) if wall > 0 else 0,
        "ttfb_p50": round(_pct(ttfb, 50), 3),
        "ttfb_p95": round(_pct(ttfb, 95), 3),
        "total_p50": round(_pct(totals, 50), 3),
        "total_p95": round(_pct(totals, 95), 3),
        "cpu_s": round(cpu1 - cpu0, 3),
        "cpu_pct_of_1core": round((cpu1 - cpu0) / wall * 100, 1) if wall > 0 else 0,
        "rss_mb": round(rss1 / 1024 / 1024, 1),
        "rss_delta_mb": round((rss1 - rss0) / 1024 / 1024, 1),
    }


async def _wait_healthy(client: httpx.AsyncClient, timeout: float = 40.0) -> bool:
    """轮询健康检查直到服务就绪。

    Args:
        client: HTTP 客户端。
        timeout: 最长等待秒数。

    Returns:
        True 表示服务已就绪。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = await client.get("/api/health")
            if r.status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.2)
    return False


async def _probe_api(client: httpx.AsyncClient, base: str, seconds: float) -> dict:
    """压一下**非流式**接口：后台查询类请求的 QPS 与延迟。

    Args:
        client: HTTP 客户端。
        base: 服务地址（仅用于日志）。
        seconds: 压测持续秒数。

    Returns:
        ``{"qps", "p50", "p95"}``。
    """
    latencies: list[float] = []
    deadline = time.monotonic() + seconds

    async def one() -> None:
        """发一次列表请求并记录延迟。"""
        while time.monotonic() < deadline:
            t = time.perf_counter()
            try:
                await client.get("/api/agents?limit=20")
            except httpx.HTTPError:
                continue
            latencies.append(time.perf_counter() - t)

    await asyncio.gather(*(one() for _ in range(16)))
    return {
        "qps": round(len(latencies) / seconds, 1),
        "p50": round(_pct(latencies, 50) * 1000, 2),
        "p95": round(_pct(latencies, 95) * 1000, 2),
    }


# --------------------------------------------------------------------- 主流程


def _prepare_db(db_url: str) -> None:
    """用 alembic 建好压测库的表结构。

    Args:
        db_url: 压测库 DSN。
    """
    env = os.environ.copy()
    env["DATABASE_URL"] = db_url
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
    )


def _print_table(rows: list[dict]) -> None:
    """把各档位指标打成一张对齐的表。

    Args:
        rows: 各档位的指标字典。
    """
    headers = [
        ("level", "并发"),
        ("failed", "失败"),
        ("wall_s", "墙钟s"),
        ("tok_per_s", "token/s"),
        ("ttfb_p50", "首字p50"),
        ("ttfb_p95", "首字p95"),
        ("total_p50", "总延迟p50"),
        ("total_p95", "总延迟p95"),
        ("cpu_s", "CPU秒"),
        ("cpu_pct_of_1core", "CPU%核"),
        ("rss_mb", "RSS(MB)"),
    ]
    widths = [
        max(len(h[1]), max((len(str(r.get(h[0], ""))) for r in rows), default=0))
        for h in headers
    ]
    line = "  ".join(h[1].rjust(w) for h, w in zip(headers, widths))
    print(line)
    print("-" * len(line))
    for r in rows:
        print("  ".join(str(r.get(h[0], "")).rjust(w) for h, w in zip(headers, widths)))
        if r.get("errors"):
            for e in r["errors"]:
                print(f"      ↳ {e}")


def _tail(path: Path, lines: int) -> str:
    """读日志文件的末尾几行。

    Args:
        path: 日志路径。
        lines: 要读的行数。

    Returns:
        末尾若干行文本；文件不存在时返回提示语。
    """
    if not path.exists():
        return "（没有日志文件）"
    text = path.read_text(encoding="utf-8", errors="replace")
    return "\n".join(text.splitlines()[-lines:])


async def _main_async(args: argparse.Namespace) -> int:
    """压测主流程。

    Args:
        args: 命令行参数。

    Returns:
        进程退出码。
    """
    tmp = Path(tempfile.gettempdir())
    db_path = tmp / f"web_agent_bench_{os.getpid()}.db"
    ckpt_path = tmp / f"web_agent_bench_ckpt_{os.getpid()}.db"
    for p in (
        db_path,
        db_path.with_name(db_path.name + "-wal"),
        db_path.with_name(db_path.name + "-shm"),
        ckpt_path,
    ):
        if p.exists():
            p.unlink()
    db_url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    print(f"压测库：{db_path}")

    _prepare_db(db_url)

    # ---- 假模型后端 ----
    text = "压" * args.tokens
    llm = FakeLLM(fixed_text=text, chunk_delay=args.chunk_delay)
    llm.start()
    print(f"假模型：chunk_delay={args.chunk_delay}s，每条回复 {args.tokens} 字")

    # ---- 服务端子进程 ----
    port = _free_port()
    env = os.environ.copy()
    env["DATABASE_URL"] = db_url
    env["CHECKPOINT_SQLITE_PATH"] = str(ckpt_path)
    env["REDIS_ENABLED"] = "false"
    env["PYTHONUNBUFFERED"] = "1"
    server_log = Path(tempfile.gettempdir()) / f"web_agent_bench_srv_{os.getpid()}.log"
    log_fh = open(server_log, "wb")
    server = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "app.main:app",
            "--host", "127.0.0.1", "--port", str(port),
            "--log-level", "warning",
        ],
        cwd=ROOT,
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    limits = httpx.Limits(max_connections=args.level_max, max_keepalive_connections=64)
    try:
        async with httpx.AsyncClient(
            base_url=base, timeout=httpx.Timeout(30, read=600), limits=limits
        ) as client:
            if not await _wait_healthy(client):
                print("❌ 服务启动超时")
                return 1
            # Windows 上 .venv 的 python.exe 是启动器 shim，真正跑服务的是它的子进程
            worker_pid = _resolve_worker_pid(server.pid)
            print(
                f"服务已就绪：{base}（启动器 pid={server.pid}，"
                f"服务进程 pid={worker_pid}）"
            )

            # ---- 造配置 ----
            code = f"bench_agent_{os.getpid()}"
            r = await client.post(
                "/api/agents",
                json={
                    "code": code,
                    "name": "压测 Agent",
                    "system_prompt": "你是压测机器人",
                    "model": "fake-model",
                    "base_url": llm.base_url,
                    "api_key": "bench",
                },
            )
            r.raise_for_status()
            wf_code = f"bench_wf_{os.getpid()}"
            r = await client.post(
                "/api/workflows",
                json={
                    "code": wf_code,
                    "name": "压测流水线",
                    "definition": {
                        "entry": code,
                        "nodes": [{"id": code, "agentCode": code, "outputKey": "out"}],
                        "edges": [],
                        "inputTemplate": "{{user_input}}",
                    },
                },
            )
            r.raise_for_status()

            # ---- 阶梯 ----
            rows: list[dict] = []
            for level in args.levels:
                print(f"\n▶ 并发 {level} …")
                try:
                    row = await _level(
                        client, base, wf_code, level, worker_pid, args.timeout
                    )
                except Exception as exc:  # noqa: BLE001 - 压测要的是「能扛到哪档」
                    # 某档直接崩了不终止整轮：崩掉的那一档本身就是结论。
                    print(f"  ✗ 失败：{type(exc).__name__}: {exc}")
                    row = {"level": level, "error": f"{type(exc).__name__}: {exc}"}
                rows.append(row)
                if "error" in row:
                    print("  服务端日志尾部：")
                    print(_tail(server_log, 25))
                    continue
                print(
                    f"  {row['wall_s']}s  {row['tok_per_s']} token/s  "
                    f"首字p95={row['ttfb_p95']}s  总延迟p95={row['total_p95']}s  "
                    f"CPU={row['cpu_s']}s({row['cpu_pct_of_1core']}%核)  "
                    f"RSS={row['rss_mb']}MB  失败={row['failed']}/{row['runs']}"
                )

            print("\n=== 结果表 ===")
            _print_table(rows)

            api_probe = await _probe_api(client, base, args.api_seconds)
            print(
                f"\n非流式接口（GET /api/agents，16 并发）："
                f"{api_probe['qps']} QPS  p50={api_probe['p50']}ms  p95={api_probe['p95']}ms"
            )

            machine = {
                "cpu_cores": os.cpu_count(),
                "platform": platform.platform(),
                "python": platform.python_version(),
            }
            result = {
                "label": args.label,
                "args": vars(args),
                "machine": machine,
                "levels": rows,
                "api_probe": api_probe,
            }
            out = Path(args.out)
            out.write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
            print(f"\nJSON 结果：{out}")
            return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            server.kill()
        llm.stop()
        log_fh.close()


def main() -> int:
    """命令行入口：解析参数并跑压测。

    Returns:
        进程退出码。
    """
    ap = argparse.ArgumentParser(description="web-agent 并发压测")
    ap.add_argument(
        "--levels",
        type=str,
        default="1,8,16,32,64,128",
        help="并发阶梯，逗号分隔",
    )
    ap.add_argument("--tokens", type=int, default=200, help="每条回复的字数")
    ap.add_argument(
        "--chunk-delay",
        type=float,
        default=0.0,
        help="假模型每帧间隔（秒）。0 = 饱和压测；0.02 ≈ 50 token/s",
    )
    ap.add_argument("--label", default="run", help="这轮压测的标签")
    ap.add_argument("--timeout", type=float, default=300.0, help="单条流超时（秒）")
    ap.add_argument("--api-seconds", type=float, default=5.0, help="非流式接口压测秒数")
    ap.add_argument("--level-max", type=int, default=200, help="客户端连接池上限")
    ap.add_argument(
        "--out",
        default=str(Path(tempfile.gettempdir()) / "web_agent_bench.json"),
        help="JSON 结果落盘路径",
    )
    args = ap.parse_args()
    args.levels = [int(x) for x in args.levels.split(",") if x.strip()]
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    sys.exit(main())
