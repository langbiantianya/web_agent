"""组件权限配置的 JSON API（V4.10）。

与页面权限 API 同构，只是绑定对象从「页面」换成「页面下的组件」。
判定依旧复用 ``routes/access.py`` 里的租户与绑定校验，审计动作沿用
``admin_ui.component_permissions.*``。
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from ludic.web import Request

from app.models.admin_control import (
    AdminComponentDefinition,
    AdminComponentPermission,
    AdminPageDefinition,
    permission_binding_scope,
)
from services.admin_web.access_api import (
    parse_api_permissions,
    permission_catalog,
    resolve_tenant,
)
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

PAGE_CODE = "access_components"
COMPONENT_CODE = "component_matrix"


def _component_payload(
    definition: Any,
    bindings: list[dict[str, Any]],
    page_titles: dict[str, str],
) -> dict[str, Any]:
    """把组件定义与它的绑定投影成前端形状。"""
    spec = page_spec(definition.page_code)
    component_spec = (
        next(
            (item for item in spec.components if item.code == definition.code),
            None,
        )
        if spec is not None
        else None
    )
    return {
        "pageCode": definition.page_code,
        "pageTitle": page_titles.get(definition.page_code, definition.page_code),
        "componentCode": definition.code,
        "label": definition.label,
        "kind": definition.kind,
        "accessMode": definition.access_mode,
        "enabled": bool(definition.enabled),
        "sortOrder": definition.sort_order,
        "recommended": [
            f"{name}.{action}"
            for name, action in (
                component_spec.permissions if component_spec is not None else ()
            )
        ],
        "bindings": bindings,
    }


def _group_bindings(rows: list[Any]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """把绑定行按（页面, 组件）分组。"""
    grouped: dict[tuple[str, str], dict[str | None, list[str]]] = {}
    for row in rows:
        key = (row.page_code, row.component_code)
        scope = grouped.setdefault(key, {})
        scope.setdefault(row.tenant_id, []).append(
            f"{row.object_name}.{row.action_name}"
        )
    return {
        key: [
            {"tenantId": tenant_id, "permissions": sorted(permissions)}
            for tenant_id, permissions in sorted(
                scopes.items(), key=lambda item: (item[0] is not None, item[0] or "")
            )
        ]
        for key, scopes in grouped.items()
    }


def register(app: Any) -> None:
    """把组件权限的 JSON API 挂到 app 上。"""

    @app.get("/admin/api/access/components")
    @api_route
    async def api_access_components_list(request: Request):
        """列出全部组件定义与当前可见范围内的绑定。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=False
        )
        try:
            definitions = list(
                await db.scalars(
                    sa.select(AdminComponentDefinition).order_by(
                        AdminComponentDefinition.page_code,
                        AdminComponentDefinition.sort_order,
                        AdminComponentDefinition.code,
                    )
                )
            )
            statement = sa.select(AdminComponentPermission)
            if not principal.is_platform_super_admin:
                statement = statement.where(
                    sa.or_(
                        AdminComponentPermission.tenant_id.is_(None),
                        AdminComponentPermission.tenant_id == principal.tenant_id,
                    )
                )
            rows = list(await db.scalars(statement))
            grouped = _group_bindings(rows)
            if not principal.is_platform_super_admin:
                for key, scopes in list(grouped.items()):
                    tenant_scopes = [scope for scope in scopes if scope["tenantId"]]
                    if tenant_scopes:
                        grouped[key] = tenant_scopes
            page_rows = list(await db.scalars(sa.select(AdminPageDefinition)))
            page_titles = {row.code: row.title for row in page_rows}
            bindable = await _bindable_permissions(db, principal)
            items = [
                _component_payload(
                    definition,
                    grouped.get((definition.page_code, definition.code), []),
                    page_titles,
                )
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

    @app.put("/admin/api/access/components/{page_code}/{component_code}")
    @api_route
    async def api_access_components_save(
        page_code: str, component_code: str, request: Request
    ):
        """保存或重置某个租户范围下的组件权限绑定。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=True
        )
        try:
            body = await read_json_body(request)
            definition = await db.get(
                AdminComponentDefinition, (page_code, component_code)
            )
            if definition is None:
                raise ApiError("not_found", "组件不存在", status_code=404)
            tenant_id = resolve_tenant(principal, body.get("tenantId"))
            operation = str(body.get("operation") or "save")
            if operation not in {"save", "reset"}:
                raise ApiError(
                    "invalid_input", "operation 只能是 save 或 reset", status_code=400
                )
            target_id = f"{page_code}/{component_code}"
            if operation == "reset":
                if tenant_id is None:
                    raise ApiError(
                        "invalid_input", "重置必须指定租户范围", status_code=400
                    )
                await db.execute(
                    sa.delete(AdminComponentPermission).where(
                        AdminComponentPermission.page_code == page_code,
                        AdminComponentPermission.component_code == component_code,
                        AdminComponentPermission.tenant_id == tenant_id,
                    )
                )
                await audit_principal_action(
                    db,
                    principal,
                    action="admin_ui.component_permissions.reset",
                    target_type="admin_component",
                    target_id=target_id,
                    tenant_id=tenant_id,
                    detail={"scope": permission_binding_scope(tenant_id)},
                )
                await db.commit()
                return json_response(
                    {
                        "item": {
                            "pageCode": page_code,
                            "componentCode": component_code,
                            "tenantId": tenant_id,
                            "permissions": [],
                        }
                    }
                )
            permissions = parse_api_permissions(body.get("permissions"))
            if tenant_id is not None and not permissions:
                raise ApiError(
                    "invalid_input", "租户级绑定不能为空", status_code=400
                )
            if not await _may_bind_permissions(db, principal, permissions):
                raise ApiError(
                    "forbidden", "不能绑定超出自身范围的权限", status_code=403
                )
            await db.execute(
                sa.delete(AdminComponentPermission).where(
                    AdminComponentPermission.page_code == page_code,
                    AdminComponentPermission.component_code == component_code,
                    (
                        AdminComponentPermission.tenant_id.is_(None)
                        if tenant_id is None
                        else AdminComponentPermission.tenant_id == tenant_id
                    ),
                )
            )
            for object_name, action in permissions:
                db.add(
                    AdminComponentPermission(
                        tenant_id=tenant_id,
                        binding_scope=permission_binding_scope(tenant_id),
                        page_code=page_code,
                        component_code=component_code,
                        object_name=object_name,
                        action_name=action,
                    )
                )
            await audit_principal_action(
                db,
                principal,
                action="admin_ui.component_permissions.update",
                target_type="admin_component",
                target_id=target_id,
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
                        "componentCode": component_code,
                        "tenantId": tenant_id,
                        "permissions": [
                            f"{name}.{action}" for name, action in permissions
                        ],
                    }
                }
            )
        finally:
            await db.close()
