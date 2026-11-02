"""执行上下文与 request_id（设计文档 §22.8.5、skill-service §5.2）。

studio 侧的去重窗口靠 ``request_id`` 生效，所以它必须同时满足两条：

- **同一次逻辑调用算出来一样**——否则重试时脚本会被跑第二遍；
- **不同的调用算出来不一样**——否则模型合法地「想再跑一遍」也会被吞掉。

这两条都是「算错就出事」的性质，所以逐条钉住。
"""

from __future__ import annotations

from app.core.studio_context import (
    StudioExecContext,
    current_exec_context,
    make_request_id,
    reset_exec_context,
    set_exec_context,
)


def test_request_id_is_stable_within_one_call():
    """同一次调用里算两次要一样。"""
    token = set_exec_context(
        StudioExecContext(run_id="7", node_id="n1", tool_call_id="call_1")
    )
    try:
        first = make_request_id("demo", "scripts/a.py", ["--x"])
        second = make_request_id("demo", "scripts/a.py", ["--x"])
        assert first == second
    finally:
        reset_exec_context(token)


def test_request_id_differs_per_tool_call():
    """不同的 tool_call → 不同的 id。

    否则模型在同一轮里调两次同一个脚本，第二次会被 studio 的去重窗口**静默
    吞掉**——模型会看到一个「没有输出」的结果，而不是它要的第二次执行。
    """
    token = set_exec_context(
        StudioExecContext(run_id="7", node_id="n1", tool_call_id="call_1")
    )
    try:
        first = make_request_id("demo", "scripts/a.py", [])
        reset = set_exec_context(
            StudioExecContext(run_id="7", node_id="n1", tool_call_id="call_2")
        )
        second = make_request_id("demo", "scripts/a.py", [])
        reset_exec_context(reset)
        assert first != second
    finally:
        reset_exec_context(token)


def test_request_id_differs_across_nodes_and_runs():
    """不同节点 / 不同 run → 不同的 id。"""
    token = set_exec_context(
        StudioExecContext(run_id="7", node_id="n1", tool_call_id="call_1")
    )
    try:
        first = make_request_id("demo", "scripts/a.py", [])
        reset = set_exec_context(
            StudioExecContext(run_id="8", node_id="n1", tool_call_id="call_1")
        )
        second = make_request_id("demo", "scripts/a.py", [])
        reset_exec_context(reset)
        assert first != second
    finally:
        reset_exec_context(token)


def test_request_id_is_not_derived_from_script_content_only():
    """**不能**只按脚本内容算 id。

    按内容哈希会把「模型再跑一次同样的脚本」也判成重复——那是错的：脚本可能
    压根不幂等（它读的是当下的数据），模型想再跑一遍就是想拿新数据。
    """
    token = set_exec_context(
        StudioExecContext(run_id="7", node_id="n1", tool_call_id="call_1")
    )
    try:
        first = make_request_id("demo", "scripts/a.py", ["--x"])
        reset = set_exec_context(
            StudioExecContext(run_id="7", node_id="n1", tool_call_id="call_2")
        )
        second = make_request_id("demo", "scripts/a.py", ["--x"])
        reset_exec_context(reset)
        assert first != second
    finally:
        reset_exec_context(token)


def test_request_id_without_context_is_random():
    """不在一次 run 里时给随机 id。

    没有 run 上下文就无法判断两次调用是不是「同一次」——宁可当成两次。
    """
    assert current_exec_context() is None
    first = make_request_id("demo", "scripts/a.py", [])
    second = make_request_id("demo", "scripts/a.py", [])
    assert first != second


def test_reset_restores_previous_context():
    """复位要真的复位——不复位的话下一个节点会继承上一个节点的 tool_call_id，
    于是两次不同的调用算出同一个 id，第二次被 studio 静默吞掉。"""
    outer = set_exec_context(
        StudioExecContext(run_id="1", node_id="a", tool_call_id="c1")
    )
    try:
        inner = set_exec_context(
            StudioExecContext(run_id="2", node_id="b", tool_call_id="c2")
        )
        assert current_exec_context().node_id == "b"
        reset_exec_context(inner)
        assert current_exec_context().node_id == "a"
    finally:
        reset_exec_context(outer)
    assert current_exec_context() is None
