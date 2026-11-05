"""一次工具调用所在的执行上下文（设计文档 §22.8.5、skill-service §5.2）。

## 为什么需要它

studio 侧有一个**去重窗口**：同一个 ``request_id`` 在窗口内不会被执行第二遍
（skill-service §5.2）。它的用途是「调用方超时重试时不把脚本跑两遍」。

要让它真的有用，``request_id`` 必须满足两条：

1. **同一次逻辑调用算出来一样**——否则重试时 studio 会当成新请求，脚本跑两遍；
2. **不同的调用算出来不一样**——否则模型合法地「想再跑一遍」也会被吞掉。

所以它的来源是「**run + 节点 + tool_call_id**」的组合，**不是脚本内容的哈希**：
按内容哈希会把「模型再跑一次同样的脚本」也判成重复——那是错的，脚本可能压根不
幂等（它读的是当下的数据）。

## 为什么用 contextvars

一次 run 里可能有多个节点**并发**跑（§15.5 的并行分支），普通模块级变量在
并发下会互相踩。Phase 1 的技能白名单（:mod:`app.core.skill_context`）也是同一
个理由用 contextvars 的。
"""

from __future__ import annotations

import hashlib
import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StudioExecContext:
    """当前这次工具调用挂在哪个 run 的哪个节点的哪次调用上。"""

    run_id: str = ""
    node_id: str = ""
    tool_call_id: str = ""
    traceparent: str = ""


_current: ContextVar[StudioExecContext | None] = ContextVar(
    "studio_exec_context", default=None
)


def set_exec_context(ctx: StudioExecContext | None) -> Token:
    """设置当前执行上下文，返回 Token 供复位。

    Args:
        ctx: 上下文；``None`` 表示清空。

    Returns:
        复位用的 Token。
    """
    return _current.set(ctx)


def reset_exec_context(token: Token) -> None:
    """复位执行上下文。

    不复位的话下一个节点会继承上一个节点的 tool_call_id，于是两次不同的调用
    会算出同一个 ``request_id``——studio 的去重窗口会把第二次**静默吞掉**。

    Args:
        token: :func:`set_exec_context` 返回的 Token。
    """
    _current.reset(token)


def current_exec_context() -> StudioExecContext | None:
    """当前执行上下文；不在一次 run 里时返回 ``None``。"""
    return _current.get()


def make_request_id(skill: str, script: str, argv: list[str] | None = None) -> str:
    """算出这次调用的 ``request_id``。

    Args:
        skill: 技能 code。
        script: 脚本路径。
        argv: 传给脚本的参数。

    Returns:
        追踪键。不在一次 run 里时返回一个随机 id（每次都不同——没有 run 上下文
        时无法判断两次调用是不是「同一次」，宁可当成两次）。
    """
    ctx = current_exec_context()
    if ctx is None:
        return uuid.uuid4().hex
    raw = "|".join(
        [ctx.run_id, ctx.node_id, ctx.tool_call_id, skill, script, ",".join(argv or [])]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
