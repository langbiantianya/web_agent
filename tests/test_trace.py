"""W3C traceparent 校验与跨工具上下文传播。"""

from __future__ import annotations

from app.core.studio_context import (
    StudioExecContext,
    current_exec_context,
    reset_exec_context,
    set_exec_context,
)
from app.core.trace import (
    child_traceparent,
    current_traceparent,
    new_traceparent,
    reset_traceparent,
    set_traceparent,
    valid_traceparent,
)


def test_traceparent_is_valid_and_child_keeps_trace_id():
    """子 span 更换 span_id，不更换 trace_id。"""
    parent = new_traceparent()
    assert valid_traceparent(parent)
    child = child_traceparent(parent)
    assert valid_traceparent(child)
    assert child.split("-")[1] == parent.split("-")[1]
    assert child.split("-")[2] != parent.split("-")[2]


def test_invalid_incoming_trace_is_replaced():
    """非法请求头不会原样透传。"""
    token = set_traceparent("not-a-trace")
    try:
        assert valid_traceparent(current_traceparent())
    finally:
        reset_traceparent(token)


def test_studio_context_carries_traceparent():
    """工具执行上下文保留本次 child trace。"""
    trace = child_traceparent()
    token = set_exec_context(
        StudioExecContext(
            run_id="1",
            node_id="n",
            tool_call_id="call",
            traceparent=trace,
        )
    )
    try:
        assert current_exec_context().traceparent == trace
    finally:
        reset_exec_context(token)
