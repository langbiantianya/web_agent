"""角色与权限管理页面。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ludic.html import (
    button,
    details,
    div,
    form,
    h1,
    h2,
    input as input_,
    label,
    p,
    section,
    span,
    summary,
)

from app.models.admin_control import AdminPageDefinition
from services.admin_web.access_policy import (
    KNOWN_PERMISSIONS,
    RoleSummary,
    role_summary_rows,
)
from services.admin_web.accounts import AdminPrincipal
from services.admin_web.listing import ListParams
from services.admin_web.views.list_controls import list_controls
from services.admin_web.views.role_cards import (
    domain_datalist,
    permission_matrix,
    role_card,
)
from services.admin_web.views.shell import shell


def roles_page(
    *,
    principal: AdminPrincipal,
    csrf: str,
    pages: Iterable[AdminPageDefinition],
    rules: list[Any],
    roles: list[RoleSummary] | None = None,
    all_roles: list[RoleSummary] | None = None,
    permission_options: list[tuple[str, str]] | None = None,
    theme: str,
    color_mode: str,
    density: str,
    list_params: ListParams | None = None,
    list_total: int = 0,
    flash: str | None = None,
    flash_kind: str = "info",
) -> Any:
    """展示并管理角色权限、继承和删除保护。"""
    summaries = roles if roles is not None else role_summary_rows(rules)
    complete = all_roles if all_roles is not None else summaries
    options = permission_options or sorted(KNOWN_PERMISSIONS)
    domain_value = (
        "tenant:*" if principal.is_platform_super_admin
        else f"tenant:{principal.tenant_id or ''}"
    )
    domain_field = (
        input_(
            name="domain",
            value=domain_value,
            list="role-domains",
            required=True,
            aria_label="Role domain",
        )
        if principal.is_platform_super_admin
        else input_(type="hidden", name="domain", value=domain_value)
    )
    return shell(
        title_text="Roles & permissions",
        principal=principal,
        csrf=csrf,
        theme=theme,
        color_mode=color_mode,
        density=density,
        pages=pages,
        flash=flash,
        flash_kind=flash_kind,
        content=div(
            section(
                div(
                    div(
                        p("ACCESS CONTROL", class_="eyebrow"),
                        h1("Roles & permissions"),
                        p(
                            "Manage reusable Casbin roles, permission matrices and "
                            "inheritance. Platform super admins bypass these rules.",
                            class_="lead",
                        ),
                    ),
                    div(
                        span(f"{len(complete)} roles", class_="metric-pill"),
                        span(
                            f"{sum(len(role.permissions) for role in complete)} "
                            "permission grants",
                            class_="metric-pill",
                        ),
                        span(
                            f"{len({role.domain for role in complete})} scopes",
                            class_="metric-pill",
                        ),
                        class_="access-summary",
                    ),
                    class_="access-hero",
                ),
                details(
                    summary("Create role"),
                    form(
                        input_(type="hidden", name="csrf_token", value=csrf),
                        label(span("Role code"), input_(
                            name="role_code",
                            placeholder="release_manager",
                            pattern="[a-z][a-z0-9_.-]{1,63}",
                            required=True,
                        )),
                        label(span("Domain"), domain_field),
                        permission_matrix(options, ()),
                        button(
                            "Create role",
                            type="submit",
                            class_="btn btn-primary",
                        ),
                        action="/admin/access/roles",
                        method="post",
                        class_="role-create-form",
                    ),
                    class_="role-create-details",
                ),
                domain_datalist(
                    "role-domains",
                    {role.domain for role in complete}
                    | {"tenant:*", "*", domain_value},
                ),
                class_="surface access-hero-surface",
            ),
            *(
                [
                    section(
                        list_controls(
                            action="/admin/access/roles",
                            params=list_params or ListParams(),
                            total=list_total,
                            search_label="Search roles",
                            search_placeholder="Role, permission or scope",
                        ),
                        class_="surface list-controls",
                    )
                ]
                if list_params is not None
                else []
            ),
            div(
                *[
                    role_card(
                        role=role,
                        roles=complete,
                        permission_options=options,
                        principal=principal,
                        csrf=csrf,
                    )
                    for role in summaries
                ]
                or [
                    section(
                        h2("No roles found"),
                        p(
                            "Create a role or adjust the current search.",
                            class_="block-muted",
                        ),
                        class_="surface empty-state",
                    )
                ],
                class_="role-list stack",
            ),
            class_="stack access-page",
        ),
    )
