"""新增 run_approval 表

Revision ID: c4d8e1f0a927
Revises: 7c1f0a94b2e6
Create Date: 2026-10-19

人工审批（V3.1）的审计记录：run 走到审批节点时开一条 pending 记录，人决策后
变成 approved / rejected，记下是谁、什么时候、说了什么。

这张表**不是**暂停点本身——暂停点（图状态）由 LangGraph checkpointer 存。两者
分工明确，所以这张表可以随时重建而不影响「能不能恢复执行」。

降级直接删表：它只装审计数据，重建后历史审批记录丢失，但不影响任何在跑的 run
（暂停点在 checkpointer 里，不在这张表）。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql as mysql_dialect

revision = "c4d8e1f0a927"
down_revision = "7c1f0a94b2e6"
branch_labels = None
depends_on = None

# 与 app.models.base.DATETIME3 保持一致：MySQL 用 fsp=3，其余方言用通用 DateTime。
# 用裸 sa.DateTime() 的话 MySQL 上落成 DATETIME（fsp=0），审批时间被截到整秒，
# 且与 ORM 的 DATETIME(3) 对不上——autogenerate 会永远报一条多余的 ALTER 差异。
DATETIME3 = sa.DateTime().with_variant(mysql_dialect.DATETIME(fsp=3), "mysql")


def upgrade() -> None:
    """建 run_approval 表。

    主键的写法必须与 0001 一致：``BigInteger().with_variant(Integer, 'sqlite')``。

    漏掉这个 SQLite 回落的后果很隐蔽——SQLite 里只有 ``INTEGER PRIMARY KEY``
    才是 rowid 别名、才会自增；写成 ``BIGINT`` 的主键不会自增，插入时必须自己
    给 id，实际报错是 "NOT NULL constraint failed: run_approval.id"。
    而模型（:data:`app.models.base.PK`）带回落、ORM 建表又是对的，于是**只有
    走迁移建出来的库**会出问题——测试用 create_all 建库，根本测不到。
    """
    op.create_table(
        "run_approval",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer, "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("prompt", sa.Text(), nullable=False, server_default=""),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("decided_by", sa.String(length=64), nullable=True),
        sa.Column(
            "requested_at",
            DATETIME3,
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("decided_at", DATETIME3, nullable=True),
        sa.UniqueConstraint("run_id", "node_id", name="uk_run_approval_node"),
    )
    op.create_index("idx_approval_status", "run_approval", ["status", "requested_at"])


def downgrade() -> None:
    """删表。"""
    op.drop_index("idx_approval_status", table_name="run_approval")
    op.drop_table("run_approval")
