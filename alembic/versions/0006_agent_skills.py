"""agent 表加 skills JSON 列（V4，§22.5）

Revision ID: d5a91c3b7e42
Revises: b3e7d1a95c28
Create Date: 2026-11-02

Agent 绑定技能的 code 列表。**不是**工具白名单——技能只贡献指令正文，模型能
不能调工具仍然只看 ``tools`` 列（§22.9：allowed-tools 只是建议，授权只看
``agent.tools``）。

用 ``batch_alter_table`` 而不是裸 ``add_column``：SQLite 不支持直接 ALTER
ADD COLUMN 带约束的列，batch 模式会在 SQLite 上重建表、在其它方言上走原生
ALTER。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "d5a91c3b7e42"
down_revision = "b3e7d1a95c28"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """给 agent 加 skills 列。"""
    with op.batch_alter_table("agent") as batch:
        batch.add_column(sa.Column("skills", sa.JSON(), nullable=True))


def downgrade() -> None:
    """删 skills 列。绑定了技能的 Agent 会退回「没有技能」的状态。"""
    with op.batch_alter_table("agent") as batch:
        batch.drop_column("skills")
