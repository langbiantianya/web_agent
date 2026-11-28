"""访问控制 JSON API 的共享形状与校验（V4.10）。

页面权限、组件权限、角色与映射四组端点共用同一批判定：租户范围解析、
权限值解析、权限目录投影。把它们放在一处，是为了**只有一份实现**——
四组端点各写一遍解析逻辑，迟早会在「跨租户」这条线上出现不一致。

这些判定与页面路由（``routes/access*.py``）同源：同一套
``parse_permission_values``、同一套 Casbin 判定、同一套租户正则。
"""

from __future__ import annotations

from typing import Any

from services.admin_web.access_policy import (
    PERMISSION_DESCRIPTIONS,
    TENANT_ID_RE,
    parse_permission_values,
    permission_id,
)
from services.admin_web.api_support import ApiError


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
