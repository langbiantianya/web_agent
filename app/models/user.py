"""终端用户、外部身份绑定与服务端登录会话。"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import DATETIME3, PK, Base


class AppUser(Base):
    """本地终端用户。

    ``id`` 是不可变、不透明的 UUID 字符串，也是历史归属的唯一外键目标。
    username、phone、email 只用于展示和检索，不能替代认证身份。
    """

    __tablename__ = "app_user"

    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    username: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    phone: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    phone_verified: Mapped[int | None] = mapped_column(sa.SmallInteger, nullable=True)
    email: Mapped[str | None] = mapped_column(sa.String(320), nullable=True)
    email_verified: Mapped[int | None] = mapped_column(sa.SmallInteger, nullable=True)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="active")
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
        sa.UniqueConstraint("tenant_id", "username", name="uq_app_user_tenant_username"),
        sa.UniqueConstraint("tenant_id", "phone", name="uq_app_user_tenant_phone"),
        sa.UniqueConstraint("tenant_id", "email", name="uq_app_user_tenant_email"),
        sa.Index("idx_app_user_tenant_status", "tenant_id", "status"),
    )


class UserIdentity(Base):
    """外部 OIDC ``(issuer, subject)`` 与本地用户的永久绑定。"""

    __tablename__ = "user_identity"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        sa.String(36),
        sa.ForeignKey("app_user.id", name="fk_user_identity_app_user"),
        nullable=False,
    )
    provider_id: Mapped[int] = mapped_column(
        sa.BigInteger,
        sa.ForeignKey("oauth_provider.id", name="fk_user_identity_provider"),
        nullable=False,
    )
    issuer: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    subject: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    created_at: Mapped[object] = mapped_column(
        DATETIME3, server_default=sa.func.now(), nullable=False
    )

    __table_args__ = (
        sa.UniqueConstraint("issuer", "subject", name="uq_user_identity_issuer_subject"),
        sa.UniqueConstraint("user_id", "provider_id", name="uq_user_identity_user_provider"),
        sa.Index("idx_user_identity_user", "user_id"),
    )


class UserSession(Base):
    """可立即撤销的服务端浏览器会话。

    数据库只保存 session token 和 CSRF token 的 SHA-256。每 5 分钟最多滑动
    一次空闲期限，避免每个读请求都追加一次写。
    """

    __tablename__ = "user_session"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    csrf_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(
        sa.String(36),
        sa.ForeignKey("app_user.id", name="fk_user_session_app_user"),
        nullable=False,
    )
    auth_at: Mapped[object] = mapped_column(DATETIME3, nullable=False)
    last_seen_at: Mapped[object] = mapped_column(DATETIME3, nullable=False)
    idle_expires_at: Mapped[object] = mapped_column(DATETIME3, nullable=False)
    absolute_expires_at: Mapped[object] = mapped_column(DATETIME3, nullable=False)
    revoked_at: Mapped[object | None] = mapped_column(DATETIME3, nullable=True)
    created_at: Mapped[object] = mapped_column(
        DATETIME3, server_default=sa.func.now(), nullable=False
    )

    __table_args__ = (
        sa.Index("idx_user_session_user_active", "user_id", "revoked_at"),
        sa.Index("idx_user_session_expiry", "idle_expires_at", "absolute_expires_at"),
    )
