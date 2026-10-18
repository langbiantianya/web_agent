"""鉴权路由：SSE ticket 签发（设计文档 §8）。"""

from __future__ import annotations

from fastapi import APIRouter

from app.core.ticket import issue_ticket
from app.schemas.auth import SSETicketIn, SSETicketOut

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/sse-ticket", response_model=SSETicketOut)
async def create_sse_ticket(payload: SSETicketIn) -> SSETicketOut:
    """换取短期 SSE ticket。

    EventSource 不能带自定义 header，只能把凭证放进 query，所以用 60 秒有效的
    ticket 代替长期 token。传入 ``run_id`` 可把票据绑定到具体 run。
    """
    ticket, ttl = issue_ticket(user_id=payload.user_id, run_id=payload.run_id)
    return SSETicketOut(ticket=ticket, expires_in=ttl)
