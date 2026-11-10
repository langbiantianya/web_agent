"""组件权限矩阵页面。"""

from __future__ import annotations

from collections.abc import Iterable
from itertools import groupby
from typing import Any

from ludic.html import (
    div,
    h1,
    h2,
    p,
    section,
    small,
    span,
)

from app.models.admin_control import (
    AdminComponentDefinition,
    AdminComponentPermission,
    AdminPageDefinition,
)
from services.admin_web.access_policy import KNOWN_PERMISSIONS
from services.admin_web.accounts import AdminPrincipal
from services.admin_web.views.access_ui import (
    recommended_permissions,
    scope_summary,
    scoped_editors,
)
from services.admin_web.views.shell import shell


def access_components_page(
    *,
    principal: AdminPrincipal,
    csrf: str,
    pages: Iterable[AdminPageDefinition],
    components: list[AdminComponentDefinition],
    bindings: dict[tuple[str, str], list[AdminComponentPermission]],
    recommended: dict[tuple[str, str], list[tuple[str, str]]] | None = None,
    permission_options: list[tuple[str, str]] | None = None,
    tenant_options: list[str] | None = None,
    page_titles: dict[str, str] | None = None,
    theme: str,
    color_mode: str,
    density: str,
    flash: str | None = None,
    flash_kind: str = "info",
) -> Any:
    """渲染按页面分组的组件权限矩阵。"""
    recommended_by_component = recommended or {}
    options = permission_options or sorted(KNOWN_PERMISSIONS)
    titles = page_titles or {}
    groups = []
    ordered = sorted(
        components,
        key=lambda row: (
            row.page_code,
            getattr(row, "sort_order", 0),
            row.code,
        ),
    )
    for page_code, values in groupby(ordered, key=lambda row: row.page_code):
        page_components = list(values)
        cards = []
        for component in page_components:
            key = (component.page_code, component.code)
            scoped_rows = bindings.get(key, [])
            cards.append(
                section(
                    div(
                        div(
                            h2(component.label),
                            small(
                                f"{component.page_code}/{component.code}",
                                class_="block-muted",
                            ),
                        ),
                        span(component.kind, class_="status-badge"),
                        class_="component-card-head",
                    ),
                    scope_summary(scoped_rows),
                    recommended_permissions(
                        recommended_by_component.get(key, [])
                    ),
                    scoped_editors(
                        rows=scoped_rows,
                        action=(
                            f"/admin/access/components/{component.page_code}/"
                            f"{component.code}/permissions"
                        ),
                        csrf=csrf,
                        options=options,
                        principal=principal,
                        tenant_options=tenant_options or [],
                    ),
                    class_="component-card",
                )
            )
        groups.append(
            section(
                div(
                    h2(titles.get(page_code, page_code)),
                    small(page_code, class_="block-muted"),
                    span(
                        f"{len(page_components)} components",
                        class_="metric-pill",
                    ),
                    class_="component-page-head",
                ),
                div(*cards, class_="component-grid"),
                class_="surface component-page-group",
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
        flash_kind=flash_kind,
        content=div(
            section(
                div(
                    div(
                        p("ACCESS CONTROL", class_="eyebrow"),
                        h1("Component permissions"),
                        p(
                            "Control tables, editors, actions and forms separately "
                            "from page navigation. Hidden components never replace "
                            "API authorization.",
                            class_="lead",
                        ),
                    ),
                    div(
                        span(
                            f"{len(components)} components",
                            class_="metric-pill",
                        ),
                        span(
                            f"{sum(len(rows) for rows in bindings.values())} grants",
                            class_="metric-pill",
                        ),
                        span(
                            f"{len({component.page_code for component in components})} pages",
                            class_="metric-pill",
                        ),
                        class_="access-summary",
                    ),
                    class_="access-hero",
                ),
                class_="surface",
            ),
            div(*groups, class_="stack component-page-list"),
            class_="stack access-page",
        ),
    )
