"""管理员身份、Session、超级管理员凭证与轮换事务。"""

from __future__ import annotations

import hmac
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.core.admin_auth import (
    AdminPrincipal,
    resolve_admin_session,
    resolve_super_admin_session,
    verify_admin_csrf,
)
from app.core.user_profile import normalize_email, normalize_phone
from app.models.admin_control import (
    AdminSession,
    SuperAdminAccount,
    SuperAdminPasswordReset,
    SuperAdminSession,
)
from app.models.base import utcnow
from services.admin_web.notifications import DeliveryResult, notify_password
from services.admin_web.security import (
    generate_password,
    hash_password,
    new_token,
    normalize_super_admin_username,
    session_expiries,
    token_hash,
    verify_password,
)


class LoginError(Exception):
    """稳定登录错误码。"""


def _naive(value: object) -> datetime:
    """把 SQLite 字符串/OR 值统一为 naive UTC datetime。"""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    return datetime.fromisoformat(str(value)).replace(tzinfo=None)


async def create_admin_session(
    db: AsyncSession, *, admin_user_id: int, role_sync_version: int
) -> tuple[str, str]:
    """创建 OAuth 管理员独立 Session。"""
    token, csrf = new_token(), new_token()
    now = utcnow()
    idle, absolute = session_expiries()
    db.add(
        AdminSession(
            token_hash=token_hash(token),
            csrf_hash=token_hash(csrf),
            admin_user_id=admin_user_id,
            auth_at=now,
            last_seen_at=now,
            idle_expires_at=idle,
            absolute_expires_at=absolute,
            role_sync_version=role_sync_version,
        )
    )
    await db.flush()
    return token, csrf


async def create_super_admin_session(
    db: AsyncSession, account: SuperAdminAccount
) -> tuple[str, str]:
    """创建平台超级管理员独立 Session。"""
    token, csrf = new_token(), new_token()
    now = utcnow()
    idle, absolute = session_expiries()
    db.add(
        SuperAdminSession(
            token_hash=token_hash(token),
            csrf_hash=token_hash(csrf),
            account_id=account.id,
            auth_at=now,
            idle_expires_at=idle,
            absolute_expires_at=absolute,
            password_version=account.password_version,
        )
    )
    await db.flush()
    return token, csrf


async def authenticate_super_admin(
    db: AsyncSession, *, username: str, password: str
) -> SuperAdminAccount:
    """验证平台超级管理员用户名密码并维护失败计数。"""
    account = await db.scalar(
        sa.select(SuperAdminAccount).where(
            SuperAdminAccount.username == username
        )
    )
    now = utcnow()
    generic = LoginError("super_admin_login_failed")
    if account is None:
        raise generic
    if account.locked_until is not None and _naive(account.locked_until) > now:
        raise LoginError("super_admin_locked")
    if account.status != "active":
        raise generic
    if account.password_expires_at is not None:
        grace_end = _naive(account.password_expires_at) + timedelta(
            hours=settings.super_admin_rotation_grace_hours
        )
        if grace_end <= now:
            raise LoginError("super_admin_password_expired")
    if not verify_password(password, account.password_hash):
        account.failed_login_count = (account.failed_login_count or 0) + 1
        if account.failed_login_count >= 5:
            account.locked_until = now + timedelta(minutes=15)
            account.failed_login_count = 0
        await db.commit()
        raise generic
    account.failed_login_count = 0
    account.locked_until = None
    await db.flush()
    return account


async def revoke_admin_sessions(db: AsyncSession, admin_user_id: int) -> None:
    """撤销一个 OAuth 管理员的全部后台 Session。"""
    await db.execute(
        sa.update(AdminSession)
        .where(
            AdminSession.admin_user_id == admin_user_id,
            AdminSession.revoked_at.is_(None),
        )
        .values(revoked_at=utcnow())
    )


async def revoke_super_admin_sessions(
    db: AsyncSession, account_id: int
) -> None:
    """撤销一个平台超级管理员的全部 Session。"""
    await db.execute(
        sa.update(SuperAdminSession)
        .where(
            SuperAdminSession.account_id == account_id,
            SuperAdminSession.revoked_at.is_(None),
        )
        .values(revoked_at=utcnow())
    )


async def verify_session_csrf(
    db: AsyncSession,
    principal: AdminPrincipal,
    raw_token: str | None,
) -> bool:
    """校验 CSRF 原值同时匹配 Cookie 与数据库中的 Session 摘要。"""
    return await verify_admin_csrf(principal, raw_token)


def _expiry_for_new_password(now: datetime) -> datetime:
    """计算新密码的 30 天有效期。"""
    return now + timedelta(days=settings.super_admin_rotation_days)


