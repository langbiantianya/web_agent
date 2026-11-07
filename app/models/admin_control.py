"""V4.7 独立后台管理员、超级管理员、Casbin 与 UI 策略模型。"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import DATETIME3, JSON_COL, PK, Base, utcnow


def permission_binding_scope(tenant_id: str | None) -> str:
    """把可空租户转换成跨数据库稳定、非空的唯一约束键。"""
    return "global" if tenant_id is None else f"tenant:{tenant_id}"


class AdminSession(Base):
    """OAuth 后台管理员使用的独立服务端会话。"""

    __tablename__ = "admin_session"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    csrf_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    admin_user_id: Mapped[int] = mapped_column(
        sa.BigInteger,
        sa.ForeignKey("admin_user.id", name="fk_admin_session_admin_user"),
        nullable=False,
    )
    auth_at: Mapped[object] = mapped_column(DATETIME3, nullable=False)
    last_seen_at: Mapped[object] = mapped_column(DATETIME3, nullable=False)
    idle_expires_at: Mapped[object] = mapped_column(DATETIME3, nullable=False)
    absolute_expires_at: Mapped[object] = mapped_column(DATETIME3, nullable=False)
    revoked_at: Mapped[object | None] = mapped_column(DATETIME3, nullable=True)
    role_sync_version: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=1
    )
    created_at: Mapped[object] = mapped_column(
        DATETIME3, server_default=sa.func.now(), nullable=False
    )

    __table_args__ = (
        sa.Index("idx_admin_session_user_active", "admin_user_id", "revoked_at"),
        sa.Index(
            "idx_admin_session_expiry",
            "idle_expires_at",
            "absolute_expires_at",
        ),
    )


class SuperAdminAccount(Base):
    """独立账号密码平台超级管理员；密码只保存 Argon2id 摘要。"""

    __tablename__ = "super_admin_account"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    password_hash: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    email: Mapped[str | None] = mapped_column(sa.String(320), nullable=True)
    phone: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    status: Mapped[str] = mapped_column(
        sa.String(24), nullable=False, default="pending_activation"
    )
    owner_slot: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    password_rotated_at: Mapped[object | None] = mapped_column(
        DATETIME3, nullable=True
    )
    password_expires_at: Mapped[object | None] = mapped_column(
        DATETIME3, nullable=True
    )
    password_version: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=1
    )
    failed_login_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0
    )
    locked_until: Mapped[object | None] = mapped_column(DATETIME3, nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    created_at: Mapped[object] = mapped_column(
        DATETIME3, server_default=sa.func.now(), nullable=False
    )
    updated_at: Mapped[object] = mapped_column(
        DATETIME3,
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
        nullable=False,
    )

    __table_args__ = (
        sa.UniqueConstraint("username", name="uq_super_admin_username"),
        sa.UniqueConstraint("email", name="uq_super_admin_email"),
        sa.UniqueConstraint("phone", name="uq_super_admin_phone"),
        sa.UniqueConstraint("owner_slot", name="uq_super_admin_owner_slot"),
        sa.Index("idx_super_admin_status", "status"),
        sa.Index("idx_super_admin_expiry", "password_expires_at"),
    )


class SuperAdminSession(Base):
    """平台超级管理员独立服务端会话。"""

    __tablename__ = "super_admin_session"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    csrf_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    account_id: Mapped[int] = mapped_column(
        sa.BigInteger,
        sa.ForeignKey(
            "super_admin_account.id",
            name="fk_super_admin_session_account",
        ),
        nullable=False,
    )
    auth_at: Mapped[object] = mapped_column(DATETIME3, nullable=False)
    idle_expires_at: Mapped[object] = mapped_column(DATETIME3, nullable=False)
    absolute_expires_at: Mapped[object] = mapped_column(DATETIME3, nullable=False)
    revoked_at: Mapped[object | None] = mapped_column(DATETIME3, nullable=True)
    password_version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    created_at: Mapped[object] = mapped_column(
        DATETIME3, server_default=sa.func.now(), nullable=False
    )

    __table_args__ = (
        sa.Index("idx_super_admin_session_account", "account_id", "revoked_at"),
        sa.Index(
            "idx_super_admin_session_expiry",
            "idle_expires_at",
            "absolute_expires_at",
        ),
    )


class SuperAdminPasswordReset(Base):
    """一次性密码生成、通知与激活记录；不保存明文密码。"""

    __tablename__ = "super_admin_password_reset"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        sa.BigInteger,
        sa.ForeignKey(
            "super_admin_account.id",
            name="fk_super_admin_reset_account",
        ),
        nullable=False,
    )
    pending_password_hash: Mapped[str] = mapped_column(
        sa.String(512), nullable=False
    )
    reason: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        sa.String(24), nullable=False, default="pending"
    )
    email_status: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default="pending"
    )
    sms_status: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default="pending"
    )
    requested_at: Mapped[object] = mapped_column(DATETIME3, nullable=False)
    expires_at: Mapped[object] = mapped_column(DATETIME3, nullable=False)
    activated_at: Mapped[object | None] = mapped_column(DATETIME3, nullable=True)
    requested_ip_hash: Mapped[str | None] = mapped_column(
        sa.String(64), nullable=True
    )
    failure_code: Mapped[str | None] = mapped_column(
        sa.String(64), nullable=True
    )

    __table_args__ = (
        sa.Index(
            "idx_super_admin_reset_account_status",
            "account_id",
            "status",
        ),
    )


class CasbinRule(Base):
    """Casbin p/g 规则。"""

    __tablename__ = "casbin_rule"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    ptype: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    v0: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    v1: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    v2: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    v3: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    v4: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    v5: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)

    __table_args__ = (
        sa.Index("idx_casbin_rule_ptype", "ptype"),
        sa.Index("idx_casbin_rule_v0", "v0"),
        sa.Index("idx_casbin_rule_v1", "v1"),
    )


class AuthzPolicyVersion(Base):
    """进程间 Casbin 策略刷新版本。"""

    __tablename__ = "authz_policy_version"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    updated_at: Mapped[object] = mapped_column(
        DATETIME3,
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
        nullable=False,
    )


class OAuthRoleMapping(Base):
    """OAuth 外部组/角色到 Casbin role 的显式映射。"""

    __tablename__ = "oauth_role_mapping"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    provider_id: Mapped[int] = mapped_column(
        sa.BigInteger,
        sa.ForeignKey("oauth_provider.id", name="fk_oauth_role_mapping_provider"),
        nullable=False,
    )
    claim_name: Mapped[str] = mapped_column(
        sa.String(64), nullable=False, default="groups"
    )
    external_value: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    role_code: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    enabled: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, default=1)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    created_at: Mapped[object] = mapped_column(
        DATETIME3, server_default=sa.func.now(), nullable=False
    )
    updated_at: Mapped[object] = mapped_column(
        DATETIME3,
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
        nullable=False,
    )

    __table_args__ = (
        sa.UniqueConstraint(
            "provider_id",
            "claim_name",
            "external_value",
            name="uq_oauth_role_mapping_value",
        ),
    )


class AdminPageDefinition(Base):
    """Ludic 后台页面注册表。"""

    __tablename__ = "admin_page_definition"

    code: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    route_key: Mapped[str] = mapped_column(sa.String(128), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    nav_group: Mapped[str] = mapped_column(
        sa.String(64), nullable=False, default="General"
    )
    access_mode: Mapped[str] = mapped_column(
        sa.String(8), nullable=False, default="all"
    )
    enabled: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, default=1)
    nav_order: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    permissions_initialized: Mapped[int] = mapped_column(
        sa.SmallInteger, nullable=False, default=0
    )
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)


class AdminComponentDefinition(Base):
    """页面内 Ludic 组件注册表。"""

    __tablename__ = "admin_component_definition"

    page_code: Mapped[str] = mapped_column(
        sa.String(64),
        sa.ForeignKey(
            "admin_page_definition.code",
            name="fk_admin_component_page",
        ),
        primary_key=True,
    )
    code: Mapped[str] = mapped_column(sa.String(96), primary_key=True)
    render_key: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    kind: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    label: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    access_mode: Mapped[str] = mapped_column(
        sa.String(8), nullable=False, default="all"
    )
    enabled: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False, default=1)
    sort_order: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    permissions_initialized: Mapped[int] = mapped_column(
        sa.SmallInteger, nullable=False, default=0
    )
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)


class AdminPagePermission(Base):
    """页面访问权限绑定；``global`` 默认值可被租户覆盖。"""

    __tablename__ = "admin_page_permission"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    binding_scope: Mapped[str] = mapped_column(
        sa.String(80), nullable=False, default="global"
    )
    page_code: Mapped[str] = mapped_column(
        sa.String(64),
        sa.ForeignKey(
            "admin_page_definition.code",
            name="fk_admin_page_permission_page",
        ),
        nullable=False,
    )
    object_name: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    action_name: Mapped[str] = mapped_column(sa.String(64), nullable=False)

    __table_args__ = (
        sa.UniqueConstraint(
            "binding_scope",
            "page_code",
            "object_name",
            "action_name",
            name="uq_admin_page_permission",
        ),
        sa.Index(
            "idx_admin_page_permission_tenant",
            "tenant_id",
            "page_code",
        ),
    )


class AdminComponentPermission(Base):
    """组件显示权限绑定；``global`` 默认值可被租户覆盖。"""

    __tablename__ = "admin_component_permission"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    binding_scope: Mapped[str] = mapped_column(
        sa.String(80), nullable=False, default="global"
    )
    page_code: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    component_code: Mapped[str] = mapped_column(sa.String(96), nullable=False)
    object_name: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    action_name: Mapped[str] = mapped_column(sa.String(64), nullable=False)

    __table_args__ = (
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
        sa.Index(
            "idx_admin_component_permission_tenant",
            "tenant_id",
            "page_code",
            "component_code",
        ),
    )


class AdminUiPreference(Base):
    """管理员主题、色温和密度偏好。"""

    __tablename__ = "admin_ui_preference"

    principal_kind: Mapped[str] = mapped_column(
        sa.String(32), primary_key=True
    )
    principal_id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    theme: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default="fluent"
    )
    color_mode: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default="system"
    )
    density: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default="comfortable"
    )
    sidebar_collapsed: Mapped[int] = mapped_column(
        sa.SmallInteger, nullable=False, default=0
    )
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    updated_at: Mapped[object] = mapped_column(
        DATETIME3,
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
        nullable=False,
    )


class AdminAuditEvent(Base):
    """后台敏感操作审计；禁止保存密码、Session 或完整联系方式。"""

    __tablename__ = "admin_audit_event"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    occurred_at: Mapped[object] = mapped_column(
        DATETIME3, nullable=False, default=utcnow
    )
    actor_kind: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    actor_id: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    actor_username: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    tenant_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    action: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    target_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    outcome: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSON_COL, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)

    __table_args__ = (
        sa.Index("idx_admin_audit_occurred", "occurred_at"),
        sa.Index(
            "idx_admin_audit_actor",
            "actor_kind",
            "actor_id",
            "occurred_at",
        ),
        sa.Index(
            "idx_admin_audit_tenant",
            "tenant_id",
            "occurred_at",
        ),
    )
