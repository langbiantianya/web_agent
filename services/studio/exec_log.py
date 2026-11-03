"""执行汇总日志（skill-service §12.2）。

## 为什么是 JSON

日志要能被机器读：告警规则按 ``status`` 统计、面板按 ``duration_ms`` 画图、
审计按 ``request_id`` 串起一次 run。纯文本的 ``执行完成：skill=… status=…``
只能给人看。

## 为什么每条执行**一条**日志

不是逐行转发脚本输出——那会淹掉日志，而且脚本输出可能含敏感数据
（主设计 §22.10 第 2 条）。只记**字节数**与**摘要**，不记内容。

## 字段（§12.2 的样本）

```json
{"ts":"...","level":"info","event":"exec.done","request_id":"...","run_id":"...",
 "node_id":"...","skill":"financial-report","script":"scripts/extract.py",
 "language":"python","status":"COMPLETED","exit_code":0,
 "duration_ms":143,"stdout_bytes":2048,"stderr_bytes":0,"truncated":false,
 "stdout_digest":"sha256:..."}
```
"""

from __future__ import annotations

import json
import logging
import time

# logger 名。带上 ``services.studio.`` 前缀，采集侧按名字过滤就能挑出来。
LOGGER_NAME = "services.studio.exec"

# 这个 logger **不走**根 logger 的格式化器——它有自己的 handler，格式是
# ``%(message)s``，也就是**一行纯 JSON**。
#
# 为什么不能跟着普通日志一起走：普通日志的格式是
# ``2026-11-03 12:00:00 INFO    services.studio.exec: {...}``——那行 JSON 被
# 包在文本里，采集侧要正则抠出来才能解析。**汇总行是给机器读的**，就该是
# 一行一个 JSON 对象。
#
# ``propagate=False`` 是必须的：不然它还会往根 logger 冒泡，被普通格式化器
# 再打一遍——同一件事出现两次，一次纯 JSON 一次带前缀。
logger = logging.getLogger(LOGGER_NAME)
logger.setLevel(logging.INFO)
logger.propagate = False
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(_handler)

# 内部状态名 → proto 里的枚举名（样本里是 ``COMPLETED`` 这种大写形式）。
_STATUS_NAMES = {
    "completed": "COMPLETED",
    "failed": "FAILED",
    "timeout": "TIMEOUT",
    "limited": "LIMITED",
    "cancelled": "CANCELLED",
}


def _digest_with_algorithm(digest: str) -> str:
    """给摘要加上算法前缀。

    Args:
        digest: 裸的十六进制摘要。

    Returns:
        ``sha256:<hex>``；摘要为空时是空串。
    """
    if not digest:
        return ""
    if digest.startswith("sha256:"):
        return digest
    return f"sha256:{digest}"


def build_exec_log(
    *,
    request_id: str,
    run_id: str,
    node_id: str,
    skill: str,
    script: str,
    language: str,
    status: str,
    exit_code: int,
    duration_ms: int,
    stdout_bytes: int,
    stderr_bytes: int,
    truncated: bool,
    stdout_digest: str,
    profile: str = "",
    image: str = "",
    startup_ms: int = 0,
    error_code: str = "",
    level: str = "info",
    ts: str | None = None,
) -> str:
    """拼一条执行汇总日志。

    Args:
        request_id: 追踪键。
        run_id: 关联的 run。
        node_id: 关联的节点。
        skill: 技能 code。
        script: 脚本路径。
        language: 运行时。
        status: 内部状态名（``completed`` 之类）。
        exit_code: 退出码。
        duration_ms: 耗时（毫秒）。
        stdout_bytes: stdout 字节数——**不记内容**。
        stderr_bytes: stderr 字节数——**不记内容**。
        truncated: 输出是否被截断。
        stdout_digest: stdout 的 sha256 摘要。
        profile: 实际档位（``fast`` / ``isolated``）。
        image: 镜像目录名（isolated 才有意义）。
        startup_ms: 沙箱启动耗时（fast 为 0）。
        error_code: 机器可读的失败码。
        level: 日志级别。
        ts: 时间戳（ISO 8601）；不给就用当前时间。

    Returns:
        JSON 文本（一行）。
    """
    payload = {
        "ts": ts or time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "level": level,
        "event": "exec.done",
        "request_id": request_id,
        "run_id": run_id,
        "node_id": node_id,
        "skill": skill,
        "script": script,
        "language": language,
        "status": _STATUS_NAMES.get(status, status.upper()),
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "stdout_bytes": stdout_bytes,
        "stderr_bytes": stderr_bytes,
        "truncated": truncated,
        "stdout_digest": _digest_with_algorithm(stdout_digest),
        "profile": profile,
        "image": image,
        "startup_ms": startup_ms,
        "error_code": error_code,
    }
    return json.dumps(payload, ensure_ascii=False)


def emit_exec_log(**kwargs) -> str:
    """记一条执行汇总日志。

    Args:
        **kwargs: 传给 :func:`build_exec_log` 的字段。

    Returns:
        记下来的那行 JSON（测试可以直接断言）。
    """
    line = build_exec_log(**kwargs)
    logger.info("%s", line)
    return line
