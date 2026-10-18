"""Conversation 数据访问。"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.enums import ConversationStatus


class ConversationRepo:
    async def create(self, db: AsyncSession, **kwargs) -> Conversation:
        """新建会话并 commit + refresh，返回对象带齐自增 id。

        Args:
            db: 依赖注入的请求级 AsyncSession。
            **kwargs: conversation 表字段，含前端持有的 session_id 与 workflow_id。
        """
        conv = Conversation(**kwargs)
        db.add(conv)
        await db.commit()
        await db.refresh(conv)
        return conv

    async def get_by_id(self, db: AsyncSession, conv_id: int) -> Conversation | None:
        """按自增 id 取会话。"""
        return await db.get(Conversation, conv_id)

    async def get_by_session_id(self, db: AsyncSession, session_id: str) -> Conversation | None:
        """按前端 session_id 定位会话（刷新页面后据此找回原会话）。"""
        return await db.scalar(sa.select(Conversation).where(Conversation.session_id == session_id))

    async def list_by_user(
        self,
        db: AsyncSession,
        user_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Conversation]:
        """列出某用户的会话，按 id 倒序（新会话在前）+ 分页。"""
        stmt = (
            sa.select(Conversation)
            .where(Conversation.user_id == user_id)
            .order_by(Conversation.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(await db.scalars(stmt))

    async def lock_by_id(self, db: AsyncSession, conv_id: int) -> Conversation | None:
        """加行锁读取，用于串行化「同一会话并发发消息」的检查（返回 409）。"""
        stmt = (
            sa.select(Conversation).where(Conversation.id == conv_id).with_for_update()
        )
        return await db.scalar(stmt)

    async def archive(self, db: AsyncSession, conv_id: int) -> bool:
        """把会话置为 archived（软删除，消息与 run 记录保留）；不存在返回 False。

        只改 status 不删数据，便于用户之后回看历史或做审计。
        """
        conv = await self.get_by_id(db, conv_id)
        if conv is None:
            return False
        conv.status = ConversationStatus.ARCHIVED.value
        await db.commit()
        return True


conversation_repo = ConversationRepo()
