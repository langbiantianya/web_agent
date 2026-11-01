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
        """按 token 预算从**近到远**回溯，装配 LLM messages。

        ``exclude_pending``：跳过仍在 streaming 的消息（上一个 run 的半成品），
        避免把不完整内容当成事实喂给模型。

        ## 裁剪方向：留最近的不是留最旧的

        查询按 id 倒序（新→旧）取最近 ``max_messages`` 条，然后**从最新往回**
        累加 token，超预算就停——留下的必然是**最近**的对话。

        曾经这里是反的：拿到的 rows 虽然是倒序，却从最旧的那条开始累加、超预算
        即 break，等于「预算不够就砍掉最近的、把最老的留着」。实测 10 条消息、
        预算 350 token 时返回的是 msg01..msg03，最新的 msg08..msg10 全丢；
        编排器随后还把 msg03 当成「本次用户消息」pop 掉——模型最后拿到的是
        系统提示 + 两条陈年旧账 + 本轮输入，最近几轮对话一个字都看不见。

        第一条（最新的那条）**无条件保留**：否则一条超长消息会把整段历史清空，
        而它往往正是本轮的用户输入——被丢掉之后编排器连「pop 掉本轮输入」这一步
        都做不成。
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
        # rows 是 id 倒序（新→旧），这里从最新往回取。
        for m in rows:
            cost = m.token_count or estimate_tokens(m.content)
            if result and used + cost > max_tokens:
                break
            result.append({"role": m.role, "content": m.content or ""})
            used += cost
        # 收集顺序是新→旧，返回时翻回时间正序（模型读历史需要正序）
        result.reverse()
        return result


message_repo = MessageRepo()

__all__ = ["MessageRepo", "message_repo", "estimate_tokens", "MessageRole"]
