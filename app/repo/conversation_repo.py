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

    async def get_for_user(
        self,
        db: AsyncSession,
        conv_id: int,
        tenant_id: str,
        user_id: str,
    ) -> Conversation | None:
        """按租户和不可变 owner 取会话；越权与不存在统一返回 ``None``。"""
        return await db.scalar(
            sa.select(Conversation).where(
                Conversation.id == conv_id,
                Conversation.tenant_id == tenant_id,
                Conversation.owner_user_id == user_id,
            )
        )

    async def get_by_session_in_tenant(
        self, db: AsyncSession, session_id: str, tenant_id: str
    ) -> Conversation | None:
        """检查租户内公开 session_id 是否已被占用。"""
        return await db.scalar(
            sa.select(Conversation).where(
                Conversation.session_id == session_id,
                Conversation.tenant_id == tenant_id,
            )
        )

    async def get_by_session_for_user(
        self,
        db: AsyncSession,
        session_id: str,
        tenant_id: str,
        user_id: str,
    ) -> Conversation | None:
        """按对外 session_id 读取当前用户拥有的会话。"""
        return await db.scalar(
            sa.select(Conversation).where(
                Conversation.session_id == session_id,
                Conversation.tenant_id == tenant_id,
                Conversation.owner_user_id == user_id,
            )
        )

    async def list_for_user(
        self,
        db: AsyncSession,
        tenant_id: str,
        user_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Conversation]:
        """列出当前租户和用户的会话，按最近创建倒序。"""
        stmt = (
            sa.select(Conversation)
            .where(
                Conversation.tenant_id == tenant_id,
                Conversation.owner_user_id == user_id,
            )
            .order_by(Conversation.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(await db.scalars(stmt))

    async def lock_for_user(
        self,
        db: AsyncSession,
        conv_id: int,
        tenant_id: str,
        user_id: str,
    ) -> Conversation | None:
        """以 owner 条件加行锁，串行化同一会话的并发发送。"""
        return await db.scalar(
            sa.select(Conversation)
            .where(
                Conversation.id == conv_id,
                Conversation.tenant_id == tenant_id,
                Conversation.owner_user_id == user_id,
            )
            .with_for_update()
        )

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
