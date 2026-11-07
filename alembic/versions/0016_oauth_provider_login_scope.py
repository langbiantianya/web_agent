"""区分 OAuth Provider 的客户端与管理员登录用途。

Revision ID: 4e8c2a1d7f90
Revises: 8c31f0b7d2a4
Create Date: 2026-11-07
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "4e8c2a1d7f90"
down_revision = "8c31f0b7d2a4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """允许每个 Provider 独立控制客户端和管理端登录。"""
    with op.batch_alter_table("oauth_provider") as batch:
        batch.add_column(
            sa.Column(
                "client_login_enabled",
                sa.SmallInteger(),
                nullable=False,
                server_default="1",
            )
        )
        batch.add_column(
            sa.Column(
                "admin_login_enabled",
                sa.SmallInteger(),
                nullable=False,
                server_default="1",
            )
        )


def downgrade() -> None:
    """移除登录用途开关并恢复共享 Provider 行为。"""
    with op.batch_alter_table("oauth_provider") as batch:
        batch.drop_column("admin_login_enabled")
        batch.drop_column("client_login_enabled")
