"""审批会签与超时语义。

Revision ID: 8a5e3d0c2b11
Revises: f4b8d2e91c70
Create Date: 2026-11-05
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql as mysql_dialect

revision = "8a5e3d0c2b11"
down_revision = "f4b8d2e91c70"
branch_labels = None
depends_on = None

DATETIME3 = sa.DateTime().with_variant(mysql_dialect.DATETIME(fsp=3), "mysql")


def upgrade() -> None:
    """增加会签计数、截止时间与不可重复投票表。"""
    with op.batch_alter_table("run_approval") as batch:
        batch.add_column(
            sa.Column(
                "required_approvals",
                sa.Integer(),
                nullable=False,
                server_default="1",
            )
        )
        batch.add_column(
            sa.Column(
                "approved_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch.add_column(
            sa.Column(
                "rejected_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch.add_column(
            sa.Column(
                "timeout_action",
                sa.String(length=16),
                nullable=False,
                server_default="reject",
            )
        )
        batch.add_column(sa.Column("deadline_at", DATETIME3, nullable=True))
    op.create_table(
        "run_approval_vote",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer, "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("approval_id", sa.BigInteger(), nullable=False),
        sa.Column("decided_by", sa.String(length=64), nullable=False),
        sa.Column("approved", sa.SmallInteger(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            DATETIME3,
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "approval_id", "decided_by", name="uk_approval_voter"
        ),
    )
    op.create_index(
        "idx_approval_vote_approval",
        "run_approval_vote",
        ["approval_id", "created_at"],
    )


def downgrade() -> None:
    """回退会签与超时字段。"""
    op.drop_index(
        "idx_approval_vote_approval", table_name="run_approval_vote"
    )
    op.drop_table("run_approval_vote")
    with op.batch_alter_table("run_approval") as batch:
        batch.drop_column("deadline_at")
        batch.drop_column("timeout_action")
        batch.drop_column("rejected_count")
        batch.drop_column("approved_count")
        batch.drop_column("required_approvals")
