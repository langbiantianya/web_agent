"""角色权限矩阵与单个角色卡片。"""

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
    h2,
    input as input_,
    label,
    legend,
    option,
    p,
    section,
    span,
    summary,
)

from services.admin_web.access_policy import (
    PERMISSION_DESCRIPTIONS,
    RoleSummary,
    permission_groups,
    permission_id,
)
from services.admin_web.accounts import AdminPrincipal
from services.admin_web.views.confirm import delete_trigger


def permission_matrix(
    permissions: Iterable[tuple[str, str]],
    selected: Iterable[tuple[str, str]],
) -> Any:
    """渲染按对象分组的权限复选框。"""
    current = set(selected)
    groups = []
    for object_name, actions in permission_groups(set(permissions)):
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
                                value=permission_id(item),
                                checked=item in current,
                            ),
                            span(item[1]),
                            class_="permission-choice",
                        )
                        for item in actions
                    ],
                    class_="permission-choice-row",
                ),
                class_="permission-group",
            )
        )
    return div(*groups, class_="permission-matrix")


def role_card(
    *,
    role: RoleSummary,
    roles: list[RoleSummary],
    permission_options: list[tuple[str, str]],
    principal: AdminPrincipal,
    csrf: str,
) -> Any:
    """渲染一个角色的权限、继承和删除操作。"""
    permission_count = len(role.permissions)
    parent_options = [
        candidate
        for candidate in roles
        if candidate.domain == role.domain and candidate.code != role.code
    ]
    protected_reason = ""
    if role.builtin:
        protected_reason = "Built-in role required by authentication defaults."
    elif role.member_count:
        protected_reason = f"Assigned to {role.member_count} administrator(s)."
    elif role.mapping_count:
        protected_reason = f"Referenced by {role.mapping_count} OAuth mapping(s)."
    elif role.child_count:
        protected_reason = f"Inherited by {role.child_count} child role(s)."
    return section(
        div(
            div(
                h2(role.code),
                p(
                    "Built-in role" if role.builtin else "Custom role",
                    class_="block-muted",
                ),
            ),
            div(
                span(role.domain, class_="status-badge"),
                span(f"{permission_count} permissions", class_="chip"),
                span(f"{role.member_count} members", class_="chip"),
                class_="role-badges",
            ),
            class_="role-card-head",
        ),
        details(
            summary(
                "Edit permissions",
                span(
                    ", ".join(permission_id(item) for item in role.permissions)
                    or "No permissions",
                    class_="role-summary-permissions",
                ),
            ),
            form(
                input_(type="hidden", name="csrf_token", value=csrf),
                input_(type="hidden", name="domain", value=role.domain),
                permission_matrix(permission_options, role.permissions),
                button(
                    "Save permissions",
                    type="submit",
                    class_="btn btn-primary",
                ),
                action=f"/admin/access/roles/{role.code}/permissions",
                method="post",
                class_="role-policy-form",
            ),
            class_="role-policy-details",
        ),
        details(
            summary(
                "Role inheritance",
                span(
                    ", ".join(role.parents) or "No parent roles",
                    class_="role-summary-permissions",
                ),
            ),
            form(
                input_(type="hidden", name="csrf_token", value=csrf),
                input_(type="hidden", name="domain", value=role.domain),
                div(
                    *[
                        label(
                            input_(
                                type="checkbox",
                                name="parents",
                                value=candidate.code,
                                checked=candidate.code in role.parents,
                            ),
                            span(candidate.code),
                            class_="permission-choice",
                        )
                        for candidate in parent_options
                    ]
                    or [
                        p(
                            "No other role exists in this domain yet.",
                            class_="block-muted",
                        )
                    ],
                    class_="permission-choice-row",
                ),
                button(
                    "Save inheritance",
                    type="submit",
                    class_="btn btn-secondary",
                ),
                action=f"/admin/access/roles/{role.code}/inheritance",
                method="post",
                class_="role-inheritance-form",
            ),
        ),
        delete_trigger(
            csrf=csrf,
            action=f"/admin/access/roles/{role.code}/delete",
            label=f"role {role.code}",
            title=f"Delete role {role.code}",
            summary="Removing a role permanently removes its policy rules.",
            impacts=(
                f"Domain: {role.domain}",
                f"Permissions: {permission_count}",
                "Roles with members, mappings or inheritance cannot be deleted.",
            ),
            disabled_reason=protected_reason,
        ),
        class_="role-card",
    )


def domain_datalist(identifier: str, values: Iterable[str]) -> Any:
    """渲染域名或角色代码建议列表。"""
    return datalist(
        *[option(value=value) for value in sorted(set(values))],
        id=identifier,
    )
