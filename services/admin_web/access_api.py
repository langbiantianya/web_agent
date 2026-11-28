"""访问控制 JSON API 的共享形状与校验（V4.10）。

页面权限、组件权限、角色与映射四组端点共用同一批判定：租户范围解析、
权限值解析、权限目录投影。把它们放在一处，是为了**只有一份实现**——
四组端点各写一遍解析逻辑，迟早会在「跨租户」这条线上出现不一致。

这些判定与页面路由（``routes/access*.py``）同源：同一套
``parse_permission_values``、同一套 Casbin 判定、同一套租户正则。
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa

from app.config.settings import settings
from app.models.admin_control import CasbinRule, OAuthRoleMapping
from app.models.oauth import OAuthProvider
from services.admin_web.access_policy import (
    KNOWN_PERMISSIONS,
    PERMISSION_DESCRIPTIONS,
    PLATFORM_ONLY_PERMISSIONS,
    TENANT_ID_RE,
    parse_permission_values,
    permission_id,
)
from services.admin_web.api_support import ApiError
from services.admin_web.rbac import casbin_service


def permission_catalog(
    permissions: list[tuple[str, str]],
) -> list[dict[str, str]]:
    """把权限集合投影成前端可直接渲染的目录。

    每个条目给 ``id``（``对象.操作``）、对象名、操作名与一句说明。前端用
    ``id`` 回传，用对象名分组渲染成矩阵。
    """
    return [
        {
            "id": permission_id(permission),
            "object": permission[0],
            "action": permission[1],
            "description": PERMISSION_DESCRIPTIONS.get(permission[0], ""),
        }
        for permission in sorted(permissions)
    ]


def parse_api_permissions(values: Any) -> list[tuple[str, str]]:
    """解析请求里的权限值列表。

    与页面路由共用 :func:`parse_permission_values`，区别只在失败时的形状：
    这里抛 :class:`ApiError`，让前端拿到稳定的机器码而不是一段 HTML。
    """
    if values is None:
        return []
    if not isinstance(values, list):
        raise ApiError(
            "invalid_input",
            "permissions 必须是数组",
            status_code=400,
            field_errors={"permissions": "必须是权限值数组"},
        )
    try:
        return parse_permission_values([str(value) for value in values])
    except ValueError as exc:
        reason = str(exc)
        message = (
            "权限值格式不正确，应为 对象.操作"
            if reason == "invalid_permission"
            else "存在未知权限"
        )
        raise ApiError(
            "invalid_input",
            message,
            status_code=400,
            field_errors={"permissions": message},
        ) from exc


def resolve_tenant(principal: Any, raw: Any) -> str | None:
    """解析写入目标租户，并挡住跨租户写入。

    平台超级管理员可以写全局（``None``）或任一租户；租户管理员只能写自己
    的租户，传了别人的租户一律拒绝。

    抛出的 :class:`ApiError` 区分两种失败：**租户名不合法**是 400，
    **越权写别人租户**是 403——前者是请求写错了，后者是被拒绝了。
    """
    text = str(raw or "").strip()
    if principal.is_platform_super_admin:
        if not text:
            return None
        if not TENANT_ID_RE.fullmatch(text):
            raise ApiError(
                "invalid_input",
                "租户标识不合法",
                status_code=400,
                field_errors={"tenantId": "租户标识不合法"},
            )
        return text
    if not principal.tenant_id:
        raise ApiError("forbidden", "当前管理员没有租户范围", status_code=403)
    if text and text != principal.tenant_id:
        raise ApiError("forbidden", "不能修改其他租户的界面策略", status_code=403)
    return principal.tenant_id


# ---------------------------------------------------------------------------
# 页面路由与 JSON API 共用的判定
#
# 这些函数原先住在 ``routes/access*.py`` 里，页面与 API 各自 import 过去。
# Ludic 视图层下线后，页面路由整个消失，它们就成了**只有 API 在用**的
# 公共判定——所以搬到这里，与租户解析、权限目录投影放在一起。
# ---------------------------------------------------------------------------


async def may_bind_permissions(
    db, principal: Any, permissions: list[tuple[str, str]]
) -> bool:
    """租户管理员只能绑定自己拥有的普通权限。"""
    if principal.is_platform_super_admin:
        return True
    for object_name, action in permissions:
        if (object_name, action) not in KNOWN_PERMISSIONS:
            return False
        if object_name in {"cross_tenant", "oauth_provider", "platform_admin"}:
            return False
        if (object_name, action) in PLATFORM_ONLY_PERMISSIONS:
            return False
        if not await casbin_service.can(
            db,
            user_id=principal.user_id or "",
            tenant_id=principal.tenant_id or "default",
            object_name=object_name,
            action=action,
        ):
            return False
    return True


async def bindable_permissions(db, principal: Any) -> list[tuple[str, str]]:
    """列出当前管理员有权继续绑定的权限。"""
    if principal.is_platform_super_admin:
        return sorted(KNOWN_PERMISSIONS)
    return [
        permission
        for permission in sorted(KNOWN_PERMISSIONS)
        if await may_bind_permissions(db, principal, [permission])
    ]


async def available_tenants(
    db, principal: Any, rows: list[Any]
) -> list[str]:
    """列出可创建覆盖的租户。"""
    if not principal.is_platform_super_admin:
        return [principal.tenant_id] if principal.tenant_id else []
    values = {
        row.tenant_id for row in rows if getattr(row, "tenant_id", None)
    }
    if settings.auth_default_tenant:
        values.add(settings.auth_default_tenant)
    values.update(
        tenant_id
        for tenant_id in await db.scalars(
            sa.select(OAuthProvider.tenant_id).distinct()
        )
        if tenant_id
    )
    return sorted(values)


async def mapping_provider(db, principal: Any, mapping: OAuthRoleMapping):
    """读取映射对应供应商，并验证当前管理员可见。"""
    provider = await db.get(OAuthProvider, mapping.provider_id)
    if provider is None:
        return None
    if (
        not principal.is_platform_super_admin
        and provider.tenant_id != principal.tenant_id
    ):
        return None
    return provider


async def role_exists_for_tenant(db, role_code: str, tenant_id: str) -> bool:
    """检查角色在供应商租户是否有权限策略。"""
    return (
        await db.scalar(
            sa.select(CasbinRule.id).where(
                CasbinRule.ptype == "p",
                CasbinRule.v0 == f"role:{role_code}",
                CasbinRule.v1.in_(
                    (f"tenant:{tenant_id}", "tenant:*", "*")
                ),
            )
        )
        is not None
    )


async def available_role_codes(db, principal: Any) -> list[str]:
    """列出当前管理员可映射的 Casbin 角色代码。"""
    rules = await casbin_service.list_policies(
        db,
        tenant_id=(
            None
            if principal.is_platform_super_admin
            else principal.tenant_id
        ),
    )
    return sorted(
        {
            (rule.v0 or "").removeprefix("role:")
            for rule in rules
            if rule.ptype == "p" and (rule.v0 or "").startswith("role:")
        }
    )


def provider_visible(principal: Any, provider: OAuthProvider) -> bool:
    """判断供应商是否在管理员可管理租户内。"""
    return principal.is_platform_super_admin or (
        provider.tenant_id == principal.tenant_id
    )


async def role_exists(db, role_code: str, domain: str) -> bool:
    """判断角色是否至少有一条权限策略。"""
    return (
        await db.scalar(
            sa.select(CasbinRule.id).where(
                CasbinRule.ptype == "p",
                CasbinRule.v0 == f"role:{role_code}",
                CasbinRule.v1 == domain,
            )
        )
        is not None
    )


async def mapping_counts(db, principal: Any) -> dict[str, int]:
    """统计每个角色被多少条 OAuth 映射引用。"""
    statement = (
        sa.select(OAuthRoleMapping.role_code, sa.func.count())
        .join(OAuthProvider, OAuthProvider.id == OAuthRoleMapping.provider_id)
        .group_by(OAuthRoleMapping.role_code)
    )
    if not principal.is_platform_super_admin:
        statement = statement.where(OAuthProvider.tenant_id == principal.tenant_id)
    return {
        str(role_code): int(count)
        for role_code, count in (await db.execute(statement)).all()
    }
