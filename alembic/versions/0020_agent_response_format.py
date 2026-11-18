"""agent 表加 JSON 响应配置列（V4.9）。

Revision ID: c7d2e5a8b134
Revises: f4b7c1a9e2d5
Create Date: 2026-11-19

让 Agent 可以声明「我的最终回答必须是 JSON」。两列：

- ``response_format``：``text``（默认）或 ``json``；
- ``response_schema``：JSON Schema 对象，限定 JSON 的结构；为空表示只要求
  是 JSON、不限定结构。

存量 Agent 的语义是「照常输出文本」，所以 ``response_format`` 的缺省值取
``text``，升级后不需要改任何既有配置。

用 ``batch_alter_table`` 而不是裸 ``add_column``：SQLite 不支持直接 ALTER
ADD COLUMN 带约束的列，batch 模式会在 SQLite 上重建表、在其它方言上走原生
ALTER。列类型与 :mod:`app.models.base` 的跨库约定一致（JSON 列用 ``sa.JSON``，
在 MySQL / PostgreSQL 上是原生 JSON，在 SQLite 上落成 TEXT）。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "c7d2e5a8b134"
down_revision = "f4b7c1a9e2d5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """给 agent 加输出格式与结构要求两列。"""
    with op.batch_alter_table("agent") as batch:
        batch.add_column(
            sa.Column(
                "response_format",
                sa.String(length=16),
                nullable=False,
                server_default="text",
            )
        )
        batch.add_column(
            sa.Column("response_schema", sa.JSON(), nullable=True)
        )


def downgrade() -> None:
    """移除输出格式与结构要求两列。

    降级后这些 Agent 回到「输出文本」，之前按 JSON 消费下游数据的调用方
    需要同步改回按文本解析。
    """
    with op.batch_alter_table("agent") as batch:
        batch.drop_column("response_schema")
        batch.drop_column("response_format")
