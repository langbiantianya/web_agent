"""新增 mcp_server / mcp_tool 表（V4，§21.6）

Revision ID: e8b4c2f61d93
Revises: d5a91c3b7e42
Create Date: 2026-11-02

MCP 服务器注册表与工具目录快照。

``mcp_server.env`` 与 ``mcp_server.headers`` 里装的是凭据——**值加密**存储，
只写不读。加密在应用层做（:mod:`app.core.crypto` 的 Fernet），所以这两列是
JSON 而不是某个数据库的原生密文类型。

主键写法必须与 0001 一致：``BigInteger().with_variant(Integer, 'sqlite')``。
漏掉这个 SQLite 回落的后果很隐蔽——SQLite 里只有 ``INTEGER PRIMARY KEY``
才是 rowid 别名、才会自增；写成 ``BIGINT`` 的主键不会自增，插入时必须自己给
id，实际报错是 "NOT NULL constraint failed: mcp_server.id"。而模型带回落、
ORM 建表又是对的，于是**只有走迁移建出来的库**会出问题。

时间列用 ``DATETIME3``：用裸 ``sa.DateTime()`` 的话 MySQL 上落成
``DATETIME``（fsp=0），时间被截到整秒，且与 ORM 对不上——autogenerate 会永远
报一条多余的 ALTER 差异。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql as mysql_dialect

revision = "e8b4c2f61d93"
down_revision = "d5a91c3b7e42"
branch_labels = None
depends_on = None

# 与 app.models.base.DATETIME3 保持一致：MySQL 用 fsp=3，其余方言用通用 DateTime
DATETIME3 = sa.DateTime().with_variant(mysql_dialect.DATETIME(fsp=3), "mysql")


def upgrade() -> None:
    """建 mcp_server / mcp_tool 两张表。"""
    op.create_table(
        "mcp_server",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer, "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("transport", sa.String(length=16), nullable=False),
        sa.Column("command", sa.String(length=512), nullable=True),
        sa.Column("args", sa.JSON(), nullable=True),
        sa.Column("env", sa.JSON(), nullable=True),
        sa.Column("url", sa.String(length=512), nullable=True),
        sa.Column("headers", sa.JSON(), nullable=True),
        sa.Column("allow_tools", sa.JSON(), nullable=True),
        sa.Column("timeout_sec", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.SmallInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", DATETIME3, nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", DATETIME3, nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("code", name="uq_mcp_server_code"),
    )
    op.create_index("idx_mcp_server_enabled", "mcp_server", ["enabled"])

    op.create_table(
        "mcp_tool",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer, "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("server_id", sa.BigInteger(), nullable=False),
        sa.Column("qualified_name", sa.String(length=128), nullable=False),
        sa.Column("tool_name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("input_schema", sa.JSON(), nullable=True),
        sa.Column("output_schema", sa.JSON(), nullable=True),
        sa.Column("annotations", sa.JSON(), nullable=True),
        sa.Column(
            "discovered_at", DATETIME3, nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("qualified_name", name="uk_mcp_tool_qualified"),
    )
    op.create_index("idx_mcp_tool_server", "mcp_tool", ["server_id"])


def downgrade() -> None:
    """删两张表。先删引用 server 的 mcp_tool，最后删 mcp_server。"""
    op.drop_index("idx_mcp_tool_server", table_name="mcp_tool")
    op.drop_table("mcp_tool")
    op.drop_index("idx_mcp_server_enabled", table_name="mcp_server")
    op.drop_table("mcp_server")
