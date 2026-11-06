"""conversation 表 —— 会话（设计文档 §4.2）。"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import PK, Base
from app.models.enums import ConversationStatus


class Conversation(Base):
    __tablename__ = "conversation"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    tenant_id: Mapped[str] = mapped_column(
        sa.String(64), nullable=False, default="default"
    )
    owner_user_id: Mapped[str | None] = mapped_column(
        sa.String(36),
        sa.ForeignKey("app_user.id", name="fk_conversation_owner"),
        # 迁移期存量行可能无主；新业务写入始终要求该字段。
        nullable=True,
    )
    workflow_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    # 旧客户端字段仅为存量数据库迁移保留；新行使用服务端默认值，不参与认证、
    # 查询或 ownership。
    user_id: Mapped[str] = mapped_column(sa.String(64), default="anonymous")
    title: Mapped[str] = mapped_column(sa.String(255), default="")
    status: Mapped[str] = mapped_column(
        sa.String(16), default=ConversationStatus.ACTIVE.value, nullable=False
    )
    created_at: Mapped[object] = mapped_column(
        sa.DateTime, server_default=sa.func.now(), nullable=False
    )
    updated_at: Mapped[object] = mapped_column(
        sa.DateTime,
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
        nullable=False,
    )

    __table_args__ = (
        sa.Index("idx_user", "user_id", "created_at"),
        sa.UniqueConstraint(
            "tenant_id", "session_id", name="uq_conversation_tenant_session"
        ),
        sa.Index(
            "idx_conversation_tenant_owner_updated",
            "tenant_id",
            "owner_user_id",
            "updated_at",
            "id",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        """调试用摘要。用 session_id 而非自增 id，因为前端持有的是 session_id。"""
        return f"<Conversation {self.session_id}>"
