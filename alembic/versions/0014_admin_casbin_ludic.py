"""V4.7 独立 admin-web、超级管理员、Casbin 与 UI 策略。

Revision ID: 5d7f2a9c4e11
Revises: 24d9a6e8c1b3
Create Date: 2026-11-07
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql as mysql_dialect

revision = "5d7f2a9c4e11"
down_revision = "24d9a6e8c1b3"
branch_labels = None
depends_on = None

DATETIME3 = sa.DateTime().with_variant(mysql_dialect.DATETIME(fsp=3), "mysql")
PK = sa.BigInteger().with_variant(sa.Integer, "sqlite")


def upgrade() -> None:
    """创建独立后台服务和 Casbin/UI 策略表。"""
    with op.batch_alter_table("admin_user") as batch:
        batch.alter_column(
            "username", existing_type=sa.String(64), nullable=True
        )
        batch.alter_column(
            "display_name", existing_type=sa.String(128), nullable=True
        )
        batch.alter_column(
            "tenant_id", existing_type=sa.String(64), nullable=True
        )
        batch.alter_column("role", existing_type=sa.String(16), nullable=True)
        batch.alter_column(
            "api_key_hash", existing_type=sa.String(128), nullable=True
        )
        batch.alter_column(
            "api_key_hint", existing_type=sa.String(16), nullable=True
        )
        batch.add_column(sa.Column("user_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("provider_id", sa.BigInteger(), nullable=True))
        batch.add_column(
            sa.Column(
                "status",
                sa.String(16),
                nullable=False,
                server_default="active",
            )
        )
        batch.add_column(sa.Column("last_role_sync_at", DATETIME3, nullable=True))
        batch.create_foreign_key(
            "fk_admin_user_app_user",
            "app_user",
            ["user_id"],
            ["id"],
        )
        batch.create_foreign_key(
            "fk_admin_user_provider",
            "oauth_provider",
            ["provider_id"],
            ["id"],
        )
        batch.create_unique_constraint(
            "uq_admin_user_user_id", ["user_id"]
        )
        batch.create_index(
            "idx_admin_user_oauth_status", ["status", "provider_id"]
        )

    with op.batch_alter_table("oauth_provider") as batch:
        batch.add_column(
            sa.Column(
                "role_claim",
                sa.String(64),
                nullable=False,
                server_default="groups",
            )
        )
        batch.add_column(
            sa.Column(
                "role_claim_required",
                sa.SmallInteger(),
                nullable=False,
                server_default="1",
            )
        )
        batch.add_column(
            sa.Column(
                "role_sync_max_age_seconds",
                sa.Integer(),
                nullable=False,
                server_default="1800",
            )
        )

    op.create_table(
        "admin_session",
        sa.Column("id", PK, primary_key=True, autoincrement=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("csrf_hash", sa.String(64), nullable=False),
        sa.Column("admin_user_id", sa.BigInteger(), nullable=False),
        sa.Column("auth_at", DATETIME3, nullable=False),
        sa.Column("last_seen_at", DATETIME3, nullable=False),
        sa.Column("idle_expires_at", DATETIME3, nullable=False),
        sa.Column("absolute_expires_at", DATETIME3, nullable=False),
        sa.Column("revoked_at", DATETIME3, nullable=True),
        sa.Column(
            "role_sync_version", sa.Integer(), nullable=False, server_default="1"
        ),
        sa.Column(
            "created_at", DATETIME3, nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["admin_user_id"],
            ["admin_user.id"],
            name="fk_admin_session_admin_user",
        ),
    )
    op.create_index(
        "idx_admin_session_user_active",
        "admin_session",
        ["admin_user_id", "revoked_at"],
    )
    op.create_index(
        "idx_admin_session_expiry",
        "admin_session",
        ["idle_expires_at", "absolute_expires_at"],
    )

    op.create_table(
        "super_admin_account",
        sa.Column("id", PK, primary_key=True, autoincrement=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.String(512), nullable=False),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("phone", sa.String(32), nullable=True),
        sa.Column(
            "status",
            sa.String(24),
            nullable=False,
            server_default="pending_activation",
        ),
        sa.Column("owner_slot", sa.String(32), nullable=True),
        sa.Column("password_rotated_at", DATETIME3, nullable=True),
        sa.Column("password_expires_at", DATETIME3, nullable=True),
        sa.Column(
            "password_version", sa.Integer(), nullable=False, server_default="1"
        ),
        sa.Column(
            "failed_login_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("locked_until", DATETIME3, nullable=True),
        sa.Column("created_by_id", sa.BigInteger(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", DATETIME3, nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", DATETIME3, nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("username", name="uq_super_admin_username"),
        sa.UniqueConstraint("email", name="uq_super_admin_email"),
        sa.UniqueConstraint("phone", name="uq_super_admin_phone"),
        sa.UniqueConstraint("owner_slot", name="uq_super_admin_owner_slot"),
    )
    op.create_index(
        "idx_super_admin_status", "super_admin_account", ["status"]
    )
    op.create_index(
        "idx_super_admin_expiry",
        "super_admin_account",
        ["password_expires_at"],
    )

    op.create_table(
        "super_admin_session",
        sa.Column("id", PK, primary_key=True, autoincrement=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("csrf_hash", sa.String(64), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("auth_at", DATETIME3, nullable=False),
        sa.Column("idle_expires_at", DATETIME3, nullable=False),
        sa.Column("absolute_expires_at", DATETIME3, nullable=False),
        sa.Column("revoked_at", DATETIME3, nullable=True),
        sa.Column("password_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", DATETIME3, nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["super_admin_account.id"],
            name="fk_super_admin_session_account",
        ),
    )
    op.create_index(
        "idx_super_admin_session_account",
        "super_admin_session",
        ["account_id", "revoked_at"],
    )
    op.create_index(
        "idx_super_admin_session_expiry",
        "super_admin_session",
        ["idle_expires_at", "absolute_expires_at"],
    )

    op.create_table(
        "super_admin_password_reset",
        sa.Column("id", PK, primary_key=True, autoincrement=True),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("pending_password_hash", sa.String(512), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column(
            "status", sa.String(24), nullable=False, server_default="pending"
        ),
        sa.Column(
            "email_status",
            sa.String(16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "sms_status",
            sa.String(16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("requested_at", DATETIME3, nullable=False),
        sa.Column("expires_at", DATETIME3, nullable=False),
        sa.Column("activated_at", DATETIME3, nullable=True),
        sa.Column("requested_ip_hash", sa.String(64), nullable=True),
        sa.Column("failure_code", sa.String(64), nullable=True),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["super_admin_account.id"],
            name="fk_super_admin_reset_account",
        ),
    )
    op.create_index(
        "idx_super_admin_reset_account_status",
        "super_admin_password_reset",
        ["account_id", "status"],
    )

    op.create_table(
        "casbin_rule",
        sa.Column("id", PK, primary_key=True, autoincrement=True),
        sa.Column("ptype", sa.String(16), nullable=False),
        sa.Column("v0", sa.String(255), nullable=True),
        sa.Column("v1", sa.String(255), nullable=True),
        sa.Column("v2", sa.String(255), nullable=True),
        sa.Column("v3", sa.String(255), nullable=True),
        sa.Column("v4", sa.String(255), nullable=True),
        sa.Column("v5", sa.String(255), nullable=True),
    )
    op.create_index("idx_casbin_rule_ptype", "casbin_rule", ["ptype"])
    op.create_index("idx_casbin_rule_v0", "casbin_rule", ["v0"])
    op.create_index("idx_casbin_rule_v1", "casbin_rule", ["v1"])

    op.create_table(
        "authz_policy_version",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "updated_at", DATETIME3, nullable=False, server_default=sa.func.now()
        ),
    )
    op.execute(
        sa.text(
            "INSERT INTO authz_policy_version (id, version) VALUES (1, 1)"
        )
    )

    op.create_table(
        "oauth_role_mapping",
        sa.Column("id", PK, primary_key=True, autoincrement=True),
        sa.Column("provider_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "claim_name",
            sa.String(64),
            nullable=False,
            server_default="groups",
        ),
        sa.Column("external_value", sa.String(255), nullable=False),
        sa.Column("role_code", sa.String(64), nullable=False),
        sa.Column(
            "enabled", sa.SmallInteger(), nullable=False, server_default="1"
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", DATETIME3, nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", DATETIME3, nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["oauth_provider.id"],
            name="fk_oauth_role_mapping_provider",
        ),
        sa.UniqueConstraint(
            "provider_id",
            "claim_name",
            "external_value",
            name="uq_oauth_role_mapping_value",
        ),
    )

    op.create_table(
        "admin_page_definition",
        sa.Column("code", sa.String(64), primary_key=True),
        sa.Column("route_key", sa.String(128), nullable=False, unique=True),
        sa.Column("title", sa.String(128), nullable=False),
        sa.Column(
            "nav_group",
            sa.String(64),
            nullable=False,
            server_default="General",
        ),
        sa.Column(
            "access_mode",
            sa.String(8),
            nullable=False,
            server_default="all",
        ),
        sa.Column(
            "enabled", sa.SmallInteger(), nullable=False, server_default="1"
        ),
        sa.Column("nav_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "permissions_initialized",
            sa.SmallInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )

    op.create_table(
        "admin_component_definition",
        sa.Column("page_code", sa.String(64), primary_key=True),
        sa.Column("code", sa.String(96), primary_key=True),
        sa.Column("render_key", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("label", sa.String(128), nullable=False),
        sa.Column(
            "access_mode",
            sa.String(8),
            nullable=False,
            server_default="all",
        ),
        sa.Column(
            "enabled", sa.SmallInteger(), nullable=False, server_default="1"
        ),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "permissions_initialized",
            sa.SmallInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(
            ["page_code"],
            ["admin_page_definition.code"],
            name="fk_admin_component_page",
        ),
    )

    op.create_table(
        "admin_page_permission",
        sa.Column("id", PK, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(64), nullable=True),
        sa.Column(
            "binding_scope",
            sa.String(80),
            nullable=False,
            server_default="global",
        ),
        sa.Column("page_code", sa.String(64), nullable=False),
        sa.Column("object_name", sa.String(64), nullable=False),
        sa.Column("action_name", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["page_code"],
            ["admin_page_definition.code"],
            name="fk_admin_page_permission_page",
        ),
        sa.UniqueConstraint(
            "binding_scope",
            "page_code",
            "object_name",
            "action_name",
            name="uq_admin_page_permission",
        ),
    )
    op.create_index(
        "idx_admin_page_permission_tenant",
        "admin_page_permission",
        ["tenant_id", "page_code"],
    )

    op.create_table(
        "admin_component_permission",
        sa.Column("id", PK, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(64), nullable=True),
        sa.Column(
            "binding_scope",
            sa.String(80),
            nullable=False,
            server_default="global",
        ),
        sa.Column("page_code", sa.String(64), nullable=False),
        sa.Column("component_code", sa.String(96), nullable=False),
        sa.Column("object_name", sa.String(64), nullable=False),
        sa.Column("action_name", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["page_code", "component_code"],
            [
                "admin_component_definition.page_code",
                "admin_component_definition.code",
            ],
            name="fk_admin_component_permission_component",
        ),
        sa.UniqueConstraint(
            "binding_scope",
            "page_code",
            "component_code",
            "object_name",
            "action_name",
            name="uq_admin_component_permission",
        ),
    )
    op.create_index(
        "idx_admin_component_permission_tenant",
        "admin_component_permission",
        ["tenant_id", "page_code", "component_code"],
    )

    op.create_table(
        "admin_ui_preference",
        sa.Column("principal_kind", sa.String(32), primary_key=True),
        sa.Column("principal_id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "theme", sa.String(16), nullable=False, server_default="fluent"
        ),
        sa.Column(
            "color_mode",
            sa.String(16),
            nullable=False,
            server_default="system",
        ),
        sa.Column(
            "density",
            sa.String(16),
            nullable=False,
            server_default="comfortable",
        ),
        sa.Column(
            "sidebar_collapsed",
            sa.SmallInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "updated_at", DATETIME3, nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "admin_audit_event",
        sa.Column("id", PK, primary_key=True, autoincrement=True),
        sa.Column(
            "occurred_at",
            DATETIME3,
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("actor_kind", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column("actor_username", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=True),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("target_type", sa.String(64), nullable=False),
        sa.Column("target_id", sa.String(128), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("trace_id", sa.String(64), nullable=True),
    )
    op.create_index(
        "idx_admin_audit_occurred",
        "admin_audit_event",
        ["occurred_at"],
    )
    op.create_index(
        "idx_admin_audit_actor",
        "admin_audit_event",
        ["actor_kind", "actor_id", "occurred_at"],
    )
    op.create_index(
        "idx_admin_audit_tenant",
        "admin_audit_event",
        ["tenant_id", "occurred_at"],
    )


def downgrade() -> None:
    """回退 V4.7 后台表和管理员字段。"""
    op.drop_index("idx_admin_audit_tenant", table_name="admin_audit_event")
    op.drop_index("idx_admin_audit_actor", table_name="admin_audit_event")
    op.drop_index("idx_admin_audit_occurred", table_name="admin_audit_event")
    op.drop_table("admin_audit_event")
    op.drop_table("admin_ui_preference")
    op.drop_index(
        "idx_admin_component_permission_tenant",
        table_name="admin_component_permission",
    )
    op.drop_table("admin_component_permission")
    op.drop_index(
        "idx_admin_page_permission_tenant",
        table_name="admin_page_permission",
    )
    op.drop_table("admin_page_permission")
    op.drop_table("admin_component_definition")
    op.drop_table("admin_page_definition")
    op.drop_table("oauth_role_mapping")
    op.drop_table("authz_policy_version")
    op.drop_index("idx_casbin_rule_v1", table_name="casbin_rule")
    op.drop_index("idx_casbin_rule_v0", table_name="casbin_rule")
    op.drop_index("idx_casbin_rule_ptype", table_name="casbin_rule")
    op.drop_table("casbin_rule")
    op.drop_index(
        "idx_super_admin_reset_account_status",
        table_name="super_admin_password_reset",
    )
    op.drop_table("super_admin_password_reset")
    op.drop_index(
        "idx_super_admin_session_expiry",
        table_name="super_admin_session",
    )
    op.drop_index(
        "idx_super_admin_session_account",
        table_name="super_admin_session",
    )
    op.drop_table("super_admin_session")
    op.drop_index("idx_super_admin_expiry", table_name="super_admin_account")
    op.drop_index("idx_super_admin_status", table_name="super_admin_account")
    op.drop_table("super_admin_account")
    op.drop_index("idx_admin_session_expiry", table_name="admin_session")
    op.drop_index("idx_admin_session_user_active", table_name="admin_session")
    op.drop_table("admin_session")

    with op.batch_alter_table("oauth_provider") as batch:
        batch.drop_column("role_sync_max_age_seconds")
        batch.drop_column("role_claim_required")
        batch.drop_column("role_claim")

    with op.batch_alter_table("admin_user") as batch:
        batch.drop_index("idx_admin_user_oauth_status")
        batch.drop_constraint("uq_admin_user_user_id", type_="unique")
        batch.drop_constraint("fk_admin_user_provider", type_="foreignkey")
        batch.drop_constraint("fk_admin_user_app_user", type_="foreignkey")
        batch.drop_column("last_role_sync_at")
        batch.drop_column("status")
        batch.drop_column("provider_id")
        batch.drop_column("user_id")
        batch.alter_column(
            "api_key_hint",
            existing_type=sa.String(16),
            nullable=False,
        )
        batch.alter_column(
            "api_key_hash",
            existing_type=sa.String(128),
            nullable=False,
        )
        batch.alter_column(
            "role", existing_type=sa.String(16), nullable=False
        )
        batch.alter_column(
            "tenant_id", existing_type=sa.String(64), nullable=False
        )
        batch.alter_column(
            "display_name", existing_type=sa.String(128), nullable=False
        )
        batch.alter_column(
            "username", existing_type=sa.String(64), nullable=False
        )
