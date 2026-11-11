"""Workflow 编排管理页：列表页与画布编排页（后者自己拼外壳）。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ludic.html import (
    a,
    button,
    div,
    form,
    h1,
    h2,
    h3,
    input as input_,
    label,
    li,
    option,
    pre,
    section,
    select,
    small,
    span,
    td,
    textarea,
    tr,
    ul,
)

from app.models.admin_control import AdminPageDefinition
from services.admin_web.accounts import AdminPrincipal
from services.admin_web.views.components import (
    _csrf,
    _management_page,
    _toggle_and_delete,
)
from services.admin_web.views.confirm import undo_panel
from services.admin_web.views.list_controls import list_controls
from services.admin_web.listing import ListParams
from services.admin_web.views.shell import shell
from services.admin_web.views.workflow_editor import workflow_editor_page


def workflows_page(
    *,
    principal: AdminPrincipal,
    csrf: str,
    pages: Iterable[AdminPageDefinition],
    rows: list[Any],
    agents: list[Any],
    theme: str,
    color_mode: str,
    density: str,
    can_write: bool,
    flash: str | None = None,
    flash_kind: str = "info",
    undo_id: int | None = None,
    list_params: ListParams | None = None,
    list_total: int = 0,
) -> Any:
    """渲染 Workflow 编排管理页。"""
    table_rows = []
    for row in rows:
        actions = _toggle_and_delete(
            csrf=csrf,
            prefix=f"/admin/workflows/{row.code}",
            enabled=bool(row.enabled),
            can_write=can_write,
            label=f"Workflow {row.name}",
            delete_title=f"Delete Workflow {row.name}",
            delete_summary=(
                f"Workflow {row.name} ({row.code}) and its graph definition "
                "will be permanently removed."
            ),
            delete_impacts=(
                "Runs already started keep their in-memory definition snapshot.",
                "Future runs of this workflow will fail with 'not found'.",
            ),
        )
        editor = a(
            "Open editor" if can_write else "View graph",
            href=f"/admin/workflows/{row.code}",
            class_="btn btn-secondary btn-small",
        )
        table_rows.append(
            tr(
                td(row.name, small(row.code, class_="block-muted")),
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
            label(span("Description"), input_(name="description")),
            label(
                span("Initial agent"),
                select(
                    *(
                        option(f"{a.name} ({a.code})", value=a.code)
                        for a in agents
                    ),
                    name="agent_code",
                    required=True,
                ),
            ),
            label(
                span("Enabled"),
                select(option("Yes", value="1"), option("No", value="0"), name="enabled"),
            ),
            button("Create workflow", type="submit", class_="btn btn-primary"),
            action="/admin/workflows",
            method="post",
            class_="form-grid",
        )
        if can_write
        else None
    )
    return _management_page(
        title_text="Workflows",
        lead="Manage graph definitions directly against the shared workflow repository.",
        principal=principal,
        csrf=csrf,
        pages=pages,
        theme=theme,
        color_mode=color_mode,
        density=density,
        create_form=create_form,
        controls=(
            list_controls(
                action="/admin/workflows",
                params=list_params or ListParams(),
                total=list_total,
                sort_options=("name", "code"),
            )
            if list_params is not None
            else None
        ),
        headers=["Workflow", "State", "Version", "Actions"],
        rows=table_rows,
        flash=flash,
        flash_kind=flash_kind,
        undo=(
            undo_panel(
                csrf=csrf,
                undo_id=undo_id,
                resource_label="Workflow",
            )
            if undo_id is not None
            else None
        ),
    )
