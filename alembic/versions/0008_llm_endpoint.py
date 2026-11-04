"""新增 llm_endpoint 接口配置表。

Revision ID: a6f2c8d41b73
Revises: e8b4c2f61d93
Create Date: 2026-11-04
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql as mysql_dialect

revision = "a6f2c8d41b73"
down_revision = "e8b4c2f61d93"
branch_labels = None
depends_on = None

DATETIME3 = sa.DateTime().with_variant(mysql_dialect.DATETIME(fsp=3), "mysql")


def upgrade() -> None:
    """创建按优先级排序并加密保存 Key 的接口配置表。"""
    op.create_table(
        "llm_endpoint",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer, "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("base_url", sa.String(length=512), nullable=False),
        sa.Column("api_key_cipher", sa.String(length=512), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.SmallInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", DATETIME3, nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", DATETIME3, nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("code", name="uq_llm_endpoint_code"),
    )
    op.create_index(
        "idx_llm_endpoint_enabled_priority",
        "llm_endpoint",
        ["enabled", "priority"],
    )


def downgrade() -> None:
    """删除接口配置表。"""
    op.drop_index("idx_llm_endpoint_enabled_priority", table_name="llm_endpoint")
    op.drop_table("llm_endpoint")
