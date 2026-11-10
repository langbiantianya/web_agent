"""页面与组件权限配置的共享 UI 组件。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ludic.html import (
    button,
    datalist,
    details,
    div,
    fieldset,
    form,
    input as input_,
    label,
    legend,
    option,
    p,
    small,
    span,
    summary,
)

from services.admin_web.access_policy import (
    PERMISSION_DESCRIPTIONS,
    permission_groups,
    permission_id,
)
from services.admin_web.accounts import AdminPrincipal


def permission_matrix(
    *,
    options: Iterable[tuple[str, str]],
    selected: Iterable[tuple[str, str]],
) -> Any:
    """渲染可勾选的对象/操作权限矩阵。"""
    current = set(selected)
    groups = []
    for object_name, actions in permission_groups(set(options)):
        groups.append(
            fieldset(
                legend(object_name),
                p(
                    PERMISSION_DESCRIPTIONS.get(object_name, object_name),
                    class_="block-muted permission-help",
                ),
                div(
                    *[
                        label(
                            input_(
                                type="checkbox",
                                name="permissions",
                                value=permission_id(permission),
                                checked=permission in current,
                            ),
                            span(permission[1]),
                            class_="permission-choice",
                        )
                        for permission in actions
                    ],
                    class_="permission-choice-row",
                ),
                class_="permission-group",
            )
        )
    return div(*groups, class_="permission-matrix")


def recommended_permissions(
    permissions: Iterable[tuple[str, str]],
) -> Any:
    """显示代码注册表推荐的权限组合。"""
    values = list(permissions)
    return div(
        span("Recommended", class_="eyebrow"),
        *(
            [span(permission_id(item), class_="chip") for item in values]
            or [span("No recommendation", class_="chip")]
        ),
        class_="chip-list recommended-permissions",
    )


def _scope_label(rows: list[Any], tenant_id: str | None) -> str:
    """返回作用域的人类可读名称。"""
    return "Tenant override" if tenant_id else "Global default"


def _permission_values(rows: list[Any]) -> list[tuple[str, str]]:
    """把权限行转换为权限元组列表。"""
    return sorted({(row.object_name, row.action_name) for row in rows})


def scope_editor(
    *,
    action: str,
    csrf: str,
    rows: list[Any],
    tenant_id: str | None,
    options: Iterable[tuple[str, str]],
    editable: bool,
) -> Any:
    """渲染单个作用域的矩阵编辑器和覆盖重置操作。"""
    current = _permission_values(rows)
    label_text = _scope_label(rows, tenant_id)
    if not editable:
        return details(
            summary(
                span(label_text, class_="scope-label"),
                small(tenant_id, class_="block-muted") if tenant_id else span(""),
                span(f"{len(current)} permissions", class_="status-badge"),
            ),
            div(
                *[
                    span(permission_id(permission), class_="chip")
                    for permission in current
                ]
                or [p("No permissions are bound.", class_="block-muted")],
                class_="chip-list",
            ),
            class_="scope-editor readonly",
        )
    save_form = form(
        input_(type="hidden", name="csrf_token", value=csrf),
        input_(type="hidden", name="tenant_id", value=tenant_id or ""),
        input_(type="hidden", name="operation", value="save"),
        permission_matrix(options=options, selected=current),
        button(
            "Save permissions",
            type="submit",
            class_="btn btn-primary btn-small",
        ),
        action=action,
        method="post",
        class_="scope-save-form",
    )
    reset_form = (
        form(
            input_(type="hidden", name="csrf_token", value=csrf),
            input_(type="hidden", name="tenant_id", value=tenant_id or ""),
            input_(type="hidden", name="operation", value="reset"),
            button(
                "Reset to global",
                type="submit",
                class_="btn btn-danger btn-small",
                title=(
                    f"Remove the {tenant_id} override and fall back to the "
                    "global policy"
                ),
            ),
            action=action,
            method="post",
            class_="scope-reset-form",
        )
        if tenant_id and rows
        else span("")
    )
    return details(
        summary(
            span(label_text, class_="scope-label"),
            small(tenant_id, class_="block-muted") if tenant_id else span(""),
            span(f"{len(current)} permissions", class_="status-badge"),
        ),
        save_form,
        reset_form,
        p(
            "Tenant overrides replace the global binding while they exist. "
            "Use reset instead of saving an empty tenant selection.",
            class_="block-muted permission-help",
        ),
        class_="scope-editor",
    )


def scoped_editors(
    *,
    rows: list[Any],
    action: str,
    csrf: str,
    options: Iterable[tuple[str, str]],
    principal: AdminPrincipal,
    tenant_options: Iterable[str],
) -> Any:
    """按全局/租户作用域渲染编辑器，并补足当前租户的编辑入口。"""
    grouped: dict[str | None, list[Any]] = {}
    for row in rows:
        grouped.setdefault(row.tenant_id, []).append(row)
    editors = [
        scope_editor(
            action=action,
            csrf=csrf,
            rows=scoped_rows,
            tenant_id=tenant_id,
            options=options,
            editable=principal.is_platform_super_admin or tenant_id is not None,
        )
        for tenant_id, scoped_rows in sorted(
            grouped.items(),
            key=lambda item: (item[0] is not None, item[0] or ""),
        )
    ]
    if principal.is_platform_super_admin and None not in grouped:
        editors.insert(
            0,
            scope_editor(
                action=action,
                csrf=csrf,
                rows=[],
                tenant_id=None,
                options=options,
                editable=True,
            ),
        )
    if not principal.is_platform_super_admin:
        own_scope = next(
            (
                tenant_id
                for tenant_id in grouped
                if tenant_id == principal.tenant_id
            ),
            None,
        )
        if own_scope is None:
            editors.append(
                scope_editor(
                    action=action,
                    csrf=csrf,
                    rows=[],
                    tenant_id=principal.tenant_id,
                    options=options,
                    editable=True,
                )
            )
    tenant_values = sorted(
        set(tenant_options) | {value for value in grouped if value}
    )
    datalist_id = "scope-tenant-options-" + action.strip("/").replace("/", "-")
    if principal.is_platform_super_admin:
        editors.append(
            details(
                summary("Add tenant override"),
                form(
                    input_(type="hidden", name="csrf_token", value=csrf),
                    input_(type="hidden", name="operation", value="save"),
                    label(
                        span("Tenant ID"),
                        input_(
                            name="tenant_id",
                            list=datalist_id,
                            placeholder="tenant-id",
                            required=True,
                        ),
                    ),
                    permission_matrix(options=options, selected=()),
                    button(
                        "Create override",
                        type="submit",
                        class_="btn btn-primary btn-small",
                    ),
                    action=action,
                    method="post",
                    class_="new-scope-form",
                ),
                class_="scope-editor new-scope-editor",
            )
        )
    return div(
        *editors,
        datalist(
            *[option(value=value) for value in tenant_values],
            id=datalist_id,
        ),
        class_="scope-matrix",
    )


def scope_summary(rows: list[Any]) -> Any:
    """显示默认绑定和租户覆盖数量。"""
    global_count = sum(1 for row in rows if row.tenant_id is None)
    tenant_scopes = {row.tenant_id for row in rows if row.tenant_id}
    return div(
        span(
            "Default set" if global_count else "No default",
            class_="status-badge",
        ),
        span(f"{len(tenant_scopes)} tenant overrides", class_="chip"),
        span(f"{len(rows)} grants", class_="chip"),
        class_="scope-badges",
    )
