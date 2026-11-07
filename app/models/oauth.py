"""终端用户 OAuth 登录用的供应商与一次性登录事务。"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import DATETIME3, PK, Base


class OAuthProvider(Base):
    """一个受管 OIDC 登录供应商。

    ``client_secret_cipher`` 只保存 Fernet 密文。供应商地址由管理员配置，
    登录请求只能按 ``code`` 选择，不能提交任意 issuer 或 token endpoint。
    """

    __tablename__ = "oauth_provider"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(sa.String(64), nullable=False, default="default")
    code: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    kind: Mapped[str] = mapped_column(sa.String(32), nullable=False, default="oidc")
    issuer: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    discovery_url: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    authorization_endpoint: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    token_endpoint: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    jwks_uri: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    client_id: Mapped[str] = mapped_column(sa.String(256), nullable=False)
    client_secret_cipher: Mapped[str | None] = mapped_column(sa.String(4096), nullable=True)
    token_auth_method: Mapped[str] = mapped_column(
        sa.String(32), nullable=False, default="client_secret_basic"
    )
    scopes: Mapped[str] = mapped_column(
        sa.String(512), nullable=False, default="openid profile email phone"
    )
    username_claim: Mapped[str] = mapped_column(
        sa.String(64), nullable=False, default="preferred_username"
    )
    role_claim: Mapped[str] = mapped_column(
        sa.String(64), nullable=False, default="groups"
    )
    role_claim_required: Mapped[int] = mapped_column(
        sa.SmallInteger, nullable=False, default=1
    )
    role_sync_max_age_seconds: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=1800
    )
    auto_create: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, default=1)
    enabled: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, default=1)
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
        sa.UniqueConstraint("tenant_id", "code", name="uq_oauth_provider_tenant_code"),
        sa.Index("idx_oauth_provider_tenant_enabled", "tenant_id", "enabled"),
    )


class OAuthLoginAttempt(Base):
    """一次 authorization-code 登录事务。

    state、浏览器绑定值和 nonce 都只保存 SHA-256；PKCE verifier 必须能在回调
    时取回，所以单独加密。``consumed_at`` 的条件更新保证并发回调只有一次成功。
    """

    __tablename__ = "oauth_login_attempt"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    provider_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    state_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    browser_binding_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    nonce_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    code_verifier_cipher: Mapped[str] = mapped_column(sa.String(4096), nullable=False)
    return_to: Mapped[str] = mapped_column(sa.String(512), nullable=False, default="/")
    expires_at: Mapped[object] = mapped_column(DATETIME3, nullable=False)
    consumed_at: Mapped[object | None] = mapped_column(DATETIME3, nullable=True)
    created_at: Mapped[object] = mapped_column(
        DATETIME3, server_default=sa.func.now(), nullable=False
    )

    __table_args__ = (
        sa.Index("idx_oauth_attempt_provider_expiry", "provider_id", "expires_at"),
    )
