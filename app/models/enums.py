"""状态枚举。"""

from __future__ import annotations

from enum import StrEnum


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        """是否已到终态——终态 run 不再接受任何事件，是回放/收敛的判定依据。"""
        return self in (RunStatus.DONE, RunStatus.ERROR, RunStatus.CANCELLED)


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


class MessageStatus(StrEnum):
    PENDING = "pending"
    STREAMING = "streaming"
    DONE = "done"
    ERROR = "error"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class ConversationStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class EventType(StrEnum):
    """占 seq 的事件类型。心跳刻意不在此列——它走 SSE 注释行，不落库。"""

    NODE_START = "node_start"
    NODE_END = "node_end"
    TOKEN = "token"
    REASONING = "reasoning"
    TOOL_CALL = "tool_call"
    ERROR = "error"
    DONE = "done"

    @property
    def terminal(self) -> bool:
        """收到该类型事件后流即可关闭。"""
        return self in (EventType.DONE, EventType.ERROR)


TERMINAL_RUN_STATUSES = (RunStatus.DONE, RunStatus.ERROR, RunStatus.CANCELLED)
