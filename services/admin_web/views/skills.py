"""Skill 定义与发布状态管理页。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ludic.html import button, small, span, td, tr

from app.models.admin_control import AdminPageDefinition
from services.admin_web.accounts import AdminPrincipal
from services.admin_web.views.components import _csrf, _management_page
from services.admin_web.views.confirm import delete_trigger
from services.admin_web.views.edit_panel import edit_link, edit_panel
from services.admin_web.views.list_controls import list_controls
from services.admin_web.listing import ListParams
from services.admin_web.views.fields import Field, render_form

# 创建表单与编辑表单**共用**同一份字段描述：差异用 modes / edit_rows /
# edit_required 表达，不再把字段抄两遍。
#
# 字段顺序是渲染结构的一部分（渲染快照比的是元素树，children 顺序变了
# 就是差异），所以创建态与编辑态的顺序各自写死。
SKILL_FIELDS = (
    Field("description", "Description", required=True),
    Field(
        "allowed_tools",
        "Suggested tools (comma separated)",
        edit_label="Suggested tools",
    ),
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
    used_by: dict[str, list[str]] | None = None,
    theme: str,
    color_mode: str,
    density: str,
    can_write: bool,
    flash: str | None = None,
    create_errors: dict[str, str] | None = None,
    create_values: dict[str, str] | None = None,
    list_params: ListParams | None = None,
    list_total: int = 0,
    editing_row: Any | None = None,
    edit_errors: dict[str, dict[str, str]] | None = None,
    edit_values: dict[str, dict[str, str]] | None = None,
) -> Any:
    """渲染 Skill 定义与发布状态管理页。"""
    # 校验失败时把已填的值与字段级错误带回创建表单（UX-03）。
    create_errors = dict(create_errors or {})
    create_values = dict(create_values or {})
    per_row_errors = dict(edit_errors or {})
    per_row_values = dict(edit_values or {})
    bindings = used_by or {}
    table_rows = []
    panel = None

    def _editor_form(row: Any) -> Any:
        """构造某个 Skill 的编辑表单（面板内使用）。"""
        return render_form(
            SKILL_FIELDS,
            mode="edit",
            values={
                "description": row.description or "",
                "allowed_tools": getattr(row, "allowed_tools", "") or "",
                "status": row.status,
                "body": row.body or "",
                **per_row_values.get(row.code, {}),
            },
            csrf=csrf,
            action=f"/admin/skills/{row.code}/update",
            submit_label="Save skill",
            layout="stack",
            errors=per_row_errors.get(row.code),
            form_id=f"skill-edit-{row.code}",
            error_summary=(
                "The skill could not be saved. Fix the highlighted fields."
                if per_row_errors.get(row.code)
                else ""
            ),
        )

    for row in rows:
        agent_codes = bindings.get(row.code, [])
        in_use_reason = (
            f"Used by agents: {', '.join(agent_codes)}"
            if agent_codes
            else ""
        )
        # 编辑表单放进独立面板（UX-09）：同一时刻只展开一个对象，
        # 并且可以用 ``?resource=<code>`` 深链接直接打开。
        editor_form = (
            _editor_form(row)
            if can_write and row.status != "archived"
            else None
        )
        if editing_row is not None and editing_row.code == row.code \
                and editor_form is not None:
            panel = edit_panel(
                resource=row.code,
                title=f"Edit skill {row.name}",
                form=editor_form,
                close_url="/admin/skills",
            )
            editor = span("")
        else:
            editor = (
                edit_link(
                    action="/admin/skills",
                    resource=row.code,
                    label="Edit skill",
                )
                if editor_form is not None
                else span("")
            )
        archive = (
            delete_trigger(
                csrf=csrf,
                action=f"/admin/skills/{row.code}/archive",
                label=f"Skill {row.name}",
                title=f"Archive Skill {row.name}",
                summary=(
                    f"Skill {row.name} ({row.code}) will be archived and "
                    "removed from active use."
                ),
                impacts=(
                    f"Used by agents: {', '.join(agent_codes)}",
                    "Skills keep their resources and revisions; restore is "
                    "available from the API.",
                ) if agent_codes else (
                    "Skills keep their resources and revisions; restore is "
                    "available from the API.",
                ),
                disabled_reason=in_use_reason,
                button_label="Archive",
                accessible_label=f"Archive Skill {row.name}",
            )
            if can_write and row.status != "archived"
            else span("")
        )
        table_rows.append(
            tr(
                td(row.name, small(row.code, class_="block-muted")),
                td(row.status),
                td("Enabled" if row.enabled else "Disabled"),
                td(", ".join(agent_codes) if agent_codes else "None"),
                td(f"v{row.version}"),
                td(
                    archive,
                    small(in_use_reason, class_="warning-text")
                    if in_use_reason
                    else span(""),
                    editor,
                ),
            )
        )
    if panel is None and editing_row is not None and can_write:
        # 深链接指向的对象不在当前页（筛选或翻页导致）时，面板照样打开。
        panel = edit_panel(
            resource=editing_row.code,
            title=f"Edit skill {editing_row.name}",
            form=_editor_form(editing_row),
            close_url="/admin/skills",
        )
    create_form = (
        render_form(
            SKILL_CREATE_FIELDS,
            mode="create",
            values=create_values,
            csrf=csrf,
            action="/admin/skills",
            submit_label="Create skill",
            layout="grid",
            errors=create_errors,
            form_id="skill-create",
            error_summary=(
                "The skill could not be saved. Fix the highlighted fields."
                if create_errors
                else ""
            ),
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
        panel=panel,
        controls=(
            list_controls(
                action="/admin/skills",
                params=list_params or ListParams(),
                total=list_total,
                sort_options=("name", "code"),
            )
            if list_params is not None
            else None
        ),
        headers=[
            "Skill",
            "Status",
            "Injection",
            "Used by agents",
            "Version",
            "Actions",
        ],
        rows=table_rows,
        flash=flash,
    )
