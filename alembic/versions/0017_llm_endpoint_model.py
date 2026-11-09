"""给 llm_endpoint 增加默认模型字段。

Revision ID: c3a95f0e7d18
Revises: 4e8c2a1d7f90
Create Date: 2026-11-09

管理端要在配置端点时就能选定模型：同一个 ``base_url`` 下的模型名各家不同
（``deepseek-chat`` / ``qwen-plus`` / …），把它跟着端点存下来，拉取模型列表
与「测试连接」才有默认值。存量行用空串补齐——空串表示「还没选」，不假装
某个模型名是通用的。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "c3a95f0e7d18"
down_revision = "4e8c2a1d7f90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增默认模型列，存量行补空串。"""
    with op.batch_alter_table("llm_endpoint") as batch:
        batch.add_column(
            sa.Column(
                "model",
                sa.String(length=128),
                nullable=False,
                server_default="",
            )
        )


def downgrade() -> None:
    """移除默认模型列。"""
    with op.batch_alter_table("llm_endpoint") as batch:
        batch.drop_column("model")
