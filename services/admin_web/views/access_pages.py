"""页面权限矩阵页面。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ludic.html import (
    div,
    h1,
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
    AdminPageDefinition,
    AdminPagePermission,
)
from services.admin_web.access_policy import KNOWN_PERMISSIONS
from services.admin_web.accounts import AdminPrincipal
from services.admin_web.views.access_ui import (
    recommended_permissions,
    scope_summary,
    scoped_editors,
)
from services.admin_web.views.shell import shell


def access_pages_page(
    *,
    principal: AdminPrincipal,
    csrf: str,
    pages: Iterable[AdminPageDefinition],
    definitions: list[AdminPageDefinition],
    bindings: dict[str, list[AdminPagePermission]],
    recommended: dict[str, list[tuple[str, str]]] | None = None,
    permission_options: list[tuple[str, str]] | None = None,
    tenant_options: list[str] | None = None,
    theme: str,
    color_mode: str,
    density: str,
    flash: str | None = None,
    flash_kind: str = "info",
) -> Any:
    """渲染页面级 RBAC 作用域矩阵。"""
    recommended_by_page = recommended or {}
    options = permission_options or sorted(KNOWN_PERMISSIONS)
    rows = []
    for definition in definitions:
        scoped_rows = bindings.get(definition.code, [])
        rows.append(
            tr(
                td(
                    definition.title,
                    small(definition.code, class_="block-muted"),
                    small(
                        f"Access mode: {definition.access_mode}",
                        class_="block-muted",
                    ),
                ),
                td(definition.nav_group),
                td(
                    scope_summary(scoped_rows),
                    recommended_permissions(
                        recommended_by_page.get(definition.code, [])
                    ),
                    scoped_editors(
                        rows=scoped_rows,
                        action=(
                            f"/admin/access/pages/{definition.code}/permissions"
                        ),
                        csrf=csrf,
                        options=options,
                        principal=principal,
                        tenant_options=tenant_options or [],
                    ),
                ),
            )
        )
    scopes = {
        row.binding_scope
        for scoped_rows in bindings.values()
        for row in scoped_rows
    }
    grant_count = sum(len(scoped_rows) for scoped_rows in bindings.values())
    return shell(
        title_text="Page permissions",
        principal=principal,
        csrf=csrf,
        theme=theme,
        color_mode=color_mode,
        density=density,
        pages=pages,
        flash=flash,
        flash_kind=flash_kind,
        content=section(
            div(
                div(
                    p("ACCESS CONTROL", class_="eyebrow"),
                    h1("Page permissions"),
                    p(
                        "Define which RBAC permissions make each registered page "
                        "visible. Tenant overrides replace the global default.",
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
            p(
                "Select permissions from groups below. API routes still perform "
                "their own authorization checks; page visibility is not an API boundary.",
                class_="block-muted access-note",
            ),
            div(
                table(
                    thead(tr(th("Page"), th("Group"), th("Permission policy"))),
                    tbody(*rows),
                    class_="data-table permission-table",
                ),
                class_="table-scroll",
            ),
            class_="surface",
        ),
    )
