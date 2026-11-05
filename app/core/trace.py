"""最小 W3C Trace Context 传播工具。

项目当前不引入完整 OpenTelemetry SDK；这里只保证 ``traceparent`` 能从 HTTP 入口
传到工具、studio、dispatcher 和 sandbox 日志。格式非法时生成新链路，绝不把
未校验的请求头继续透传。
"""

from __future__ import annotations

import re
import secrets
from contextvars import ContextVar, Token

_TRACEPARENT_RE = re.compile(
    r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$"
)
_current: ContextVar[str | None] = ContextVar("current_traceparent", default=None)


def new_traceparent(flags: str = "01") -> str:
    """生成一条新的 128-bit trace 链路。"""
    trace_id = secrets.token_hex(16)
    span_id = secrets.token_hex(8)
    return f"00-{trace_id}-{span_id}-{flags}"


def child_traceparent(parent: str | None = None) -> str:
    """基于父链路生成新的 span，保留 trace id。"""
    candidate = parent or current_traceparent()
    match = _TRACEPARENT_RE.fullmatch(candidate or "")
    if match is None:
        return new_traceparent()
    trace_id, _, flags = match.groups()
    return f"00-{trace_id}-{secrets.token_hex(8)}-{flags}"


def set_traceparent(value: str | None) -> Token:
    """设置当前请求链路；非法值转为新链路。"""
    cleaned = value if value and _TRACEPARENT_RE.fullmatch(value) else None
    return _current.set(cleaned or new_traceparent())


def reset_traceparent(token: Token) -> None:
    """恢复外层请求链路。"""
    _current.reset(token)


def current_traceparent() -> str | None:
    """读取当前上下文链路。"""
    return _current.get()


def valid_traceparent(value: str | None) -> bool:
    """校验 W3C traceparent 形状。"""
    if not value:
        return False
    match = _TRACEPARENT_RE.fullmatch(value)
    if match is None:
        return False
    trace_id, span_id, _ = match.groups()
    return set(trace_id) != {"0"} and set(span_id) != {"0"}
