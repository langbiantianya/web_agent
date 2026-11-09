"""LLM 接口优先级与故障切换配置页。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ludic.html import (
    button,
    details,
    form,
    input as input_,
    label,
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


def llm_page(
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
    """渲染 LLM 接口优先级与故障切换配置页。"""
    table_rows = []
    for row in rows:
        actions = _toggle_and_delete(
            csrf=csrf,
            prefix=f"/admin/llm/{row.code}",
            enabled=bool(row.enabled),
            can_write=can_write,
        )
        edit = (
            details(
                summary("Edit endpoint"),
                form(
                    _csrf(csrf),
                    label(span("Name"), input_(name="name", value=row.name or row.code)),
                    label(span("Base URL"), input_(name="base_url", value=row.base_url)),
                    label(
                        span("Priority"),
                        input_(name="priority", type="number", value=row.priority),
                    ),
                    label(
                        span("Replacement API key"),
                        input_(
                            name="api_key",
                            type="password",
                            autocomplete="new-password",
                            placeholder="Leave blank to keep current key",
                        ),
                    ),
                    button("Save endpoint", type="submit", class_="btn btn-primary"),
                    action=f"/admin/llm/{row.code}/update",
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
                td(row.base_url),
                td(str(row.priority)),
                td("Enabled" if row.enabled else "Disabled"),
                td(actions, edit),
            )
        )
    create_form = (
        form(
            _csrf(csrf),
            label(span("Code"), input_(name="code", required=True)),
            label(span("Name"), input_(name="name")),
            label(span("Base URL"), input_(name="base_url", required=True)),
            label(
                span("API key"),
                input_(
                    name="api_key",
                    type="password",
                    autocomplete="new-password",
                ),
            ),
            label(
                span("Priority"),
                input_(name="priority", type="number", value="100", min="0"),
            ),
            button("Create endpoint", type="submit", class_="btn btn-primary"),
            action="/admin/llm",
            method="post",
            class_="form-grid",
        )
        if can_write
        else None
    )
    return _management_page(
        title_text="LLM endpoints",
        lead="Lower priority numbers are attempted first. API keys remain write-only.",
        principal=principal,
        csrf=csrf,
        pages=pages,
        theme=theme,
        color_mode=color_mode,
        density=density,
        create_form=create_form,
        headers=["Endpoint", "Base URL", "Priority", "State", "Actions"],
        rows=table_rows,
        flash=flash,
    )
