"""Message 数据访问 + 上下文装配（设计文档 §4.4）。"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.models.enums import MessageRole
from app.models.message import Message


def estimate_tokens(text: str | None) -> int:
    """粗略 token 估算。

    CJK 字符按 1 token/字，其余按 4 字符/token（英文经验值）。
    仅用于上下文预算裁剪，不追求与具体分词器完全一致。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return cjk + (other + 3) // 4


class MessageRepo:
    async def add(self, db: AsyncSession, **kwargs) -> Message:
        """追加一条消息，只 flush 不 commit——由调用方决定事务边界。

        未显式传 ``token_count`` 时用 :func:`estimate_tokens` 现算，
        供后续上下文装配按预算裁剪，避免每轮再全文重算。
        """
        if not kwargs.get("token_count"):
            kwargs["token_count"] = estimate_tokens(kwargs.get("content"))
        msg = Message(**kwargs)
        db.add(msg)
        await db.flush()
        return msg

    async def get_by_id(self, db: AsyncSession, msg_id: int) -> Message | None:
        """按自增 id 取消息。"""
        return await db.get(Message, msg_id)

    async def list_by_conversation(
        self,
        db: AsyncSession,
        conversation_id: int,
        limit: int = 200,
        offset: int = 0,
    ) -> list[Message]:
        """按 id 正序（时间顺序）分页列出会话消息，供历史展示。"""
        stmt = (
            sa.select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.id.asc())
            .limit(limit)
            .offset(offset)
        )
        return list(await db.scalars(stmt))

    async def list_by_run(self, db: AsyncSession, run_id: int) -> list[Message]:
        """列出某次 run 产生的消息（按 id 正序），用于核对一次执行的完整产出。"""
        stmt = (
            sa.select(Message)
            .where(Message.run_id == run_id)
            .order_by(Message.id.asc())
        )
        return list(await db.scalars(stmt))

    async def load_context(
        self,
        db: AsyncSession,
        conversation_id: int,
        max_tokens: int | None = None,
        max_messages: int | None = None,
        exclude_pending: bool = True,
    ) -> list[dict]:
        """按 token 预算从近到远回溯，装配 LLM messages。

        ``exclude_pending``：跳过仍在 streaming 的消息（上一个 run 的半成品），
        避免把不完整内容当成事实喂给模型。
        """
        max_tokens = max_tokens or settings.context_max_tokens
        max_messages = max_messages or settings.context_max_messages

        stmt = (
            sa.select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.id.desc())
            .limit(max_messages)
        )
        if exclude_pending:
            stmt = stmt.where(Message.status != "streaming")

        rows = list(await db.scalars(stmt))
        result: list[dict] = []
        used = 0
        # rows 是 seq 倒序（新→旧），反向遍历即按时间正序回溯。
        # 注意：此处直接返回 result，不要再 reversed 一次——那会把顺序又翻回去。
        for m in reversed(rows):
            cost = m.token_count or estimate_tokens(m.content)
            if used + cost > max_tokens:
                break
            result.append({"role": m.role, "content": m.content or ""})
            used += cost
        return result


message_repo = MessageRepo()

__all__ = ["MessageRepo", "message_repo", "estimate_tokens", "MessageRole"]
