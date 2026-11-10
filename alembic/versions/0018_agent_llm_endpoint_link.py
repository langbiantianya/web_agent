"""将 Agent 关联到统一的 LLM 连接配置。

Revision ID: d7e2b4c901af
Revises: c3a95f0e7d18
Create Date: 2026-11-10

管理端不再让每个 Agent 重复填写 Base URL 与 API Key。Agent 只保存稳定连接
编码，运行期从 ``llm_endpoint`` 取地址和密文；模型仍可按 Agent 覆盖。
存量 Agent 保持 ``NULL``，继续走原有兼容路径。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "d7e2b4c901af"
down_revision = "c3a95f0e7d18"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增可空的连接编码列与查询索引。"""
    with op.batch_alter_table("agent") as batch:
        batch.add_column(
            sa.Column("llm_endpoint_code", sa.String(length=64), nullable=True)
        )
        batch.create_index(
            "idx_agent_llm_endpoint",
            ["llm_endpoint_code"],
            unique=False,
        )


def downgrade() -> None:
    """移除 Agent→LLM 连接关联。"""
    with op.batch_alter_table("agent") as batch:
        batch.drop_index("idx_agent_llm_endpoint")
        batch.drop_column("llm_endpoint_code")
