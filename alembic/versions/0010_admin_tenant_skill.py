"""管理员用户、租户化 Skill、草稿状态与完整资源修订。

Revision ID: f4b8d2e91c70
Revises: c9d4e2a71f60
Create Date: 2026-11-05
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql as mysql_dialect

revision = "f4b8d2e91c70"
down_revision = "c9d4e2a71f60"
branch_labels = None
depends_on = None

DATETIME3 = sa.DateTime().with_variant(mysql_dialect.DATETIME(fsp=3), "mysql")
LONGBLOB = sa.LargeBinary().with_variant(mysql_dialect.LONGBLOB(), "mysql")


def upgrade() -> None:
    """增加管理员用户、Skill 租户/生命周期和二进制修订资源。"""
    op.create_table(
        "admin_user",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer, "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("api_key_hash", sa.String(length=128), nullable=False),
        sa.Column("api_key_hint", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.SmallInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("last_used_at", DATETIME3, nullable=True),
        sa.Column(
            "created_at", DATETIME3, nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", DATETIME3, nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("username", name="uq_admin_user_username"),
        sa.UniqueConstraint("api_key_hash", name="uq_admin_user_key_hash"),
    )
    op.create_index(
        "idx_admin_user_tenant", "admin_user", ["tenant_id", "role"]
    )

    with op.batch_alter_table("agent") as batch:
        batch.add_column(
            sa.Column(
                "tenant_id",
                sa.String(length=64),
                nullable=False,
                server_default="default",
            )
        )
        batch.add_column(sa.Column("tool_policy", sa.JSON(), nullable=True))
        batch.create_index("idx_agent_tenant", ["tenant_id"])

    with op.batch_alter_table("skill") as batch:
        batch.add_column(
            sa.Column(
                "tenant_id",
                sa.String(length=64),
                nullable=False,
                server_default="default",
            )
        )
        batch.add_column(
            sa.Column(
                "status",
                sa.String(length=16),
                nullable=False,
                server_default="published",
            )
        )
        batch.add_column(sa.Column("deleted_at", DATETIME3, nullable=True))
        batch.drop_constraint("uq_skill_code", type_="unique")
        batch.create_unique_constraint(
            "uq_skill_tenant_code", ["tenant_id", "code"]
        )
        batch.create_index(
            "idx_skill_tenant_status", ["tenant_id", "status"]
        )

    op.create_table(
        "skill_revision_resource",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer, "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("revision_id", sa.BigInteger(), nullable=False),
        sa.Column("path", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=64), nullable=False),
        sa.Column("is_text", sa.SmallInteger(), nullable=False),
        sa.Column("content", LONGBLOB, nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.UniqueConstraint("revision_id", "path", name="uk_revision_path"),
    )
    op.create_index(
        "idx_skill_revision_resource_revision",
        "skill_revision_resource",
        ["revision_id"],
    )


def downgrade() -> None:
    """回退本迁移。"""
    op.drop_index(
        "idx_skill_revision_resource_revision",
        table_name="skill_revision_resource",
    )
    op.drop_table("skill_revision_resource")
    with op.batch_alter_table("skill") as batch:
        batch.drop_index("idx_skill_tenant_status")
        batch.drop_constraint("uq_skill_tenant_code", type_="unique")
        batch.create_unique_constraint("uq_skill_code", ["code"])
        batch.drop_column("deleted_at")
        batch.drop_column("status")
        batch.drop_column("tenant_id")
    with op.batch_alter_table("agent") as batch:
        batch.drop_index("idx_agent_tenant")
        batch.drop_column("tool_policy")
        batch.drop_column("tenant_id")
    op.drop_index("idx_admin_user_tenant", table_name="admin_user")
    op.drop_table("admin_user")
