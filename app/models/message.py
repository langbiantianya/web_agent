"""message 表 —— 会话历史事实来源（设计文档 §4.2）。"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import DATETIME3, PK, Base
from app.models.enums import MessageRole, MessageStatus


class Message(Base):
    __tablename__ = "message"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    run_id: Mapped[int | None] = mapped_column(sa.BigInteger, default=None)
    agent_code: Mapped[str | None] = mapped_column(sa.String(64), default=None)
    role: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    content: Mapped[str | None] = mapped_column(sa.Text, default=None)
    status: Mapped[str] = mapped_column(
        sa.String(16), default=MessageStatus.DONE.value, nullable=False
    )
    token_count: Mapped[int] = mapped_column(sa.Integer, default=0)
    created_at: Mapped[object] = mapped_column(
        DATETIME3, server_default=sa.func.now(), nullable=False
    )

    __table_args__ = (
        # (conversation_id, id) 是加载会话历史的主路径；id 自增保证同会话内顺序
        sa.Index("idx_conv", "conversation_id", "id"),
        sa.Index("idx_run", "run_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        """调试用摘要。带 conversation_id，便于定位是哪段会话历史。"""
        return f"<Message {self.id} {self.role} conv={self.conversation_id}>"
