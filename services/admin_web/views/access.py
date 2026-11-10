"""访问控制页：页面权限与组件权限。

这两块放在一个模块里，因为它们共用同一套权限模型（页面定义、组件定义、
租户覆盖），拆开反而要来回 import。角色与 OAuth 声明映射在 ``views/roles.py``。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ludic.html import (
    button,
    datalist,
    div,
    form,
    h1,
    input as input_,
    option,
    p,
    section,
    small,
    span,
    table,
    tbody,
    td,
    th,
    thead,
    tr,
)

from app.models.admin_control import (
    AdminComponentDefinition,
    AdminComponentPermission,
    AdminPageDefinition,
    AdminPagePermission,
)
from services.admin_web.access_policy import KNOWN_PERMISSIONS
from services.admin_web.accounts import AdminPrincipal
from services.admin_web.views.shell import shell


def access_pages_page(
    *,
    principal: AdminPrincipal,
    csrf: str,
    pages: Iterable[AdminPageDefinition],
    definitions: list[AdminPageDefinition],
    bindings: dict[str, list[AdminPagePermission]],
    theme: str,
    color_mode: str,
    density: str,
    flash: str | None = None,
) -> Any:
    """渲染页面权限配置器。"""
    rows = []
    for definition in definitions:
        rows.append(
            tr(
                td(definition.title, small(definition.code, class_="block-muted")),
                td(definition.nav_group),
                td(
                    _permission_editor(
                        action=(
                            f"/admin/access/pages/{definition.code}/permissions"
                        ),
                        csrf=csrf,
                        rows=bindings.get(definition.code, []),
                        is_platform_super_admin=principal.is_platform_super_admin,
                        tenant_id=principal.tenant_id,
                        aria_label=definition.title,
                    )
                ),
            )
        )
    scopes = {
        row.binding_scope
        for rows in bindings.values()
        for row in rows
    }
    grant_count = sum(len(rows) for rows in bindings.values())
    return shell(
        title_text="Page permissions",
        principal=principal,
        csrf=csrf,
        theme=theme,
        color_mode=color_mode,
        density=density,
        pages=pages,
        flash=flash,
        content=section(
            div(
                div(
                    p("ACCESS CONTROL", class_="eyebrow"),
                    h1("Page permissions"),
                    p(
                        "Bind known pages to RBAC permissions. Global bindings are "
                        "defaults; tenant scopes replace them when present.",
                        class_="lead",
                    ),
                ),
                div(
                    span(f"{len(definitions)} pages", class_="metric-pill"),
                    span(f"{len(scopes)} scopes", class_="metric-pill"),
                    span(f"{grant_count} grants", class_="metric-pill"),
                    class_="access-summary",
                ),
                class_="access-hero",
            ),
            _permission_datalist(bindings),
            div(
                table(
                    thead(tr(th("Page"), th("Group"), th("Permissions"))),
                    tbody(*rows),
                    class_="data-table",
                ),
                class_="table-scroll",
            ),
            class_="surface",
        ),
    )


def _permission_datalist(bindings: dict[Any, list[Any]]) -> Any:
    """提供权限输入提示。"""
    current = sorted(
        {
            f"{row.object_name}.{row.action_name}"
            for rows in bindings.values()
            for row in rows
        }
    )
    return div(
        datalist(
            *[
                option(value=f"{object_name}.{action}")
                for object_name, action in sorted(KNOWN_PERMISSIONS)
            ],
            id="permission-list",
        ),
        p("Permission catalog", class_="eyebrow"),
        *[
            span(value, class_="chip")
            for value in current
        ],
        class_="chip-list",
    )


def _permission_editor(
    *,
    action: str,
    csrf: str,
    rows: list[Any],
    is_platform_super_admin: bool,
    tenant_id: str | None,
    aria_label: str,
) -> Any:
    """渲染现有作用域和新增租户覆盖的权限编辑表单。"""
    grouped: dict[str, list[Any]] = {}
    for row in rows:
        grouped.setdefault(row.binding_scope, []).append(row)
    editors = []
    for _, scoped_rows in sorted(grouped.items()):
        current = ", ".join(
            f"{row.object_name}.{row.action_name}"
            for row in scoped_rows
        )
        scope_name = scoped_rows[0].tenant_id or "global"
        editors.append(
            form(
                input_(type="hidden", name="csrf_token", value=csrf),
                (
                    input_(
                        type="text",
                        name="tenant_id",
                        value=scoped_rows[0].tenant_id or "",
                        placeholder="blank = global",
                        aria_label=f"Tenant scope for {aria_label}",
                    )
                    if is_platform_super_admin
                    else input_(
                        type="hidden",
                        name="tenant_id",
                        value=tenant_id or "",
                    )
                ),
                input_(
                    type="text",
                    name="permissions",
                    value=current,
                    list="permission-list",
                    placeholder="Choose permissions or clear to remove this override",
                    aria_label=f"{aria_label}, {scope_name} permissions",
                ),
                button("Save scope", type="submit", class_="btn btn-small"),
                action=action,
                method="post",
                class_="inline-edit",
            )
        )
    if is_platform_super_admin:
        editors.append(
            form(
                input_(type="hidden", name="csrf_token", value=csrf),
                input_(
                    type="text",
                    name="tenant_id",
                    placeholder="Tenant override (blank = global)",
                    aria_label=f"New tenant scope for {aria_label}",
                ),
                input_(
                    type="text",
                    name="permissions",
                    list="permission-list",
                    placeholder="workflow.read, admin_ui.manage",
                    aria_label=f"New scope permissions for {aria_label}",
                ),
                button("Upsert scope", type="submit", class_="btn btn-small"),
                action=action,
                method="post",
                class_="inline-edit",
            )
        )
    return div(*editors, class_="scope-editors")


def access_components_page(
    *,
    principal: AdminPrincipal,
    csrf: str,
    pages: Iterable[AdminPageDefinition],
    components: list[AdminComponentDefinition],
    bindings: dict[tuple[str, str], list[AdminComponentPermission]],
    theme: str,
    color_mode: str,
    density: str,
    flash: str | None = None,
) -> Any:
    """渲染组件权限配置器。"""
    rows = []
    for component in components:
        key = (component.page_code, component.code)
        rows.append(
            tr(
                td(
                    component.label,
                    small(
                        f"{component.page_code}/{component.code}",
                        class_="block-muted",
                    ),
                ),
                td(component.kind),
                td(
                    _permission_editor(
                        action=(
                            f"/admin/access/components/{component.page_code}/"
                            f"{component.code}/permissions"
                        ),
                        csrf=csrf,
                        rows=bindings.get(key, []),
                        is_platform_super_admin=principal.is_platform_super_admin,
                        tenant_id=principal.tenant_id,
                        aria_label=component.label,
                    )
                ),
            )
        )
    return shell(
        title_text="Component permissions",
        principal=principal,
        csrf=csrf,
        theme=theme,
        color_mode=color_mode,
        density=density,
        pages=pages,
        flash=flash,
        content=section(
            div(
                div(
                    p("ACCESS CONTROL", class_="eyebrow"),
                    h1("Component permissions"),
                    p(
                        "Control page-level actions, editors and tables. Components "
                        "without bindings are hidden; API authorization remains separate.",
                        class_="lead",
                    ),
                ),
                div(
                    span(f"{len(components)} components", class_="metric-pill"),
                    span(
                        f"{sum(len(rows) for rows in bindings.values())} grants",
                        class_="metric-pill",
                    ),
                    class_="access-summary",
                ),
                class_="access-hero",
            ),
            _permission_datalist(bindings),
            div(
                table(
                    thead(tr(th("Component"), th("Type"), th("Permissions"))),
                    tbody(*rows),
                    class_="data-table",
                ),
                class_="table-scroll",
            ),
            class_="surface",
        ),
    )
