"""数据库管理员用户。

V4.7 起本表保存 OAuth 后台管理员。旧的 username/API Key 字段仅为迁移期数据库
列，业务不再读取或写入；管理员角色由 Casbin 决定。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import DATETIME3, PK, Base


class AdminUser(Base):
    """一个由 OAuth 身份激活的后台管理员。"""

    __tablename__ = "admin_user"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    user_id: Mapped[str | None] = mapped_column(
        sa.String(36),
        sa.ForeignKey("app_user.id", name="fk_admin_user_app_user"),
        nullable=True,
        unique=True,
    )
    provider_id: Mapped[int | None] = mapped_column(
        sa.BigInteger,
        sa.ForeignKey("oauth_provider.id", name="fk_admin_user_provider"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default="active"
    )
    last_role_sync_at: Mapped[object | None] = mapped_column(DATETIME3, nullable=True)
    enabled: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, default=1)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    last_used_at: Mapped[object | None] = mapped_column(DATETIME3, nullable=True)
    created_at: Mapped[object] = mapped_column(
        DATETIME3, server_default=sa.func.now(), nullable=False
    )
    updated_at: Mapped[object] = mapped_column(
        DATETIME3,
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
        nullable=False,
    )

    __table_args__ = (
        sa.Index("idx_admin_user_tenant", "tenant_id"),
        sa.Index("idx_admin_user_oauth_status", "status", "provider_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        """调试摘要不包含 OAuth token 或会话信息。"""
        return (
            f"<AdminUser {self.user_id} "
            f"tenant={self.tenant_id} status={self.status}>"
        )
