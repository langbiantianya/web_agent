"""Skill 定义与发布状态管理页。"""

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
from services.admin_web.views.components import _csrf, _management_page


def skills_page(
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
    """渲染 Skill 定义与发布状态管理页。"""
    table_rows = []
    for row in rows:
        editor = (
            details(
                summary("Edit skill"),
                form(
                    _csrf(csrf),
                    label(
                        span("Description"),
                        input_(name="description", value=row.description or "", required=True),
                    ),
                    label(
                        span("Status"),
                        select(
                            option(
                                "Published",
                                value="published",
                                selected=row.status == "published",
                            ),
                            option(
                                "Draft",
                                value="draft",
                                selected=row.status == "draft",
                            ),
                            name="status",
                        ),
                    ),
                    label(
                        span("Skill body"),
                        textarea(row.body or "", name="body", rows=14, required=True),
                    ),
                    button("Save skill", type="submit", class_="btn btn-primary"),
                    action=f"/admin/skills/{row.code}/update",
                    method="post",
                    class_="stack",
                ),
            )
            if can_write and row.status != "archived"
            else span("")
        )
        archive = (
            form(
                _csrf(csrf),
                button("Archive", type="submit", class_="btn btn-danger btn-small"),
                action=f"/admin/skills/{row.code}/archive",
                method="post",
                class_="inline-form",
            )
            if can_write and row.status != "archived"
            else span("")
        )
        table_rows.append(
            tr(
                td(row.name, small(row.code, class_="block-muted")),
                td(row.status),
                td("Enabled" if row.enabled else "Disabled"),
                td(f"v{row.version}"),
                td(archive, editor),
            )
        )
    create_form = (
        form(
            _csrf(csrf),
            label(span("Code"), input_(name="code", required=True)),
            label(span("Description"), input_(name="description", required=True)),
            label(
                span("Status"),
                select(
                    option("Published", value="published"),
                    option("Draft", value="draft"),
                    name="status",
                ),
            ),
            label(span("Skill body"), textarea(name="body", rows=10)),
            label(
                span("Enabled"),
                select(option("Yes", value="1"), option("No", value="0"), name="enabled"),
            ),
            button("Create skill", type="submit", class_="btn btn-primary"),
            action="/admin/skills",
            method="post",
            class_="form-grid",
        )
        if can_write
        else None
    )
    return _management_page(
        title_text="Skills",
        lead="Review publication state and edit instruction packages without crossing the legacy client API.",
        principal=principal,
        csrf=csrf,
        pages=pages,
        theme=theme,
        color_mode=color_mode,
        density=density,
        create_form=create_form,
        headers=["Skill", "Status", "Injection", "Version", "Actions"],
        rows=table_rows,
        flash=flash,
    )
