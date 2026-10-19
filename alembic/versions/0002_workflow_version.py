"""workflow 表加 version 列

Revision ID: 7c1f0a94b2e6
Revises: cf7910dfd924
Create Date: 2026-10-19

给 workflow 加 ``version``，让缓存能像 agent 那样靠版本号自然失效，不再依赖
调用方记得 ``put()`` / ``clear()``。

回填策略用 ``server_default`` 而不是 UPDATE：加列当步就能把所有既有行填成 1，
省掉一次全表 UPDATE。SQLite 的 ADD COLUMN 不支持非常量 DEFAULT，
``sa.func.now()`` 这类表达式在这里用不了，所以走普通常量 + 显式回填两步。

降级只回填值、不删列：删列在 SQLite 上要重建表，会把 definition 这个 JSON
列也一起搬一遍，风险收益不划算；而留着 version 列对旧代码无害（ORM 有默认值）。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "7c1f0a94b2e6"
down_revision = "cf7910dfd924"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """加 version 列并把所有既有行回填成 1。"""
    with op.batch_alter_table("workflow") as batch:
        batch.add_column(
            sa.Column("version", sa.Integer(), nullable=False, server_default="1")
        )
    # server_default 已经把新行填成 1；显式 UPDATE 一次是为了让
    # 依赖 server_default 的方言（MySQL 允许省略）也拿到确定的值，
    # 同时把 DEFAULT 从「永远存在」降级为「仅回填用」。
    op.execute(sa.text("UPDATE workflow SET version = 1 WHERE version IS NULL"))
    with op.batch_alter_table("workflow") as batch:
        batch.alter_column("version", server_default=None)


def downgrade() -> None:
    """只回填值，不删列——理由见模块 docstring。

    保留这一版是为了让「回滚后代码仍能跑」：即使有人手工回滚到这里，
    workflow 表也是自洽的。
    """
    return None
