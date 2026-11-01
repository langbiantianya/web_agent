"""会话 / 消息 / run 相关 schema。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMBase


# --------------------------------------------------------------- conversation


class ConversationCreate(BaseModel):
    session_id: str | None = Field(default=None, max_length=64)
    workflow_code: str = Field(min_length=1, max_length=64)
    user_id: str = Field(default="anonymous", max_length=64)
    title: str = Field(default="", max_length=255)


class ConversationOut(ORMBase):
    id: int
    session_id: str
    workflow_id: int
    user_id: str
    title: str
    status: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ------------------------------------------------------------------- message


class SendMessageIn(BaseModel):
    content: str = Field(min_length=1)
    user_id: str = Field(default="anonymous", max_length=64)


class SendMessageOut(BaseModel):
    run_id: int
    stream_url: str
    conversation_id: int
    status: str = "pending"


class MessageOut(ORMBase):
    id: int
    conversation_id: int
    run_id: int | None = None
    agent_code: str | None = None
    role: str
    content: str | None = None
    status: str
    token_count: int = 0
    created_at: datetime | None = None


class ActiveRunOut(BaseModel):
    run_id: int
    status: str
    current_node: str | None = None
    last_seq: int = 0
    stream_url: str


# ----------------------------------------------------------------------- run


class RunStepOut(ORMBase):
    id: int
    run_id: int
    node_id: str
    agent_code: str
    status: str
    input_json: dict | None = None
    output_json: dict | None = None
    error_msg: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    # 每次尝试的明细（一条一个尝试）。`status` / `error_msg` 只反映**最近一次**
    # 尝试；要复盘「第 1 次超时、第 2 次才成功」看这个数组。
    attempts_json: list[dict] = []


class RunOut(ORMBase):
    id: int
    conversation_id: int
    workflow_id: int
    status: str
    current_node: str | None = None
    last_seq: int = 0
    error_msg: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime | None = None


class RunDetailOut(RunOut):
    steps: list[RunStepOut] = []


class CancelOut(BaseModel):
    run_id: int
    status: str
    cancelled: bool


class ApprovalOut(ORMBase):
    """一条待审批项（V3.1）。

    审批人看到的就是这些：哪个节点要批、批什么、什么时候提出的。

    继承 ``ORMBase``（而不是直接 ``BaseModel``）是因为它要直接从 ``run_approval``
    的 ORM 实例上校验——少一个 ``from_attributes`` 就会在这一步炸掉。
    """

    id: int
    run_id: int
    node_id: str
    status: str
    prompt: str = ""
    comment: str | None = None
    decided_by: str | None = None
    requested_at: datetime | None = None
    decided_at: datetime | None = None


class ApprovalDecisionIn(BaseModel):
    """提交审批决策的请求体。"""

    approved: bool
    # 驳回时建议写清原因：它会进 run 的 error_msg，也是给下游模型的上下文
    comment: str = ""
    # 谁做的决策（user_id / 姓名），只做审计用
    decidedBy: str = ""


class ApprovalDecisionOut(BaseModel):
    """提交决策后的回执。"""

    run_id: int
    approval_id: int
    approved: bool
    status: str
    comment: str | None = None
    decided_by: str | None = None
