"""API 层公共依赖。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.db import get_db
from app.core.errors import UnauthorizedError
from app.core.ticket import verify_ticket

DbSession = Annotated[AsyncSession, Depends(get_db)]


def sse_ticket(
    ticket: Annotated[str, Query(description="由 /api/auth/sse-ticket 签发的短期票据")],
) -> dict:
    """SSE 连接鉴权依赖。EventSource 不能带自定义 header，只能走 query。"""
    return verify_ticket(ticket)


TicketDep = Annotated[dict, Depends(sse_ticket)]


def resume_from_seq(
    last_seq: Annotated[int, Query(ge=0, description="上次收到的 seq")] = 0,
    last_event_id: Annotated[
        str | None,
        Header(alias="last-event-id", description="浏览器自动重连时携带"),
    ] = None,
) -> int:
    """断点位置：优先用 ``Last-Event-ID`` 头，回退到 ``last_seq`` query。

    浏览器内置的 EventSource 重连会自动带上 Last-Event-ID；前端主动重连则用
    query 传 last_seq。两者都支持才能无缝覆盖两种重连方式（设计文档 §5.5）。
    """
    if last_event_id:
        try:
            parsed = int(last_event_id)
        except (TypeError, ValueError):
            parsed = 0
        return max(parsed, last_seq)
    return last_seq


ResumeFrom = Annotated[int, Depends(resume_from_seq)]


__all__ = [
    "DbSession",
    "ResumeFrom",
    "TicketDep",
    "UnauthorizedError",
    "get_db",
    "resume_from_seq",
    "sse_ticket",
]
