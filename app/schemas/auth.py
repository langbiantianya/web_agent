"""SSE ticket 相关 schema。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SSETicketIn(BaseModel):
    user_id: str = Field(default="anonymous", max_length=64)
    run_id: int | None = Field(default=None, ge=1, description="绑定到具体 run")


class SSETicketOut(BaseModel):
    ticket: str
    expires_in: int
    stream_base: str = "/api/runs"
