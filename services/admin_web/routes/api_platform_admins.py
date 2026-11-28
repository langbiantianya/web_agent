"""平台超级管理员管理的 JSON API（V4.10）。

只有 **owner** 能管平台超级管理员——这个限制比页面权限更严：页面注册表
把 ``platform_admin.owner`` 挂在页面上，但平台超级管理员本身会绕过 Casbin
判定，所以这里必须**显式**检查 ``is_owner``。

删除是硬删除，所以删除前留快照并把 ``undoId`` 返回给前端：误删之后还能
在撤销窗口内原样恢复。
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from ludic.web import Request

from app.core.user_profile import normalize_email, normalize_phone
from app.models.admin_control import (
    SuperAdminAccount,
    SuperAdminPasswordReset,
    SuperAdminSession,
)
from services.admin_web.accounts import (
    LoginError,
    create_super_admin_account,
    reset_super_admin_password,
    revoke_super_admin_sessions,
)
from services.admin_web.api_support import (
    ApiError,
    api_route,
    authorize,
    json_response,
    read_json_body,
)
from services.admin_web.audit import audit_principal_action
from services.admin_web.deletion import record_deletion
from services.admin_web.listing import parse_list_params
from services.admin_web.security import normalize_super_admin_username

PAGE_CODE = "platform_admins"
COMPONENT_CODE = "admin_list"


def _account_payload(account: Any) -> dict[str, Any]:
    """把超级管理员账号投影成前端形状（不含任何口令材料）。"""
    return {
        "id": account.id,
        "username": account.username,
        "email": account.email,
        "phone": account.phone,
        "status": account.status,
        "ownerSlot": account.owner_slot,
        "createdAt": str(account.created_at) if account.created_at else None,
        "updatedAt": str(account.updated_at) if account.updated_at else None,
    }


def _require_owner(principal: Any) -> None:
    """平台超级管理员管理只对 owner 开放。"""
    if not principal.is_platform_super_admin or not principal.is_owner:
        raise ApiError("not_found", "页面不可用", status_code=404)


def register(app: Any) -> None:
    """把平台超级管理员的 JSON API 挂到 app 上。"""

    @app.get("/admin/api/platform-admins")
    @api_route
    async def api_platform_admins_list(request: Request):
        """列出平台超级管理员账号。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=False
        )
        try:
            _require_owner(principal)
            params = parse_list_params(request)
            accounts = list(
                await db.scalars(
                    sa.select(SuperAdminAccount).order_by(SuperAdminAccount.id)
                )
            )
            keyword = params.q.casefold()
            if keyword:
                accounts = [
                    account
                    for account in accounts
                    if keyword
                    in " ".join(
                        str(value or "")
                        for value in (
                            account.username,
                            account.email,
                            account.phone,
                        )
                    ).casefold()
                ]
            start = params.offset
            page_items = accounts[start:start + params.size]
            return json_response(
                {
                    "items": [_account_payload(account) for account in page_items],
                    "total": len(accounts),
                    "page": {
                        "q": params.q,
                        "sort": params.sort,
                        "page": params.page,
                        "size": params.size,
                    },
                }
            )
        finally:
            await db.close()

    @app.post("/admin/api/platform-admins")
    @api_route
    async def api_platform_admins_create(request: Request):
        """创建平台超级管理员并发送随机密码。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=True
        )
        try:
            _require_owner(principal)
            body = await read_json_body(request)
            raw_username = str(body.get("username") or "").strip()
            raw_email = str(body.get("email") or "").strip()
            raw_phone = str(body.get("phone") or "").strip()
            username = normalize_super_admin_username(raw_username) or ""
            email = normalize_email(raw_email) if raw_email else None
            phone = normalize_phone(raw_phone) if raw_phone else None
            if (
                not username
                or not (email or phone)
                or (raw_email and email is None)
                or (raw_phone and phone is None)
            ):
                raise ApiError(
                    "invalid_input",
                    "用户名不合法，且必须留一个可送达的邮箱或手机号",
                    status_code=400,
                    field_errors={"username": "取值不合法"},
                )
            try:
                account, _delivery = await create_super_admin_account(
                    db,
                    username=username,
                    email=email,
                    phone=phone,
                    created_by_id=principal.principal_id,
                    password_sender=request.app.state.admin_password_sender,
                )
            except LoginError as exc:
                code = (
                    "duplicate"
                    if str(exc) == "super_admin_exists"
                    else "notification_failed"
                )
                raise ApiError(code, "账号创建失败", status_code=409) from exc
            except ValueError as exc:
                raise ApiError(
                    "invalid_input", "账号信息不合法", status_code=400
                ) from exc
            await audit_principal_action(
                db,
                principal,
                action="platform_admin.account.create",
                target_type="super_admin_account",
                target_id=str(account.id),
                detail={"target_username": account.username},
            )
            await db.commit()
            return json_response({"item": _account_payload(account)}, status_code=201)
        finally:
            await db.close()

    @app.delete("/admin/api/platform-admins/{account_id}")
    @api_route
    async def api_platform_admins_delete(account_id: int, request: Request):
        """删除平台超级管理员，并留下可撤销的快照。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=True
        )
        try:
            _require_owner(principal)
            account = await db.get(SuperAdminAccount, account_id)
            if account is None:
                raise ApiError("not_found", "账号不存在", status_code=404)
            if account.owner_slot == "superAdmin" or account.id == principal.principal_id:
                raise ApiError("in_use", "不能删除 owner 账号", status_code=409)
            entry = await record_deletion(
                db,
                resource_type="super_admin_account",
                row=account,
                actor_id=principal.principal_id,
                actor_username=principal.username,
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
            await db.execute(
                sa.delete(SuperAdminSession).where(
                    SuperAdminSession.account_id == account.id
                )
            )
            await db.execute(
                sa.delete(SuperAdminPasswordReset).where(
                    SuperAdminPasswordReset.account_id == account.id
                )
            )
            try:
                await db.delete(account)
                await db.commit()
            except sa.exc.IntegrityError as exc:
                await db.rollback()
                raise ApiError("in_use", "账号仍被引用，不能删除", status_code=409) from exc
            return json_response({"ok": True, "undoId": entry.id})
        finally:
            await db.close()

    @app.post("/admin/api/platform-admins/{account_id}/reset-password")
    @api_route
    async def api_platform_admins_reset(account_id: int, request: Request):
        """重置账号密码并把随机新密码发到登记渠道。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=True
        )
        try:
            _require_owner(principal)
            account = await db.get(SuperAdminAccount, account_id)
            if account is None:
                raise ApiError("not_found", "账号不存在", status_code=404)
            try:
                _reset, delivery = await reset_super_admin_password(
                    db,
                    account,
                    reason="manual_reset",
                    password_sender=request.app.state.admin_password_sender,
                )
            except LoginError as exc:
                await audit_principal_action(
                    db,
                    principal,
                    action="platform_admin.password.reset",
                    target_type="super_admin_account",
                    target_id=str(account.id),
                    outcome="denied",
                    detail={"code": str(exc)},
                )
                await db.commit()
                raise ApiError(
                    "invalid_input", "密码重置被拒绝", status_code=400
                ) from exc
            await audit_principal_action(
                db,
                principal,
                action="platform_admin.password.reset",
                target_type="super_admin_account",
                target_id=str(account.id),
                outcome="success" if delivery.delivered else "failed",
            )
            await db.commit()
            return json_response(
                {"item": {"id": account_id, "delivered": bool(delivery.delivered)}}
            )
        finally:
            await db.close()
