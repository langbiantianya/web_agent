"""新增 skill / skill_resource / skill_revision 表（V4，§22.5）

Revision ID: b3e7d1a95c28
Revises: f1a2b3c4d5e6
Create Date: 2026-11-02

技能内容全部落在这三张表里：``skill`` 存技能定义（frontmatter 拆成列 + 正文），
``skill_resource`` 存资源文件（一行一个），``skill_revision`` 存变更审计快照
（技能进了 DB 就没有 git 了，改错了要能回滚）。

主键写法必须与 0001 一致：``BigInteger().with_variant(Integer, 'sqlite')``。
漏掉这个 SQLite 回落的后果很隐蔽——SQLite 里只有 ``INTEGER PRIMARY KEY``
才是 rowid 别名、才会自增；写成 ``BIGINT`` 的主键不会自增，插入时必须自己给
id，实际报错是 "NOT NULL constraint failed: skill.id"。而模型带回落、ORM 建表
又是对的，于是**只有走迁移建出来的库**会出问题——测试用 create_all 建库，
根本测不到。

时间列用 ``DATETIME3``（与 app.models.base 一致）：用裸 ``sa.DateTime()`` 的话
MySQL 上落成 ``DATETIME``（fsp=0），时间被截到整秒，且与 ORM 对不上——
autogenerate 会永远报一条多余的 ALTER 差异。

资源内容列用 ``LONGBLOB``：SQLAlchemy 的 ``LargeBinary`` 在 MySQL 上只映射到
``BLOB``（64 KB 上限），技能包里放一张图就可能超。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql as mysql_dialect

revision = "b3e7d1a95c28"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None

# 与 app.models.base.DATETIME3 保持一致：MySQL 用 fsp=3，其余方言用通用 DateTime
DATETIME3 = sa.DateTime().with_variant(mysql_dialect.DATETIME(fsp=3), "mysql")

# 与 app.models.skill.LONGBLOB 保持一致：MySQL 用 LONGBLOB，其余方言用 LargeBinary
LONGBLOB = sa.LargeBinary().with_variant(mysql_dialect.LONGBLOB(), "mysql")


def upgrade() -> None:
    """建 skill / skill_resource / skill_revision 三张表。"""
    op.create_table(
        "skill",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer, "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=1024), nullable=False),
        sa.Column("license", sa.String(length=128), nullable=False),
        sa.Column("compatibility", sa.String(length=500), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("allowed_tools", sa.String(length=512), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.SmallInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", DATETIME3, nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", DATETIME3, nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("code", name="uq_skill_code"),
    )
    op.create_index("idx_skill_enabled", "skill", ["enabled"])

    op.create_table(
        "skill_resource",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer, "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("skill_id", sa.BigInteger(), nullable=False),
        sa.Column("path", sa.String(length=255), nullable=False),
        sa.Column(
            "mime_type",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("is_text", sa.SmallInteger(), nullable=False),
        sa.Column("content", LONGBLOB, nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at", DATETIME3, nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("skill_id", "path", name="uk_skill_path"),
    )
    op.create_index("idx_skill_resource_skill", "skill_resource", ["skill_id"])

    op.create_table(
        "skill_revision",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer, "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("skill_id", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("resources", sa.JSON(), nullable=True),
        sa.Column("author", sa.String(length=64), nullable=False),
        sa.Column("note", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at", DATETIME3, nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("skill_id", "version", name="uk_skill_version"),
    )
    op.create_index("idx_skill_revision_skill", "skill_revision", ["skill_id"])


def downgrade() -> None:
    """删三张表。先删引用 skill 的两张，最后删 skill。"""
    op.drop_index("idx_skill_revision_skill", table_name="skill_revision")
    op.drop_table("skill_revision")
    op.drop_index("idx_skill_resource_skill", table_name="skill_resource")
    op.drop_table("skill_resource")
    op.drop_index("idx_skill_enabled", table_name="skill")
    op.drop_table("skill")
