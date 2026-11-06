"""OIDC claims 到本地用户与身份绑定的转换。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.user_profile import (
    generated_username,
    normalize_email,
    normalize_phone,
    normalize_username,
)
from app.models.oauth import OAuthProvider
from app.models.user import AppUser, UserIdentity


class UserProvisionError(Exception):
    """登录用户创建失败；值是稳定错误码。"""


@dataclass(frozen=True)
class ProvisionedIdentity:
    """身份绑定结果。"""

    user: AppUser
    created: bool


async def _identity_user(
    db: AsyncSession, issuer: str, subject: str
) -> tuple[UserIdentity, AppUser] | None:
    """按外部身份主键读取绑定与本地用户。"""
    row = (
        await db.execute(
            sa.select(UserIdentity, AppUser)
            .join(AppUser, AppUser.id == UserIdentity.user_id)
            .where(
                UserIdentity.issuer == issuer,
                UserIdentity.subject == subject,
            )
        )
    ).first()
    return (row[0], row[1]) if row is not None else None


async def provision_oidc_user(
    db: AsyncSession,
    provider: OAuthProvider,
    claims: dict,
) -> ProvisionedIdentity:
    """查找或创建本地用户。

    已绑定 ``(issuer, subject)`` 的身份不会因 provider 修改用户名或资料而变化。
    新用户才会读取 username/email/phone claims；冲突不会自动合并账号。
    """
    issuer = provider.issuer.rstrip("/")
    subject = str(claims["sub"])
    existing = await _identity_user(db, issuer, subject)
    if existing is not None:
        return ProvisionedIdentity(user=existing[1], created=False)
    if provider.auto_create != 1:
        raise UserProvisionError("account_not_provisioned")

    claim_username = normalize_username(claims.get(provider.username_claim))
    if claim_username is None:
        username = generated_username()
        for _ in range(5):
            taken = await db.scalar(
                sa.select(AppUser.id).where(
                    AppUser.tenant_id == provider.tenant_id,
                    AppUser.username == username,
                )
            )
            if taken is None:
                break
            username = generated_username()
        else:
            raise UserProvisionError("username_conflict")
    else:
        username = claim_username
        taken = await db.scalar(
            sa.select(AppUser.id).where(
                AppUser.tenant_id == provider.tenant_id,
                AppUser.username == username,
            )
        )
        if taken is not None:
            raise UserProvisionError("username_conflict")

    phone = normalize_phone(claims.get("phone_number") or claims.get("phone"))
    email = normalize_email(claims.get("email"))
    for column, value, code in (
        (AppUser.phone, phone, "phone_conflict"),
        (AppUser.email, email, "email_conflict"),
    ):
        if value is None:
            continue
        taken = await db.scalar(
            sa.select(AppUser.id).where(
                AppUser.tenant_id == provider.tenant_id,
                column == value,
            )
        )
        if taken is not None:
            raise UserProvisionError(code)

    user = AppUser(
        id=str(uuid.uuid4()),
        tenant_id=provider.tenant_id,
        username=username,
        phone=phone,
        phone_verified=(
            int(bool(claims["phone_number_verified"]))
            if phone is not None and isinstance(claims.get("phone_number_verified"), bool)
            else None
        ),
        email=email,
        email_verified=(
            int(bool(claims["email_verified"]))
            if email is not None and isinstance(claims.get("email_verified"), bool)
            else None
        ),
        status="active",
    )
    identity = UserIdentity(
        user_id=user.id,
        provider_id=provider.id,
        issuer=issuer,
        subject=subject,
    )
    db.add(user)
    db.add(identity)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        # 两个回调可能同时首次登录；唯一约束竞争后，只有真正建立绑定的一方
        # 能继续，另一方按稳定冲突失败，绝不自动按 username 合并。
        existing = await _identity_user(db, issuer, subject)
        if existing is not None:
            return ProvisionedIdentity(user=existing[1], created=False)
        raise UserProvisionError("profile_conflict") from exc
    await db.refresh(user)
    return ProvisionedIdentity(user=user, created=True)
