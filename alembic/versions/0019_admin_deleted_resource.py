"""新增 admin_deleted_resource 表 —— 删除操作的短时撤销快照。

Revision ID: f4b7c1a9e2d5
Revises: d7e2b4c901af
Create Date: 2026-11-10

删除在管理端是硬删除：误删后没有恢复入口。快照表把删除前的行存下来，
在有限窗口内提供撤销，把「不可撤销」降级为「可撤销」。

列类型与 :mod:`app.models.base` 的跨库约定一致：主键用 ``PK``（SQLite
回落 Integer），时间戳用 ``DATETIME3``，payload 用 JSON 列。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql as mysql_dialect

revision = "f4b7c1a9e2d5"
down_revision = "d7e2b4c901af"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建删除快照表。"""
    op.create_table(
        "admin_deleted_resource",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("resource_code", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column("actor_username", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime().with_variant(
                mysql_dialect.DATETIME(fsp=3), "mysql"
            ),
            nullable=False,
        ),
        sa.Column(
            "expires_at",
            sa.DateTime().with_variant(
                mysql_dialect.DATETIME(fsp=3), "mysql"
            ),
            nullable=False,
        ),
    )
    op.create_index(
        "idx_admin_deleted_resource_created",
        "admin_deleted_resource",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    """删除快照表。"""
    op.drop_index(
        "idx_admin_deleted_resource_created",
        table_name="admin_deleted_resource",
    )
    op.drop_table("admin_deleted_resource")
