"""状态枚举。"""

from __future__ import annotations

from enum import StrEnum


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    # V3.1：暂停在审批节点上，等人给决策。**非终态**——它还会被恢复继续跑。
    WAITING = "waiting"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"
    # V3.1：审批被驳回，本次 run 就此终止。
    REJECTED = "rejected"

    @property
    def terminal(self) -> bool:
        """是否已到终态——终态 run 不再接受任何事件，是回放/收敛的判定依据。

        ``WAITING`` 刻意**不算**终态：它在等人审批，事件流还开着，run 还有下文。
        """
        return self in (RunStatus.DONE, RunStatus.ERROR, RunStatus.CANCELLED, RunStatus.REJECTED)


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    # V3.1：审批节点在等人决策。
    WAITING = "waiting"
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
    # V3.2：工具执行结果。与 TOOL_CALL 配对——模型要了工具，这里告诉前端
    # 「调完了、结果是什么、成功还是失败」。
    TOOL_RESULT = "tool_result"
    ERROR = "error"
    DONE = "done"
    # V3.1：人工审批的三个事件。它们都**不是**终止事件——「等审批」时流要继续
    # 开着，批准后同一条流上继续吐字；驳回之后会紧跟一个 done 事件收尾。
    APPROVAL_REQUIRED = "approval_required"
    APPROVED = "approved"
    REJECTED = "rejected"

    @property
    def terminal(self) -> bool:
        """收到该类型事件后流即可关闭。"""
        return self in (EventType.DONE, EventType.ERROR)


TERMINAL_RUN_STATUSES = (RunStatus.DONE, RunStatus.ERROR, RunStatus.CANCELLED, RunStatus.REJECTED)
