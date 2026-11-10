"""平台超级管理员管理的路由。"""

from __future__ import annotations

import hashlib
from typing import Any

import sqlalchemy as sa
from ludic.web import Request
from starlette.responses import RedirectResponse

from app.core.user_profile import normalize_email, normalize_phone
from app.models.admin_control import SuperAdminAccount
from services.admin_web.accounts import (
    LoginError, create_super_admin_account, reset_super_admin_password,
    revoke_super_admin_sessions,
)
from services.admin_web.audit import audit_principal_action, record_admin_audit
from services.admin_web.common import (
    login_redirect, not_found, origin_ok, require_csrf, unauthorized, with_auth,
)
from services.admin_web.db import AdminSessionLocal
from services.admin_web.listing import parse_list_params
from services.admin_web.registry import visible_pages
from services.admin_web.security import (
    clear_super_admin_cookies, normalize_super_admin_username,
)
from services.admin_web.views import platform_admins_page


def _filter_accounts(accounts: list[Any], query: str) -> list[Any]:
    """按关键字过滤平台超级管理员账号（大小写不敏感）。"""
    keyword = (query or "").strip().casefold()
    if not keyword:
        return list(accounts)
    return [
        account
        for account in accounts
        if keyword
        in " ".join(
            str(value or "")
            for value in (
                getattr(account, "username", ""),
                getattr(account, "email", ""),
                getattr(account, "phone", ""),
            )
        ).casefold()
    ]


