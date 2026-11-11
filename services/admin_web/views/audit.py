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
from services.admin_web.views.fields import _Option
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
    flash_kind: str = "info",
    run_id: str = "",
    tool_name: str = "",
    source: str = "",
    ok: str = "",
) -> Any:
    """渲染工具审计摘要和筛选器。

    筛选器的当前值要**回显**：URL 里带着 ``?source=builtin`` 重新打开页面时，
    下拉框必须停在「内置」上，否则管理员看到的筛选条件与实际生效的条件
    不一致（UX-36）。
    """
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
        label(
            span("Run ID"),
            input_(name="run_id", type="number", min="1", value=run_id),
        ),
        label(span("Tool name"), input_(name="tool_name", value=tool_name)),
        label(
            span("Source"),
            select(
                _Option("Any", value="", selected=not source),
                option("Builtin", value="builtin", selected=source == "builtin"),
                option("Skill", value="skill", selected=source == "skill"),
                option("MCP", value="mcp", selected=source == "mcp"),
                name="source",
            ),
        ),
        label(
            span("Outcome"),
            select(
                _Option("Any", value="", selected=not ok),
                option("Success", value="true", selected=ok == "true"),
                option("Failed", value="false", selected=ok == "false"),
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
        flash_kind=flash_kind,
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
