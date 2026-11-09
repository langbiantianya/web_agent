"""工具审计摘要与筛选器页面。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ludic.html import (
    button,
    div,
    form,
    h1,
    h2,
    input as input_,
    label,
    option,
    p,
    section,
    select,
    small,
    span,
    table,
    tbody,
    td,
    th,
    thead,
    tr,
)

from app.models.admin_control import AdminPageDefinition
from services.admin_web.accounts import AdminPrincipal
from services.admin_web.views.shell import shell


def audit_page(
    *,
    principal: AdminPrincipal,
    csrf: str,
    pages: Iterable[AdminPageDefinition],
    rows: list[Any],
    total: int,
    theme: str,
    color_mode: str,
    density: str,
    flash: str | None = None,
) -> Any:
    """渲染工具审计摘要和筛选器。"""
    table_rows = [
        tr(
            td(str(row.id)),
            td(str(row.run_id)),
            td(row.tool_name, small(row.node_id, class_="block-muted")),
            td(row.source),
            td("Success" if row.ok else (row.failure_type or "Failed")),
            td(f"{row.duration_ms} ms"),
            td(str(row.created_at)),
        )
        for row in rows
    ]
    filters = form(
        label(span("Run ID"), input_(name="run_id", type="number", min="1")),
        label(span("Tool name"), input_(name="tool_name")),
        label(
            span("Source"),
            select(
                option("Any", value=""),
                option("Builtin", value="builtin"),
                option("Skill", value="skill"),
                option("MCP", value="mcp"),
                name="source",
            ),
        ),
        label(
            span("Outcome"),
            select(
                option("Any", value=""),
                option("Success", value="true"),
                option("Failed", value="false"),
                name="ok",
            ),
        ),
        button("Apply filters", type="submit", class_="btn btn-primary"),
        action="/admin/audit",
        method="get",
        class_="form-grid",
    )
    return shell(
        title_text="Tool audit",
        principal=principal,
        csrf=csrf,
        theme=theme,
        color_mode=color_mode,
        density=density,
        pages=pages,
        flash=flash,
        content=div(
            section(
                h1("Tool audit"),
                p(
                    f"{total} redacted tool invocation records are visible to this tenant.",
                    class_="lead",
                ),
                filters,
                class_="surface",
            ),
            section(
                h2("Recent invocations"),
                div(
                    table(
                        thead(
                            tr(
                                th("ID"),
                                th("Run"),
                                th("Tool"),
                                th("Source"),
                                th("Outcome"),
                                th("Duration"),
                                th("Created"),
                            )
                        ),
                        tbody(
                            *(table_rows or [tr(td("No audit records found."))])
                        ),
                        class_="data-table",
                    ),
                    class_="table-scroll",
                ),
                class_="surface",
            ),
            class_="stack",
        ),
    )
