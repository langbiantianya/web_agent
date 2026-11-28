"""页面权限配置的 JSON API（V4.10）。

给 SvelteKit 前端用。与页面路由 ``routes/access.py`` **共用判定**：
同一套租户范围解析、同一套 Casbin 绑定校验、同一套审计动作。差别只有
请求体是 JSON、CSRF 令牌从 ``X-CSRF-Token`` 头取、失败返回 JSON。

**绑定是按「页面 × 租户范围」整组替换的**，不是逐条增删：一次保存先删掉
该范围下的旧绑定，再写入新绑定。这样不会出现「勾了一半的权限」这种中间
状态，也和页面路由的语义一致。
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from ludic.web import Request

from app.models.admin_control import (
    AdminPageDefinition,
    AdminPagePermission,
    permission_binding_scope,
)
from services.admin_web.access_api import (
    parse_api_permissions,
    permission_catalog,
    resolve_tenant,
)
from services.admin_web.access_policy import CRITICAL_PAGES
from services.admin_web.api_support import (
    ApiError,
    api_route,
    authorize,
    json_response,
    read_json_body,
)
from services.admin_web.audit import audit_principal_action
from services.admin_web.registry import page_spec
from services.admin_web.routes.access import (
    _available_tenants,
    _bindable_permissions,
    _may_bind_permissions,
)

PAGE_CODE = "access_pages"
COMPONENT_CODE = "page_matrix"


def _page_payload(definition: Any, bindings: list[dict[str, Any]]) -> dict[str, Any]:
    """把页面定义与它的绑定投影成前端形状。"""
    spec = page_spec(definition.code)
    return {
        "code": definition.code,
        "title": definition.title,
        "navGroup": definition.nav_group,
        "navOrder": definition.nav_order,
        "enabled": bool(definition.enabled),
        "accessMode": definition.access_mode,
        "critical": definition.code in CRITICAL_PAGES,
        "recommended": [f"{name}.{action}" for name, action in spec.permissions]
        if spec is not None
        else [],
        "bindings": bindings,
    }


def _group_bindings(rows: list[Any]) -> dict[str, list[dict[str, Any]]]:
    """把绑定行按页面分组，保留每个租户范围各自的权限集。"""
    grouped: dict[str, dict[str | None, list[str]]] = {}
    for row in rows:
        scope = grouped.setdefault(row.page_code, {})
        permissions = scope.setdefault(row.tenant_id, [])
        permissions.append(f"{row.object_name}.{row.action_name}")
    return {
        code: [
            {"tenantId": tenant_id, "permissions": sorted(permissions)}
            for tenant_id, permissions in sorted(
                scopes.items(), key=lambda item: (item[0] is not None, item[0] or "")
            )
        ]
        for code, scopes in grouped.items()
    }


def register(app: Any) -> None:
    """把页面权限的 JSON API 挂到 app 上。"""

    @app.get("/admin/api/access/pages")
    @api_route
    async def api_access_pages_list(request: Request):
        """列出全部页面定义与当前可见范围内的绑定。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=False
        )
        try:
            definitions = list(
                await db.scalars(
                    sa.select(AdminPageDefinition).order_by(
                        AdminPageDefinition.nav_order, AdminPageDefinition.code
                    )
                )
            )
            statement = sa.select(AdminPagePermission)
            if not principal.is_platform_super_admin:
                statement = statement.where(
                    sa.or_(
                        AdminPagePermission.tenant_id.is_(None),
                        AdminPagePermission.tenant_id == principal.tenant_id,
                    )
                )
            rows = list(await db.scalars(statement))
            grouped = _group_bindings(rows)
            if not principal.is_platform_super_admin:
                # 租户管理员只看自己租户的覆盖：全局绑定被覆盖时，
                # 全局那一条不再对他生效，混在一起展示会误导。
                for code, scopes in list(grouped.items()):
                    tenant_scopes = [
                        scope for scope in scopes if scope["tenantId"]
                    ]
                    if tenant_scopes:
                        grouped[code] = tenant_scopes
            bindable = await _bindable_permissions(db, principal)
            items = [
                _page_payload(definition, grouped.get(definition.code, []))
                for definition in definitions
            ]
            return json_response(
                {
                    "items": items,
                    "permissionOptions": permission_catalog(bindable),
                    "tenantOptions": await _available_tenants(db, principal, rows),
                }
            )
        finally:
            await db.close()

    @app.put("/admin/api/access/pages/{page_code}")
    @api_route
    async def api_access_pages_save(page_code: str, request: Request):
        """保存或重置某个租户范围下的页面权限绑定。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=True
        )
        try:
            body = await read_json_body(request)
            definition = await db.get(AdminPageDefinition, page_code)
            if definition is None:
                raise ApiError("not_found", "页面不存在", status_code=404)
            tenant_id = resolve_tenant(principal, body.get("tenantId"))
            operation = str(body.get("operation") or "save")
            if operation not in {"save", "reset"}:
                raise ApiError(
                    "invalid_input", "operation 只能是 save 或 reset", status_code=400
                )
            if operation == "reset":
                if tenant_id is None:
                    raise ApiError(
                        "invalid_input", "重置必须指定租户范围", status_code=400
                    )
                await db.execute(
                    sa.delete(AdminPagePermission).where(
                        AdminPagePermission.page_code == page_code,
                        AdminPagePermission.tenant_id == tenant_id,
                    )
                )
                await audit_principal_action(
                    db,
                    principal,
                    action="admin_ui.page_permissions.reset",
                    target_type="admin_page",
                    target_id=page_code,
                    tenant_id=tenant_id,
                    detail={"scope": permission_binding_scope(tenant_id)},
                )
                await db.commit()
                return json_response(
                    {"item": {"pageCode": page_code, "tenantId": tenant_id, "permissions": []}}
                )
            permissions = parse_api_permissions(body.get("permissions"))
            if page_code in CRITICAL_PAGES and not permissions:
                raise ApiError(
                    "invalid_input", "关键页面不能被完全关闭", status_code=400
                )
            if tenant_id is not None and not permissions:
                raise ApiError(
                    "invalid_input", "租户级绑定不能为空", status_code=400
                )
            if not await _may_bind_permissions(db, principal, permissions):
                raise ApiError(
                    "forbidden", "不能绑定超出自身范围的权限", status_code=403
                )
            await db.execute(
                sa.delete(AdminPagePermission).where(
                    AdminPagePermission.page_code == page_code,
                    (
                        AdminPagePermission.tenant_id.is_(None)
                        if tenant_id is None
                        else AdminPagePermission.tenant_id == tenant_id
                    ),
                )
            )
            for object_name, action in permissions:
                db.add(
                    AdminPagePermission(
                        tenant_id=tenant_id,
                        binding_scope=permission_binding_scope(tenant_id),
                        page_code=page_code,
                        object_name=object_name,
                        action_name=action,
                    )
                )
            await audit_principal_action(
                db,
                principal,
                action="admin_ui.page_permissions.update",
                target_type="admin_page",
                target_id=page_code,
                tenant_id=tenant_id,
                detail={
                    "scope": permission_binding_scope(tenant_id),
                    "permissions": [f"{name}.{action}" for name, action in permissions],
                },
            )
            await db.commit()
            return json_response(
                {
                    "item": {
                        "pageCode": page_code,
                        "tenantId": tenant_id,
                        "permissions": [
                            f"{name}.{action}" for name, action in permissions
                        ],
                    }
                }
            )
        finally:
            await db.close()
