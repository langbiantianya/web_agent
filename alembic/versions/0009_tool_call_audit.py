"""新增工具调用审计账本。

Revision ID: c9d4e2a71f60
Revises: a6f2c8d41b73
Create Date: 2026-11-05
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql as mysql_dialect

revision = "c9d4e2a71f60"
down_revision = "a6f2c8d41b73"
branch_labels = None
depends_on = None

DATETIME3 = sa.DateTime().with_variant(mysql_dialect.DATETIME(fsp=3), "mysql")
AUDIT_TEXT = sa.Text().with_variant(mysql_dialect.LONGTEXT(), "mysql")


def upgrade() -> None:
    """创建脱敏工具调用审计表及查询、清理索引。"""
    op.create_table(
        "tool_call_audit",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer, "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "tenant_id",
            sa.String(length=64),
            nullable=False,
            server_default="default",
        ),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("tool_call_id", sa.String(length=128), nullable=False),
        sa.Column("tool_name", sa.String(length=255), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("server", sa.String(length=128), nullable=True),
        sa.Column("arguments_json", sa.JSON(), nullable=True),
        sa.Column("arguments_redacted", sa.SmallInteger(), nullable=False),
        sa.Column("arguments_chars", sa.Integer(), nullable=False),
        sa.Column("result_text", AUDIT_TEXT, nullable=True),
        sa.Column("result_chars", sa.Integer(), nullable=False),
        sa.Column("result_bytes", sa.Integer(), nullable=False),
        sa.Column("result_truncated", sa.SmallInteger(), nullable=False),
        sa.Column("ok", sa.SmallInteger(), nullable=False),
        sa.Column("failure_type", sa.String(length=64), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", DATETIME3, nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", DATETIME3, nullable=False),
    )
    op.create_index(
        "idx_tool_audit_tenant_created",
        "tool_call_audit",
        ["tenant_id", "created_at"],
    )
    op.create_index(
        "idx_tool_audit_run_created",
        "tool_call_audit",
        ["run_id", "created_at"],
    )
    op.create_index(
        "idx_tool_audit_expires",
        "tool_call_audit",
        ["expires_at"],
    )


def downgrade() -> None:
    """删除工具调用审计表。"""
    op.drop_index("idx_tool_audit_expires", table_name="tool_call_audit")
    op.drop_index("idx_tool_audit_run_created", table_name="tool_call_audit")
    op.drop_index("idx_tool_audit_tenant_created", table_name="tool_call_audit")
    op.drop_table("tool_call_audit")
