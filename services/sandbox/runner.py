"""沙箱里的脚本执行器（skill-service §6、§9、§12）。

它只做一件事：读 payload → 起一个子进程 → 收有界输出 → 用控制帧写回 stdout。
容器本身（无网络、只读 rootfs、非 root、资源限额）由 dispatcher 创建；这里的
rlimit / 进程组 / 输出上限是**第二道**。

**stdout 是控制通道**：脚本的 stdout/stderr 都被重定向到管道，只有控制帧走容器
stdout。这样 dispatcher 不用猜日志里哪段是脚本输出。
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from services.sandbox.protocol import (
    MAX_STDERR_BYTES,
    MAX_STDOUT_BYTES,
    encode_frames,
    parse_payload,
)

# runner 读 payload 的路径；测试可以覆盖它。
PAYLOAD_PATH = os.environ.get("SANDBOX_PAYLOAD", "/run/sandbox/payload.json")

# 语言 → 解释器。镜像里三样都有（skill-service §10.1）。
_LANGUAGE_SUFFIX = {"python": ".py", "bash": ".sh", "node": ".js"}

# 读取输出时每次读多少字节。
_READ_SIZE = 8192

# 结束进程组时给多久优雅退出。
_KILL_GRACE_SEC = 2.0


def _interpreter(language: str) -> list[str]:
    """按语言给解释器命令前缀。

    Args:
        language: ``python`` / ``bash`` / ``node``。

    Returns:
        命令前缀（不含脚本路径与参数）。

    Raises:
        ValueError: 语言不认识。
    """
    if language == "python":
        return [sys.executable]
    if language == "bash":
        return ["/bin/bash"]
    if language == "node":
        return ["node"]
    raise ValueError(f"不支持的运行时：{language}")


def _env(tmp: str) -> dict[str, str]:
    """子进程的环境变量白名单（§6.3）。

    Args:
        tmp: 临时工作目录（``HOME`` / ``TMPDIR`` 指向它）。

    Returns:
        环境变量字典。
    """
    env: dict[str, str] = {}
    for key in ("PATH", "LANG", "TZ"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["HOME"] = tmp
    env["TMPDIR"] = tmp
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _preexec(timeout_ms: int):  # noqa: ANN201 - 返回闭包或 None
    """POSIX 上的 rlimit 设置；非 POSIX 返回 None。

    Args:
        timeout_ms: 执行时限（用来对齐 ``RLIMIT_CPU``）。

    Returns:
        ``preexec_fn`` 或 ``None``。
    """
    if os.name != "posix":
        return None

    def _apply() -> None:
        import resource

        cpu = max(1, int(timeout_ms / 1000) + 1)
        limits = (
            (resource.RLIMIT_FSIZE, 64 * 1024 * 1024),
            (resource.RLIMIT_NOFILE, 256),
            (resource.RLIMIT_NPROC, 64),
            (resource.RLIMIT_CORE, 0),
            (resource.RLIMIT_CPU, cpu),
        )
        for which, value in limits:
            try:
                resource.setrlimit(which, (value, value))
            except (ValueError, OSError):
                pass

    return _apply


class _Capture:
    """有上限地读一个管道。"""

    def __init__(self, cap: int) -> None:
        """记住上限。

        Args:
            cap: 最多保留多少字节。
        """
        self.cap = cap
        self.data = bytearray()
        self.overflow = False
        self._lock = threading.Lock()

    def drain(self, stream) -> None:  # noqa: ANN001 - 管道对象
        """一直读到 EOF 或超限。

        Args:
            stream: 可读的二进制管道。
        """
        while True:
            try:
                chunk = stream.read(_READ_SIZE)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            with self._lock:
                room = self.cap - len(self.data)
                if room > 0:
                    self.data.extend(chunk[:room])
                if len(chunk) > room:
                    self.overflow = True
            if self.overflow:
                return


def _terminate(proc: subprocess.Popen) -> None:
    """先 SIGTERM 再 SIGKILL，杀的是**整个进程组**。

    Args:
        proc: 子进程。
    """
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (AttributeError, OSError):
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        proc.wait(timeout=_KILL_GRACE_SEC)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (AttributeError, OSError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=_KILL_GRACE_SEC)
    except subprocess.TimeoutExpired:
        pass


def _feed_stdin(proc: subprocess.Popen, data: bytes) -> None:
    """把 stdin 写进去并关掉；对端提前退出时忽略 EPIPE。

    Args:
        proc: 子进程。
        data: 要写的字节。
    """
    try:
        if data:
            proc.stdin.write(data)
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass


def run_script(payload: dict) -> dict:
    """按 payload 跑一次脚本。

    Args:
        payload: :func:`services.sandbox.protocol.parse_payload` 的结果。

    Returns:
        可直接传给 :func:`services.sandbox.protocol.encode_frames` 的字段。
    """
    language = str(payload.get("language") or "python")
    content = str(payload.get("content") or "")
    argv = [str(item) for item in (payload.get("argv") or [])]
    stdin_data = str(payload.get("stdin") or "").encode("utf-8")
    timeout_ms = int(payload.get("timeout_ms") or 60_000)
    limits = payload.get("limits") or {}
    stdout_cap = min(int(limits.get("stdout_bytes", MAX_STDOUT_BYTES)), MAX_STDOUT_BYTES)
    stderr_cap = min(int(limits.get("stderr_bytes", MAX_STDERR_BYTES)), MAX_STDERR_BYTES)

    started = time.monotonic()
    tmp = tempfile.mkdtemp(prefix="sandbox-")
    try:
        try:
            suffix = _LANGUAGE_SUFFIX[language]
        except KeyError:
            return {
                "exit_code": -1,
                "truncated": False,
                "stdout": b"",
                "stderr": f"不支持的运行时：{language}".encode("utf-8"),
                "duration_ms": 0,
                "status": "failed",
                "error_code": "unknown_language",
            }
        script_path = Path(tmp) / f"main{suffix}"
        script_path.write_text(content, encoding="utf-8")

        cmd = [*_interpreter(language), str(script_path), *argv]
        kwargs: dict = {
            "cwd": tmp,
            "env": _env(tmp),
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
        }
        preexec = _preexec(timeout_ms)
        if preexec is not None:
            kwargs["preexec_fn"] = preexec
            kwargs["start_new_session"] = True
        else:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

        try:
            proc = subprocess.Popen(cmd, **kwargs)
        except OSError as exc:
            return {
                "exit_code": -1,
                "truncated": False,
                "stdout": b"",
                "stderr": str(exc).encode("utf-8"),
                "duration_ms": int((time.monotonic() - started) * 1000),
                "status": "failed",
                "error_code": "spawn_failed",
            }

        out = _Capture(stdout_cap)
        err = _Capture(stderr_cap)
        writer = threading.Thread(
            target=_feed_stdin, args=(proc, stdin_data), daemon=True
        )
        out_reader = threading.Thread(target=out.drain, args=(proc.stdout,), daemon=True)
        err_reader = threading.Thread(target=err.drain, args=(proc.stderr,), daemon=True)
        writer.start()
        out_reader.start()
        err_reader.start()

        status = "completed"
        error_code = ""
        try:
            while proc.poll() is None:
                if out.overflow or err.overflow:
                    status = "limited"
                    error_code = "output_limit"
                    _terminate(proc)
                    break
                if (time.monotonic() - started) * 1000 > timeout_ms:
                    status = "timeout"
                    error_code = "timeout"
                    _terminate(proc)
                    break
                time.sleep(0.02)
        finally:
            _terminate(proc)
            writer.join(timeout=2)
            out_reader.join(timeout=2)
            err_reader.join(timeout=2)

        exit_code = proc.returncode if proc.returncode is not None else -1
        if status != "completed":
            exit_code = -1
        return {
            "exit_code": exit_code,
            "truncated": bool(out.overflow or err.overflow),
            "stdout": bytes(out.data),
            "stderr": bytes(err.data),
            "duration_ms": int((time.monotonic() - started) * 1000),
            "status": status,
            "error_code": error_code,
        }
    finally:
        # 工作目录写的是脚本与它产生的文件；跑完就删（只读 rootfs 下只有 /tmp 可写）。
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    """runner 的入口。

    Args:
        argv: 未使用（保留给测试）。

    Returns:
        进程退出码：0 表示控制帧已写出；非 0 表示 runner 自己失败。
    """
    try:
        payload = parse_payload(Path(PAYLOAD_PATH).read_bytes())
    except Exception as exc:  # noqa: BLE001 - runner 里任何失败都只能是「没有结果」
        print(f"sandbox runner：读不到 payload：{exc}", file=sys.stderr)
        return 2
    nonce = str(payload["nonce"])
    try:
        result = run_script(payload)
    except Exception as exc:  # noqa: BLE001
        print(f"sandbox runner：执行失败：{exc}", file=sys.stderr)
        return 3
    sys.stdout.write(encode_frames(nonce=nonce, **result))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover - 容器入口
    raise SystemExit(main())
