"""沙箱的输入 / 输出协议（skill-service §2.4、§9）。

这个模块**不依赖任何第三方库**，要同时被三处 import：

- ``services/sandbox/runner.py``（沙箱镜像里）；
- ``services/dispatcher``（调度器）；
- ``services/studio``（通过 HTTP 调调度器）。

## 为什么输出不是「容器日志原样当 stdout」

容器日志里混着控制信息（退出码、截断标记），而脚本输出可能很大。直接拿日志当
stdout 会对不齐 fast 档的语义（超限停止读取），也没法可靠区分「脚本没输出」与
「runner 没跑起来」。

所以 runner **独占容器 stdout** 发控制帧，脚本的 stdout/stderr 被它重定向到自己的
管道；dispatcher 只解析控制帧。

## 为什么分片 4 KB

Docker 的 json-file driver 与 CRI 都会在 16 KB 处切行；4 KB 的 base64 分片（约
5.5 KB）远低于这个阈值。万一运行时还是切了行，非 ``\\x1eSTUDIO1`` 开头的行会被当成
上一片的续行。
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

# 控制帧的行首标记。用 ``\x1e``（记录分隔符）降低和用户输出撞车的概率。
FRAME_MARKER = "\x1eSTUDIO1"

# 一片原始字节的大小。base64 之后约 5.5 KB，低于 16 KB 的切行阈值。
CHUNK_SIZE = 4096

# 默认输出上限；与 studio 的 §9.2 一致。
MAX_STDOUT_BYTES = 1024 * 1024
MAX_STDERR_BYTES = 256 * 1024

# 请求里的脚本正文上限（与调用方一致）。
MAX_CONTENT_CHARS = 200_000
MAX_STDIN_CHARS = 256 * 1024
MAX_ARGV_ITEMS = 64
MAX_ARGV_ITEM_CHARS = 4096

# 控制帧的总大小上限（防 runner 自己失控）。
MAX_FRAME_BYTES = 4 * 1024 * 1024


class FrameError(ValueError):
    """控制帧不完整 / 摘要不匹配 / 编码坏了。"""


@dataclass(frozen=True, slots=True)
class SandboxSpec:
    """一次沙箱执行的输入。"""

    skill: str = ""
    tenant_id: str = "default"
    script: str = ""
    language: str = "python"
    content: str = ""
    argv: tuple[str, ...] = ()
    stdin: str = ""
    timeout_ms: int = 0
    image: str = ""
    request_id: str = ""
    traceparent: str = ""

    def payload(self, nonce: str) -> dict[str, Any]:
        """转成 runner 读的 payload。

        Args:
            nonce: 本次执行的随机串——控制帧要用它防串线。

        Returns:
            可 JSON 序列化的 dict。
        """
        return {
            "nonce": nonce,
            "skill": self.skill,
            "tenant_id": self.tenant_id,
            "script": self.script,
            "language": self.language,
            "content": self.content,
            "argv": list(self.argv),
            "stdin": self.stdin,
            "timeout_ms": self.timeout_ms,
            "traceparent": self.traceparent,
            "limits": {
                "stdout_bytes": MAX_STDOUT_BYTES,
                "stderr_bytes": MAX_STDERR_BYTES,
            },
        }


@dataclass(frozen=True, slots=True)
class SandboxResult:
    """一次沙箱执行的结果。"""

    status: str = "failed"
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    duration_ms: int = 0
    error: str = ""
    error_code: str = ""
    startup_ms: int = 0
    image_startup_ms: int = 0
    sandbox_id: str = ""
    cpu_seconds: float = 0.0
    peak_memory_bytes: int = 0


@dataclass(slots=True)
class _FrameState:
    """正在拼装的控制帧。"""

    meta: dict[str, Any] | None = None
    stdout_chunks: dict[int, bytes] = field(default_factory=dict)
    stderr_chunks: dict[int, bytes] = field(default_factory=dict)
    digest: str = ""
    ended: bool = False


def build_payload(spec: SandboxSpec, nonce: str) -> bytes:
    """把 spec 序列化成 runner 读的 payload 字节。

    Args:
        spec: 执行输入。
        nonce: 本次执行的随机串。

    Returns:
        UTF-8 JSON 字节。
    """
    return json.dumps(
        spec.payload(nonce), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def parse_payload(raw: bytes) -> dict[str, Any]:
    """解析 runner 的 payload。

    Args:
        raw: JSON 字节。

    Returns:
        dict。

    Raises:
        FrameError: 不是合法 JSON，或关键字段缺失/超限。
    """
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FrameError(f"payload 不是合法的 UTF-8 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise FrameError("payload 顶层必须是对象")
    if not data.get("nonce"):
        raise FrameError("payload 缺少 nonce")
    content = data.get("content")
    if not isinstance(content, str) or not content.strip():
        raise FrameError("payload 缺少 content")
    if len(content) > MAX_CONTENT_CHARS:
        raise FrameError("payload 的 content 超长")
    stdin = data.get("stdin") or ""
    if not isinstance(stdin, str) or len(stdin) > MAX_STDIN_CHARS:
        raise FrameError("payload 的 stdin 非法或超长")
    argv = data.get("argv") or []
    if not isinstance(argv, list) or len(argv) > MAX_ARGV_ITEMS:
        raise FrameError("payload 的 argv 非法或超长")
    for item in argv:
        if not isinstance(item, str) or len(item) > MAX_ARGV_ITEM_CHARS:
            raise FrameError("payload 的 argv 单项非法或超长")
    return data


def _b64(data: bytes) -> str:
    """base64 编码（不带换行）。"""
    return base64.b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    """base64 解码；坏数据抛 :class:`FrameError`。"""
    try:
        return base64.b64decode(text, validate=True)
    except (ValueError, TypeError) as exc:
        raise FrameError(f"控制帧的 base64 坏了：{exc}") from exc


def digest_of(stdout: bytes, stderr: bytes) -> str:
    """计算 stdout/stderr 的联合摘要（END 帧用）。"""
    hasher = hashlib.sha256()
    hasher.update(stdout)
    hasher.update(b"\x00")
    hasher.update(stderr)
    return hasher.hexdigest()


def encode_frames(
    *,
    nonce: str,
    exit_code: int,
    truncated: bool,
    stdout: bytes,
    stderr: bytes,
    duration_ms: int = 0,
    status: str = "completed",
    error_code: str = "",
    cpu_seconds: float = 0.0,
    peak_memory_bytes: int = 0,
) -> str:
    """把结果编成控制帧文本（runner 用）。

    Args:
        nonce: 本次执行的随机串。
        exit_code: 脚本退出码。
        truncated: 输出是否被截断。
        stdout: stdout 原始字节。
        stderr: stderr 原始字节。
        duration_ms: 执行耗时。
        status: ``completed`` / ``timeout`` / ``limited`` / ``failed``。
        error_code: 机器可读的失败码。
        cpu_seconds: cgroup 统计的 CPU 秒数。
        peak_memory_bytes: cgroup 统计的内存峰值（字节）。

    Returns:
        多行文本，每行一个控制帧。
    """
    meta = {
        "exit_code": exit_code,
        "truncated": truncated,
        "stdout_bytes": len(stdout),
        "stderr_bytes": len(stderr),
        "duration_ms": duration_ms,
        "status": status,
        "error_code": error_code,
        "cpu_seconds": max(0.0, float(cpu_seconds)),
        "peak_memory_bytes": max(0, int(peak_memory_bytes)),
    }
    lines = [
        f"{FRAME_MARKER} {nonce} META {_b64(json.dumps(meta, separators=(',', ':')).encode('utf-8'))}"
    ]
    for seq, start in enumerate(range(0, len(stdout), CHUNK_SIZE)):
        lines.append(
            f"{FRAME_MARKER} {nonce} OUT {seq} {_b64(stdout[start:start + CHUNK_SIZE])}"
        )
    for seq, start in enumerate(range(0, len(stderr), CHUNK_SIZE)):
        lines.append(
            f"{FRAME_MARKER} {nonce} ERR {seq} {_b64(stderr[start:start + CHUNK_SIZE])}"
        )
    lines.append(f"{FRAME_MARKER} {nonce} END {digest_of(stdout, stderr)}")
    return "\n".join(lines) + "\n"


class FrameAssembler:
    """把控制帧文本拼回结果（dispatcher 用）。"""

    def __init__(self) -> None:
        """建一个空的拼装器。"""
        self._state = _FrameState()
        self._nonce: str | None = None
        self._total = 0

    def feed(self, line: str) -> None:
        """喂一行日志。

        Args:
            line: 一行文本（可带 \\r\\n）。

        Raises:
            FrameError: 帧的格式不对，或总大小超限。
        """
        cleaned = line.rstrip("\r\n")
        if not cleaned:
            return
        self._total += len(cleaned)
        if self._total > MAX_FRAME_BYTES:
            raise FrameError("控制帧总大小超过上限")
        if not cleaned.startswith(FRAME_MARKER):
            # 不是控制帧：容器里别的输出（比如 runner 崩溃时的 stderr 被误读），
            # 忽略即可。真正的帧切行由 parse_frames 先拼接再喂。
            return
        parts = cleaned.split(" ", 3)
        if len(parts) < 4:
            raise FrameError(f"控制帧格式不对：{cleaned[:80]!r}")
        _marker, nonce, kind, rest = parts
        if self._nonce is None:
            self._nonce = nonce
        elif nonce != self._nonce:
            raise FrameError("控制帧的 nonce 不一致（串线或伪造）")
        if kind == "META":
            raw = _unb64(rest)
            try:
                meta = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise FrameError(f"META 不是合法 JSON：{exc}") from exc
            if not isinstance(meta, dict):
                raise FrameError("META 顶层必须是对象")
            self._state.meta = meta
        elif kind in ("OUT", "ERR"):
            seq_text, _, b64_text = rest.partition(" ")
            try:
                seq = int(seq_text)
            except ValueError as exc:
                raise FrameError(f"分片序号不是整数：{seq_text!r}") from exc
            target = (
                self._state.stdout_chunks if kind == "OUT" else self._state.stderr_chunks
            )
            target[seq] = _unb64(b64_text)
        elif kind == "END":
            self._state.digest = rest.strip()
            self._state.ended = True
        # 未知 kind：忽略（向前兼容）。

    def finish(self) -> dict[str, Any]:
        """收尾并校验。

        Returns:
            ``{exit_code, truncated, stdout(bytes), stderr(bytes), duration_ms}``。

        Raises:
            FrameError: 缺 META / END，或摘要不匹配。
        """
        state = self._state
        if state.meta is None:
            raise FrameError("缺少 META 帧")
        if not state.ended:
            raise FrameError("缺少 END 帧")
        stdout = b"".join(state.stdout_chunks[k] for k in sorted(state.stdout_chunks))
        stderr = b"".join(state.stderr_chunks[k] for k in sorted(state.stderr_chunks))
        if digest_of(stdout, stderr) != state.digest:
            raise FrameError("END 摘要不匹配（结果可能被截断或篡改）")
        meta = state.meta
        return {
            "exit_code": int(meta.get("exit_code", -1)),
            "truncated": bool(meta.get("truncated", False)),
            "stdout": stdout,
            "stderr": stderr,
            "duration_ms": int(meta.get("duration_ms", 0)),
            "status": str(meta.get("status") or "completed"),
            "error_code": str(meta.get("error_code") or ""),
            "cpu_seconds": max(0.0, float(meta.get("cpu_seconds") or 0)),
            "peak_memory_bytes": max(
                0, int(meta.get("peak_memory_bytes") or 0)
            ),
        }


def parse_frames(text: str) -> dict[str, Any]:
    """一次性解析控制帧文本。

    Args:
        text: 日志文本。

    Returns:
        同 :meth:`FrameAssembler.finish`。

    Raises:
        FrameError: 控制帧缺失或坏掉。
    """
    assembler = FrameAssembler()
    merged: list[str] = []
    # **不能**用 str.splitlines()：它会把 \\x1e（我们的帧标记首字符）当成换行，
    # 结果第一行永远是空串，META 永远找不到。只按真实的 \\n 切。
    for line in text.split("\n"):
        # 运行时（Docker json-file / CRI）可能在 16 KB 处切行；我们的分片远低于
        # 这个阈值，但万一被切了，非标记行要接回上一行再解析。
        if line.startswith(FRAME_MARKER) or not merged:
            merged.append(line)
        else:
            merged[-1] += line
    for line in merged:
        assembler.feed(line)
    return assembler.finish()
