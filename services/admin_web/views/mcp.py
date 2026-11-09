"""MCP 服务器管理页。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ludic.html import (
    button,
    details,
    form,
    input as input_,
    label,
    option,
    select,
    small,
    span,
    summary,
    td,
    tr,
)

from app.models.admin_control import AdminPageDefinition
from services.admin_web.accounts import AdminPrincipal
from services.admin_web.views.components import (
    _csrf,
    _management_page,
    _toggle_and_delete,
)


def mcp_page(
    *,
    principal: AdminPrincipal,
    csrf: str,
    pages: Iterable[AdminPageDefinition],
    rows: list[Any],
    theme: str,
    color_mode: str,
    density: str,
    can_write: bool,
    can_refresh: bool,
    flash: str | None = None,
) -> Any:
    """渲染 MCP 服务器管理页。"""
    table_rows = []
    for row in rows:
        actions = _toggle_and_delete(
            csrf=csrf,
            prefix=f"/admin/mcp/{row.code}",
            enabled=bool(row.enabled),
            can_write=can_write,
        )
        refresh = (
            form(
                _csrf(csrf),
                button("Refresh tools", type="submit", class_="btn btn-secondary btn-small"),
                action=f"/admin/mcp/{row.code}/refresh",
                method="post",
                class_="inline-form",
            )
            if can_refresh
            else span("")
        )
        edit = (
            details(
                summary("Edit connection"),
                form(
                    _csrf(csrf),
                    label(span("Name"), input_(name="name", value=row.name or row.code)),
                    label(
                        span("Timeout seconds"),
                        input_(name="timeout_sec", type="number", value=row.timeout_sec),
                    ),
                    label(
                        span("Command"),
                        input_(name="command", value=row.command or ""),
                    ),
                    label(span("URL"), input_(name="url", value=row.url or "")),
                    button("Save connection", type="submit", class_="btn btn-primary"),
                    action=f"/admin/mcp/{row.code}/update",
                    method="post",
                    class_="stack",
                ),
            )
            if can_write
            else span("")
        )
        table_rows.append(
            tr(
                td(row.name or row.code, small(row.code, class_="block-muted")),
                td(row.transport),
                td("Enabled" if row.enabled else "Disabled"),
                td(f"v{row.version}"),
                td(refresh, actions, edit),
            )
        )
    create_form = (
        form(
            _csrf(csrf),
            label(span("Code"), input_(name="code", required=True)),
            label(span("Name"), input_(name="name")),
            label(
                span("Transport"),
                select(
                    option("HTTP", value="http"),
                    option("stdio", value="stdio"),
                    name="transport",
                ),
            ),
            label(span("HTTP URL"), input_(name="url")),
            label(span("stdio command"), input_(name="command")),
            label(
                span("Timeout seconds"),
                input_(name="timeout_sec", type="number", value="30", min="1", max="600"),
            ),
            button("Register server", type="submit", class_="btn btn-primary"),
            action="/admin/mcp",
            method="post",
            class_="form-grid",
        )
        if can_write
        else None
    )
    return _management_page(
        title_text="MCP servers",
        lead="Connection credentials stay encrypted and are never rendered back into the console.",
        principal=principal,
        csrf=csrf,
        pages=pages,
        theme=theme,
        color_mode=color_mode,
        density=density,
        create_form=create_form,
        headers=["Server", "Transport", "State", "Version", "Actions"],
        rows=table_rows,
        flash=flash,
    )
