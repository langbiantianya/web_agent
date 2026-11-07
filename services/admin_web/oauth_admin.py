"""OAuth 后台管理员创建与角色同步。"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.core.user_accounts import UserProvisionError, provision_oidc_user
from app.models.admin_control import OAuthRoleMapping
from app.models.admin_user import AdminUser
from app.models.base import utcnow
from app.models.oauth import OAuthProvider
from services.admin_web.accounts import revoke_admin_sessions
from services.admin_web.rbac import casbin_service


def role_claim_values(provider: OAuthProvider, claims: dict) -> set[str]:
    """读取并规范化 provider 配置的 groups/roles claim。"""
    value = claims.get(provider.role_claim)
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {item for item in value if isinstance(item, str)}
    return set()


async def sync_oauth_admin(
    db: AsyncSession, provider: OAuthProvider, claims: dict
) -> AdminUser | None:
    """把 OAuth claims 映射成 Casbin role，并重建可用的管理员 Session。"""
    provisioned = await provision_oidc_user(db, provider, claims)
    user = provisioned.user
    existing = await db.scalar(
        sa.select(AdminUser).where(AdminUser.user_id == user.id)
    )
    if existing is not None and existing.provider_id != provider.id:
        # 一个管理员只有一个角色权威 provider；其他 provider 只完成普通身份认证。
        return existing if existing.status == "active" else None

    claim_present = provider.role_claim in claims
    external_roles = role_claim_values(provider, claims)
    mappings: list[str] = []
    if external_roles:
        rows = await db.scalars(
            sa.select(OAuthRoleMapping).where(
                OAuthRoleMapping.provider_id == provider.id,
                OAuthRoleMapping.claim_name == provider.role_claim,
                OAuthRoleMapping.external_value.in_(external_roles),
                OAuthRoleMapping.enabled == 1,
            )
        )
        mappings = sorted({row.role_code for row in rows})
    elif not claim_present and provider.role_claim_required == 0 and existing:
        # provider 明确声明 claim 可选时，缺失值不应被误判为“立即撤权”。
        mappings = sorted(
            {
                row.role
                for row in await casbin_service.role_rows(
                    db,
                    user_id=user.id,
                    tenant_id=user.tenant_id,
                )
                if row.role
            }
        )

    if existing is None and not mappings:
        return None
    admin = existing
    if admin is None:
        admin = AdminUser(
            user_id=user.id,
            provider_id=provider.id,
            tenant_id=user.tenant_id,
            status="active",
            enabled=1,
            version=1,
            last_role_sync_at=utcnow(),
        )
        db.add(admin)
        await db.flush()
    admin.provider_id = provider.id
    admin.tenant_id = user.tenant_id
    admin.status = "active" if mappings else "disabled"
    admin.enabled = 1 if mappings else 0
    admin.last_role_sync_at = utcnow()
    admin.version = (admin.version or 1) + 1

    await casbin_service.replace_user_roles(
        db,
        user_id=user.id,
        tenant_id=user.tenant_id,
        role_codes=mappings,
    )
    # 每次重新认证都撤销旧 Session。新 Session 在本次回调中随后创建，
    # 避免旧标签页继续使用同步前的角色快照。
    await revoke_admin_sessions(db, admin.id)
    await db.commit()
    await db.refresh(admin)
    if not mappings:
        return None
    return admin


async def provider_for_admin_login(
    db: AsyncSession, code: str
) -> OAuthProvider | None:
    """读取默认公开租户内启用的 OAuth 登录 provider。"""
    provider = await db.scalar(
        sa.select(OAuthProvider).where(
            OAuthProvider.tenant_id == settings.auth_default_tenant,
            OAuthProvider.code == code,
        )
    )
    if (
        provider is None
        or provider.enabled != 1
        or provider.admin_login_enabled != 1
    ):
        return None
    return provider


def provisioning_error_code(exc: Exception) -> str:
    """把身份/角色失败转成稳定错误码。"""
    if isinstance(exc, UserProvisionError):
        return str(exc)
    return "oauth_admin_login_failed"