async def create_super_admin_account(
    db: AsyncSession,
    *,
    username: str,
    email: str | None,
    phone: str | None,
    owner: bool = False,
    created_by_id: int | None = None,
    password_sender=notify_password,
) -> tuple[SuperAdminAccount, DeliveryResult]:
    """随机生成初始密码，通知成功后才创建 active 账号。"""
    username = normalize_super_admin_username(username) or ""
    normalized_email = normalize_email(email) if email else None
    normalized_phone = normalize_phone(phone) if phone else None
    if not username:
        raise ValueError("invalid_super_admin_username")
    if username == "superAdmin" and not owner:
        raise ValueError("reserved_owner_username")
    if owner and username != "superAdmin":
        raise ValueError("owner_username_must_be_superAdmin")
    if email and normalized_email is None:
        raise ValueError("invalid_super_admin_email")
    if phone and normalized_phone is None:
        raise ValueError("invalid_super_admin_phone")
    email, phone = normalized_email, normalized_phone
    if not email and not phone:
        raise ValueError("super_admin_notification_required")
    duplicates = [SuperAdminAccount.username == username]
    if email:
        duplicates.append(SuperAdminAccount.email == email)
    if phone:
        duplicates.append(SuperAdminAccount.phone == phone)
    if await db.scalar(sa.select(SuperAdminAccount.id).where(sa.or_(*duplicates))):
        raise LoginError("super_admin_exists")
    password = generate_password()
    delivery = await password_sender(
        username=username,
        email=email,
        phone=phone,
        password=password,
    )
    if not delivery.delivered:
        raise LoginError("super_admin_notification_failed")
    now = utcnow()
    account = SuperAdminAccount(
        username=username,
        password_hash=hash_password(password),
        email=email,
        phone=phone,
        status="active",
        owner_slot="superAdmin" if owner else None,
        password_rotated_at=now,
        password_expires_at=_expiry_for_new_password(now),
        password_version=1,
        created_by_id=created_by_id,
    )
    db.add(account)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise LoginError("super_admin_exists") from exc
    await db.refresh(account)
    return account, delivery


async def ensure_owner_super_admin(db: AsyncSession) -> str | None:
    """首次安装创建 owner，并返回仅供首次启动日志输出的随机密码。

    已有 owner 时返回 ``None``，因此重启不会再次生成或记录密码。该初始化
    不依赖邮件或短信，避免首次安装被通知渠道配置阻塞。
    """
    existing = await db.scalar(
        sa.select(SuperAdminAccount.id).where(
            SuperAdminAccount.owner_slot == "superAdmin"
        )
    )
    if existing is not None:
        return None

    password = generate_password()
    now = utcnow()
    db.add(
        SuperAdminAccount(
            username="superAdmin",
            password_hash=hash_password(password),
            email=None,
            phone=None,
            status="active",
            owner_slot="superAdmin",
            password_rotated_at=now,
            password_expires_at=_expiry_for_new_password(now),
            password_version=1,
        )
    )
    try:
        await db.commit()
    except IntegrityError:
        # 多副本同时启动时只有一个实例能创建 owner；竞争失败者不记录密码。
        await db.rollback()
        return None
    return password


async def reset_super_admin_password(
    db: AsyncSession,
    account: SuperAdminAccount,
    *,
    reason: str,
    requested_ip_hash: str | None = None,
    password_sender=notify_password,
) -> tuple[SuperAdminPasswordReset, DeliveryResult]:
    """生成随机新密码，通知成功后原子启用并撤销旧 Session。"""
    account = await db.scalar(
        sa.select(SuperAdminAccount)
        .where(SuperAdminAccount.id == account.id)
        .with_for_update()
    )
    if account is None:
        raise LoginError("super_admin_not_found")
    now = utcnow()
    latest = await db.scalar(
        sa.select(SuperAdminPasswordReset)
        .where(
            SuperAdminPasswordReset.account_id == account.id,
            SuperAdminPasswordReset.status == "pending",
        )
        .order_by(SuperAdminPasswordReset.id.desc())
    )
    if (
        latest is not None
        and (now - _naive(latest.requested_at)).total_seconds()
        < settings.super_admin_reset_cooldown_seconds
    ):
        raise LoginError("reset_rate_limited")

    password = generate_password()
    reset = SuperAdminPasswordReset(
        account_id=account.id,
        pending_password_hash=hash_password(password),
        reason=reason,
        status="pending",
        requested_at=now,
        expires_at=now + timedelta(minutes=10),
        requested_ip_hash=requested_ip_hash,
    )
    db.add(reset)
    await db.commit()
    await db.refresh(reset)

    delivery = await password_sender(
        username=account.username,
        email=account.email,
        phone=account.phone,
        password=password,
    )
    reset.email_status = delivery.email_status
    reset.sms_status = delivery.sms_status
    if not delivery.delivered:
        reset.status = "failed"
        reset.failure_code = "notification_failed"
        await db.commit()
        return reset, delivery

    account.password_hash = reset.pending_password_hash
    account.password_rotated_at = now
    account.password_expires_at = _expiry_for_new_password(now)
    account.password_version = (account.password_version or 1) + 1
    account.status = "active"
    account.failed_login_count = 0
    account.locked_until = None
    account.version = (account.version or 1) + 1
    reset.status = "activated"
    reset.activated_at = now
    await revoke_super_admin_sessions(db, account.id)
    await db.commit()
    await db.refresh(reset)
    await db.refresh(account)
    return reset, delivery


async def rotate_expired_super_admins(
    db: AsyncSession,
    *,
    password_sender=notify_password,
) -> int:
    """轮换所有已经超过 30 天密码有效期的超级管理员。"""
    now = utcnow()
    accounts = list(
        await db.scalars(
            sa.select(SuperAdminAccount).where(
                SuperAdminAccount.status == "active",
                sa.or_(
                    SuperAdminAccount.password_expires_at.is_(None),
                    SuperAdminAccount.password_expires_at <= now,
                ),
            )
        )
    )
    rotated = 0
    for account in accounts:
        try:
            _, delivery = await reset_super_admin_password(
                db,
                account,
                reason="scheduled_rotation",
                password_sender=password_sender,
            )
            if delivery.delivered:
                rotated += 1
        except LoginError:
            continue
    return rotated


def constant_time_username(left: str, right: str) -> bool:
    """用户名比较也使用常量时间比较，避免细微时序差异。"""
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