def register(app: Any) -> None:
    """把平台超级管理员管理的路由挂到 app 上。"""

    @app.get("/admin/platform-admins")
    async def platform_admins_get(request: Request):
        """owner 专属平台超级管理员页。"""
        values = await with_auth(request)
        if values is None:
            return unauthorized()
        db, principal, pref, csrf = values
        try:
            if not principal.is_platform_super_admin or not principal.is_owner:
                return not_found()
            # 列表页的搜索 / 分页（UX-06）。
            list_params = parse_list_params(request)
            accounts = list(
                await db.scalars(
                    sa.select(SuperAdminAccount).order_by(SuperAdminAccount.id)
                )
            )
            accounts = _filter_accounts(accounts, list_params.q)
            total = len(accounts)
            start = list_params.offset
            accounts = accounts[start:start + list_params.size]
            return platform_admins_page(
                principal=principal,
                csrf=csrf,
                pages=await visible_pages(db, principal),
                accounts=accounts,
                theme=pref.theme,
                color_mode=pref.color_mode,
                density=pref.density,
                list_params=list_params,
                list_total=total,
            )
        finally:
            await db.close()

    @app.post("/admin/platform-admins")
    async def platform_admins_post(request: Request):
        """owner 创建平台超级管理员并发送随机密码。"""
        values = await with_auth(request)
        if values is None:
            return unauthorized()
        db, principal, _, _ = values
        try:
            if (
                not origin_ok(request)
                or not principal.is_platform_super_admin
                or not principal.is_owner
            ):
                return not_found()
            form = await request.form()
            if not await require_csrf(db, request, principal, form):
                return not_found()
            raw_username = str(form.get("username") or "").strip()
            raw_email = str(form.get("email") or "").strip()
            raw_phone = str(form.get("phone") or "").strip()
            username = normalize_super_admin_username(raw_username) or ""
            email = normalize_email(raw_email) if raw_email else None
            phone = normalize_phone(raw_phone) if raw_phone else None
            if (
                not username
                or not (email or phone)
                or (raw_email and email is None)
                or (raw_phone and phone is None)
            ):
                return RedirectResponse(
                    "/admin/platform-admins?error=invalid_fields", status_code=303
                )
            try:
                account, _ = await create_super_admin_account(
                    db,
                    username=username,
                    email=email,
                    phone=phone,
                    created_by_id=principal.principal_id,
                    password_sender=app.state.admin_password_sender,
                )
            except LoginError as exc:
                code = (
                    "username_exists"
                    if str(exc) == "super_admin_exists"
                    else "notification_failed"
                )
                return RedirectResponse(
                    f"/admin/platform-admins?error={code}", status_code=303
                )
            except ValueError:
                return RedirectResponse(
                    "/admin/platform-admins?error=invalid_fields", status_code=303
                )
            await audit_principal_action(
                db,
                principal,
                action="platform_admin.account.create",
                target_type="super_admin_account",
                target_id=str(account.id),
                detail={"target_username": account.username},
            )
            await db.commit()
            return RedirectResponse(
                "/admin/platform-admins?notice=created", status_code=303
            )
        finally:
            await db.close()

    @app.post("/admin/platform-admins/{account_id}/delete")
    async def platform_admins_delete(account_id: int, request: Request):
        """owner 删除平台超级管理员。"""
        values = await with_auth(request)
        if values is None:
            return unauthorized()
        db, principal, _, _ = values
        try:
            if (
                not origin_ok(request)
                or not principal.is_platform_super_admin
                or not principal.is_owner
            ):
                return not_found()
            form = await request.form()
            if not await require_csrf(db, request, principal, form):
                return not_found()
            account = await db.get(SuperAdminAccount, account_id)
            if account is None:
                return not_found()
            if account.owner_slot == "superAdmin" or account.id == principal.principal_id:
                return RedirectResponse(
                    "/admin/platform-admins?error=owner_protected", status_code=303
                )
            await audit_principal_action(
                db,
                principal,
                action="platform_admin.account.delete",
                target_type="super_admin_account",
                target_id=str(account.id),
                detail={"target_username": account.username},
            )
            await revoke_super_admin_sessions(db, account.id)
            await db.delete(account)
            await db.commit()
            return RedirectResponse(
                "/admin/platform-admins?notice=deleted", status_code=303
            )
        finally:
            await db.close()

    @app.post("/admin/super-admin/forgot-password")
    async def forgot_password(request: Request):
        """向数据库已登记渠道发送随机新密码，响应不泄露账号状态。"""
        if not origin_ok(request):
            return login_redirect("invalid_origin")
        form = await request.form()
        username = str(form.get("username") or "").strip()
        async with AdminSessionLocal() as db:
            outcome = "accepted"
            reason = "account_missing"
            account = await db.scalar(
                sa.select(SuperAdminAccount).where(
                    SuperAdminAccount.username == username,
                    SuperAdminAccount.status == "active",
                )
            )
            if account is not None and (account.email or account.phone):
                try:
                    await reset_super_admin_password(
                        db,
                        account,
                        reason="forgot_password",
                        requested_ip_hash=hashlib.sha256(
                            (request.client.host if request.client else "").encode()
                        ).hexdigest(),
                        password_sender=app.state.admin_password_sender,
                    )
                    reason = "password_resent"
                except LoginError as exc:
                    reason = str(exc)
                    if reason == "reset_rate_limited":
                        outcome = "rate_limited"
            await record_admin_audit(
                db,
                actor_kind="anonymous",
                actor_id=None,
                actor_username=username,
                action="platform_admin.password.forgot",
                target_type="super_admin_account",
                target_id=username,
                outcome=outcome,
                detail={"reason": reason},
            )
            await db.commit()
        return RedirectResponse(
            "/admin/login?notice=If+the+account+exists%2C+a+new+password+was+sent",
            status_code=303,
        )

    @app.post("/admin/super-admin/reset-my-password")
    async def reset_my_password(request: Request):
        """平台超级管理员重置自己的随机密码。"""
        values = await with_auth(request)
        if values is None:
            return unauthorized()
        db, principal, _, _ = values
        try:
            if not principal.is_platform_super_admin or not origin_ok(request):
                return not_found()
            form = await request.form()
            if not await require_csrf(db, request, principal, form):
                return not_found()
            account = await db.get(SuperAdminAccount, principal.principal_id)
            if account is None:
                return not_found()
            try:
                _, delivery = await reset_super_admin_password(
                    db,
                    account,
                    reason="manual_reset",
                    password_sender=app.state.admin_password_sender,
                )
            except LoginError as exc:
                await audit_principal_action(
                    db,
                    principal,
                    action="platform_admin.password.reset_self",
                    target_type="super_admin_account",
                    target_id=str(account.id),
                    outcome="denied",
                    detail={"code": str(exc)},
                )
                await db.commit()
                return RedirectResponse(
                    "/admin/preferences?error=reset_rate_limited", status_code=303
                )
            response = RedirectResponse(
                "/admin/login?notice=password_resent"
                if delivery.delivered
                else "/admin/preferences?error=notification_failed",
                status_code=303,
            )
            await audit_principal_action(
                db,
                principal,
                action="platform_admin.password.reset_self",
                target_type="super_admin_account",
                target_id=str(account.id),
                outcome="success" if delivery.delivered else "failed",
            )
            await db.commit()
            if delivery.delivered:
                clear_super_admin_cookies(response)
            return response
        finally:
            await db.close()
