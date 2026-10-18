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
