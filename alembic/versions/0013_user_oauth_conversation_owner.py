"""终端用户 OIDC、服务端会话与会话历史归属。

Revision ID: 24d9a6e8c1b3
Revises: b7c1d9e2f4a6
Create Date: 2026-11-06
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql as mysql_dialect

revision = "24d9a6e8c1b3"
down_revision = "b7c1d9e2f4a6"
branch_labels = None
depends_on = None

DATETIME3 = sa.DateTime().with_variant(mysql_dialect.DATETIME(fsp=3), "mysql")
PK = sa.BigInteger().with_variant(sa.Integer, "sqlite")


def upgrade() -> None:
    """创建终端用户认证表，并给 conversation 增加租户与 owner。"""
    op.create_table(
        "oauth_provider",
        sa.Column("id", PK, primary_key=True, autoincrement=True),
        sa.Column(
            "tenant_id", sa.String(length=64), nullable=False,
            server_default="default",
        ),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column(
            "kind", sa.String(length=32), nullable=False, server_default="oidc"
        ),
        sa.Column("issuer", sa.String(length=512), nullable=False),
        sa.Column("discovery_url", sa.String(length=512), nullable=True),
        sa.Column("authorization_endpoint", sa.String(length=512), nullable=True),
        sa.Column("token_endpoint", sa.String(length=512), nullable=True),
        sa.Column("jwks_uri", sa.String(length=512), nullable=True),
        sa.Column("client_id", sa.String(length=256), nullable=False),
        sa.Column("client_secret_cipher", sa.String(length=4096), nullable=True),
        sa.Column(
            "token_auth_method",
            sa.String(length=32),
            nullable=False,
            server_default="client_secret_basic",
        ),
        sa.Column(
            "scopes",
            sa.String(length=512),
            nullable=False,
            server_default="openid profile email phone",
        ),
        sa.Column(
            "username_claim",
            sa.String(length=64),
            nullable=False,
            server_default="preferred_username",
        ),
        sa.Column(
            "auto_create", sa.SmallInteger(), nullable=False, server_default="1"
        ),
        sa.Column("enabled", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", DATETIME3, nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", DATETIME3, nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "tenant_id", "code", name="uq_oauth_provider_tenant_code"
        ),
    )
    op.create_index(
        "idx_oauth_provider_tenant_enabled",
        "oauth_provider",
        ["tenant_id", "enabled"],
    )

    op.create_table(
        "oauth_login_attempt",
        sa.Column("id", PK, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("provider_id", sa.BigInteger(), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("browser_binding_hash", sa.String(length=64), nullable=False),
        sa.Column("nonce_hash", sa.String(length=64), nullable=False),
        sa.Column("code_verifier_cipher", sa.String(length=4096), nullable=False),
        sa.Column(
            "return_to", sa.String(length=512), nullable=False,
            server_default="/",
        ),
        sa.Column("expires_at", DATETIME3, nullable=False),
        sa.Column("consumed_at", DATETIME3, nullable=True),
        sa.Column(
            "created_at", DATETIME3, nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "idx_oauth_attempt_provider_expiry",
        "oauth_login_attempt",
        ["provider_id", "expires_at"],
    )

    op.create_table(
        "app_user",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("phone_verified", sa.SmallInteger(), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("email_verified", sa.SmallInteger(), nullable=True),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default="active"
        ),
        sa.Column(
            "created_at", DATETIME3, nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", DATETIME3, nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "tenant_id", "username", name="uq_app_user_tenant_username"
        ),
        sa.UniqueConstraint("tenant_id", "phone", name="uq_app_user_tenant_phone"),
        sa.UniqueConstraint("tenant_id", "email", name="uq_app_user_tenant_email"),
    )
    op.create_index(
        "idx_app_user_tenant_status", "app_user", ["tenant_id", "status"]
    )

    op.create_table(
        "user_identity",
        sa.Column("id", PK, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("provider_id", sa.BigInteger(), nullable=False),
        sa.Column("issuer", sa.String(length=512), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at", DATETIME3, nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.id"], name="fk_user_identity_app_user"
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"], ["oauth_provider.id"], name="fk_user_identity_provider"
        ),
        sa.UniqueConstraint(
            "issuer", "subject", name="uq_user_identity_issuer_subject"
        ),
        sa.UniqueConstraint(
            "user_id", "provider_id", name="uq_user_identity_user_provider"
        ),
    )
    op.create_index("idx_user_identity_user", "user_identity", ["user_id"])

    op.create_table(
        "user_session",
        sa.Column("id", PK, primary_key=True, autoincrement=True),
        sa.Column("token_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("csrf_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("auth_at", DATETIME3, nullable=False),
        sa.Column("last_seen_at", DATETIME3, nullable=False),
        sa.Column("idle_expires_at", DATETIME3, nullable=False),
        sa.Column("absolute_expires_at", DATETIME3, nullable=False),
        sa.Column("revoked_at", DATETIME3, nullable=True),
        sa.Column(
            "created_at", DATETIME3, nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.id"], name="fk_user_session_app_user"
        ),
    )
    op.create_index(
        "idx_user_session_user_active", "user_session", ["user_id", "revoked_at"]
    )
    op.create_index(
        "idx_user_session_expiry",
        "user_session",
        ["idle_expires_at", "absolute_expires_at"],
    )

    dialect = op.get_context().dialect.name
    with op.batch_alter_table(
        "conversation",
        naming_convention={
            "uq": "uq_%(table_name)s_%(column_0_name)s",
        },
    ) as batch:
        if dialect == "sqlite":
            batch.drop_constraint(
                "uq_conversation_session_id", type_="unique"
            )
        elif dialect == "postgresql":
            batch.drop_constraint(
                "conversation_session_id_key", type_="unique"
            )
        else:
            batch.drop_constraint("session_id", type_="unique")
        batch.add_column(
            sa.Column(
                "tenant_id",
                sa.String(length=64),
                nullable=False,
                server_default="default",
            )
        )
        batch.add_column(sa.Column("owner_user_id", sa.String(length=36), nullable=True))
        batch.create_foreign_key(
            "fk_conversation_owner",
            "app_user",
            ["owner_user_id"],
            ["id"],
        )
        batch.create_unique_constraint(
            "uq_conversation_tenant_session", ["tenant_id", "session_id"]
        )
        batch.create_index(
            "idx_conversation_tenant_owner_updated",
            ["tenant_id", "owner_user_id", "updated_at", "id"],
        )


def downgrade() -> None:
    """回退终端用户认证和 conversation 归属字段。"""
    with op.batch_alter_table("conversation") as batch:
        batch.drop_index("idx_conversation_tenant_owner_updated")
        batch.drop_constraint(
            "uq_conversation_tenant_session", type_="unique"
        )
        batch.drop_constraint("fk_conversation_owner", type_="foreignkey")
        batch.drop_column("owner_user_id")
        batch.drop_column("tenant_id")
        batch.create_unique_constraint(
            "uq_conversation_session_id", ["session_id"]
        )

    op.drop_index("idx_user_session_expiry", table_name="user_session")
    op.drop_index("idx_user_session_user_active", table_name="user_session")
    op.drop_table("user_session")
    op.drop_index("idx_user_identity_user", table_name="user_identity")
    op.drop_table("user_identity")
    op.drop_index("idx_app_user_tenant_status", table_name="app_user")
    op.drop_table("app_user")
    op.drop_index("idx_oauth_attempt_provider_expiry", table_name="oauth_login_attempt")
    op.drop_table("oauth_login_attempt")
    op.drop_index("idx_oauth_provider_tenant_enabled", table_name="oauth_provider")
    op.drop_table("oauth_provider")
