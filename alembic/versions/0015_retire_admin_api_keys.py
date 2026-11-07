"""退役旧管理员用户名和 API Key 列。

Revision ID: 8c31f0b7d2a4
Revises: 5d7f2a9c4e11
Create Date: 2026-11-07
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "8c31f0b7d2a4"
down_revision = "5d7f2a9c4e11"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """删除旧管理员的用户名、展示名、role 和 API Key 字段。"""
    with op.batch_alter_table("admin_user") as batch:
        batch.drop_index("idx_admin_user_tenant")
        batch.drop_constraint("uq_admin_user_username", type_="unique")
        batch.drop_constraint("uq_admin_user_key_hash", type_="unique")
        batch.drop_column("api_key_hint")
        batch.drop_column("api_key_hash")
        batch.drop_column("role")
        batch.drop_column("display_name")
        batch.drop_column("username")
        batch.create_index("idx_admin_user_tenant", ["tenant_id"])


def downgrade() -> None:
    """恢复仅用于旧版本回滚的兼容列。"""
    with op.batch_alter_table("admin_user") as batch:
        batch.drop_index("idx_admin_user_tenant")
        batch.add_column(sa.Column("username", sa.String(64), nullable=True))
        batch.add_column(
            sa.Column("display_name", sa.String(128), nullable=True)
        )
        batch.add_column(sa.Column("role", sa.String(16), nullable=True))
        batch.add_column(
            sa.Column("api_key_hash", sa.String(128), nullable=True)
        )
        batch.add_column(sa.Column("api_key_hint", sa.String(16), nullable=True))
        batch.create_unique_constraint(
            "uq_admin_user_username", ["username"]
        )
        batch.create_unique_constraint(
            "uq_admin_user_key_hash", ["api_key_hash"]
        )
        batch.create_index(
            "idx_admin_user_tenant", ["tenant_id", "role"]
        )
