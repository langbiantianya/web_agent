"""数据库管理员用户。

环境变量 ``ADMIN_KEY`` 只负责首次部署的引导管理；日常管理员落在本表，密钥只存
HMAC 摘要。每个用户绑定一个租户，写操作与审计作者都从认证主体获得。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import DATETIME3, PK, Base


class AdminUser(Base):
    """一个可登录管理 API 的用户。"""

    __tablename__ = "admin_user"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(sa.String(128), nullable=False, default="")
    tenant_id: Mapped[str] = mapped_column(
        sa.String(64), nullable=False, default="default"
    )
    role: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="viewer")
    api_key_hash: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    api_key_hint: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="")
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
        sa.UniqueConstraint("username", name="uq_admin_user_username"),
        sa.UniqueConstraint("api_key_hash", name="uq_admin_user_key_hash"),
        sa.Index("idx_admin_user_tenant", "tenant_id", "role"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        """调试摘要不包含密钥摘要。"""
        return f"<AdminUser {self.username} tenant={self.tenant_id} role={self.role}>"
