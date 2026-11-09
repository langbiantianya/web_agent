"""Skill 定义与发布状态管理页。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ludic.html import button, details, form, small, span, summary, td, tr

from app.models.admin_control import AdminPageDefinition
from services.admin_web.accounts import AdminPrincipal
from services.admin_web.views.components import _csrf, _management_page
from services.admin_web.views.fields import Field, render_form

# 创建表单与编辑表单**共用**同一份字段描述：差异用 modes / edit_rows /
# edit_required 表达，不再把字段抄两遍。
#
# 字段顺序是渲染结构的一部分（渲染快照比的是元素树，children 顺序变了
# 就是差异），所以创建态与编辑态的顺序各自写死。
SKILL_FIELDS = (
    Field("description", "Description", required=True),
    Field(
        "status",
        "Status",
        kind="select",
        options=(("Published", "published"), ("Draft", "draft")),
    ),
    Field(
        "body",
        "Skill body",
        kind="textarea",
        rows=10,
        edit_rows=14,
        edit_required=True,
    ),
)

SKILL_CREATE_FIELDS = (
    Field("code", "Code", required=True, modes=("create",)),
    *SKILL_FIELDS,
    Field(
        "enabled",
        "Enabled",
        kind="select",
        modes=("create",),
        options=(("Yes", "1"), ("No", "0")),
    ),
)


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
                render_form(
                    SKILL_FIELDS,
                    mode="edit",
                    values={
                        "description": row.description or "",
                        "status": row.status,
                        "body": row.body or "",
                    },
                    csrf=csrf,
                    action=f"/admin/skills/{row.code}/update",
                    submit_label="Save skill",
                    layout="stack",
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
        render_form(
            SKILL_CREATE_FIELDS,
            mode="create",
            values={},
            csrf=csrf,
            action="/admin/skills",
            submit_label="Create skill",
            layout="grid",
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
