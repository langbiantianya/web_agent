"""Agent 配置管理页。"""

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
    textarea,
    tr,
)

from app.models.admin_control import AdminPageDefinition
from services.admin_web.accounts import AdminPrincipal
from services.admin_web.views.components import (
    _csrf,
    _management_page,
    _toggle_and_delete,
)


def agents_page(
    *,
    principal: AdminPrincipal,
    csrf: str,
    pages: Iterable[AdminPageDefinition],
    rows: list[Any],
    theme: str,
    color_mode: str,
    density: str,
    can_write: bool,
    flash: str | None = None,
) -> Any:
    """渲染 Agent 配置管理页。"""
    table_rows = []
    for row in rows:
        actions = _toggle_and_delete(
            csrf=csrf,
            prefix=f"/admin/agents/{row.code}",
            enabled=bool(row.enabled),
            can_write=can_write,
        )
        editor = (
            details(
                summary("Edit configuration"),
                form(
                    _csrf(csrf),
                    label(span("Name"), input_(name="name", value=row.name, required=True)),
                    label(span("Model"), input_(name="model", value=row.model, required=True)),
                    label(span("Base URL"), input_(name="base_url", value=row.base_url or "")),
                    label(
                        span("Replacement API key"),
                        input_(
                            name="api_key",
                            type="password",
                            autocomplete="new-password",
                            placeholder="Leave blank to keep current key",
                        ),
                    ),
                    label(
                        span("Tools (comma separated)"),
                        input_(name="tools", value=", ".join(row.tools or [])),
                    ),
                    label(
                        span("Skills (comma separated)"),
                        input_(name="skills", value=", ".join(row.skills or [])),
                    ),
                    label(
                        span("System prompt"),
                        textarea(row.system_prompt, name="system_prompt", rows=10, required=True),
                    ),
                    button("Save changes", type="submit", class_="btn btn-primary"),
                    action=f"/admin/agents/{row.code}/update",
                    method="post",
                    class_="stack",
                ),
            )
            if can_write
            else span("")
        )
        table_rows.append(
            tr(
                td(row.name, small(row.code, class_="block-muted")),
                td(row.model),
                td("Enabled" if row.enabled else "Disabled"),
                td(f"v{row.version}"),
                td(actions, editor),
            )
        )
    create_form = (
        form(
            _csrf(csrf),
            label(span("Code"), input_(name="code", required=True)),
            label(span("Name"), input_(name="name", required=True)),
            label(span("Model"), input_(name="model", required=True)),
            label(span("Base URL"), input_(name="base_url")),
            label(
                span("API key"),
                input_(name="api_key", type="password", autocomplete="new-password"),
            ),
            label(span("Tools (comma separated)"), input_(name="tools")),
            label(span("Skills (comma separated)"), input_(name="skills")),
            label(
                span("System prompt"),
                textarea(name="system_prompt", rows=8, required=True),
            ),
            label(
                span("Enabled"),
                select(option("Yes", value="1"), option("No", value="0"), name="enabled"),
            ),
            button("Create agent", type="submit", class_="btn btn-primary"),
            action="/admin/agents",
            method="post",
            class_="form-grid",
        )
        if can_write
        else None
    )
    return _management_page(
        title_text="Agents",
        lead="Prompt, model, tool, and skill bindings are managed inside the isolated admin service.",
        principal=principal,
        csrf=csrf,
        pages=pages,
        theme=theme,
        color_mode=color_mode,
        density=density,
        create_form=create_form,
        headers=["Agent", "Model", "State", "Version", "Actions"],
        rows=table_rows,
        flash=flash,
    )
