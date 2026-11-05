"""MCP OAuth 凭据与租户隔离。

Revision ID: b7c1d9e2f4a6
Revises: 8a5e3d0c2b11
Create Date: 2026-11-05
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql as mysql_dialect

revision = "b7c1d9e2f4a6"
down_revision = "8a5e3d0c2b11"
branch_labels = None
depends_on = None

DATETIME3 = sa.DateTime().with_variant(mysql_dialect.DATETIME(fsp=3), "mysql")


def upgrade() -> None:
    """给 MCP 服务器增加租户和 OAuth 机器身份字段。"""
    with op.batch_alter_table("mcp_server") as batch:
        batch.add_column(
            sa.Column(
                "tenant_id",
                sa.String(length=64),
                nullable=False,
                server_default="default",
            )
        )
        batch.add_column(
            sa.Column(
                "auth_type",
                sa.String(length=32),
                nullable=False,
                server_default="static",
            )
        )
        batch.add_column(sa.Column("oauth_token_url", sa.String(length=512)))
        batch.add_column(sa.Column("oauth_revocation_url", sa.String(length=512)))
        batch.add_column(sa.Column("oauth_client_id", sa.String(length=256)))
        batch.add_column(sa.Column("oauth_client_secret_cipher", sa.String(length=4096)))
        batch.add_column(sa.Column("oauth_scopes", sa.JSON()))
        batch.add_column(sa.Column("oauth_resource", sa.String(length=512)))
        batch.add_column(sa.Column("oauth_access_token_cipher", sa.String(length=8192)))
        batch.add_column(sa.Column("oauth_refresh_token_cipher", sa.String(length=8192)))
        batch.add_column(sa.Column("oauth_expires_at", DATETIME3))
        batch.drop_index("idx_mcp_server_enabled")
        batch.create_index(
            "idx_mcp_server_tenant_enabled", ["tenant_id", "enabled"]
        )


def downgrade() -> None:
    """回退 MCP OAuth 与租户字段。"""
    with op.batch_alter_table("mcp_server") as batch:
        batch.drop_index("idx_mcp_server_tenant_enabled")
        batch.create_index("idx_mcp_server_enabled", ["enabled"])
        batch.drop_column("oauth_expires_at")
        batch.drop_column("oauth_refresh_token_cipher")
        batch.drop_column("oauth_access_token_cipher")
        batch.drop_column("oauth_resource")
        batch.drop_column("oauth_scopes")
        batch.drop_column("oauth_client_secret_cipher")
        batch.drop_column("oauth_client_id")
        batch.drop_column("oauth_revocation_url")
        batch.drop_column("oauth_token_url")
        batch.drop_column("auth_type")
        batch.drop_column("tenant_id")
