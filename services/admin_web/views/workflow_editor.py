"""Workflow 画布编排页。

从 ``workflows.py`` 拆出来：画布页本身要渲染节点面板、属性面板、JSON
视图与保存表单，和列表页挤在一个文件里会突破单文件行数上限。
"""

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
    textarea,
    ul,
)

from app.models.admin_control import AdminPageDefinition
from services.admin_web.accounts import AdminPrincipal
from services.admin_web.views.components import _csrf
from services.admin_web.views.shell import shell


def workflow_editor_page(
    *,
    principal: AdminPrincipal,
    csrf: str,
    pages: Iterable[AdminPageDefinition],
    workflow: Any,
    agents: list[Any],
    theme: str,
    color_mode: str,
    density: str,
    can_write: bool,
    flash: str | None = None,
    flash_kind: str = "info",
) -> Any:
    """渲染 Workflow 画布编排页。"""
    import json as _json

    state = {
        "code": workflow.code,
        "name": workflow.name,
        "description": workflow.description or "",
        "version": int(workflow.version or 1),
        "enabled": bool(workflow.enabled),
        "canWrite": bool(can_write),
        "definition": workflow.definition or {},
        "agents": [
            {"code": a.code, "name": a.name, "enabled": bool(a.enabled)}
            for a in agents
        ],
    }
    state_text = _json.dumps(state, ensure_ascii=False)
    palette = div(
        h2("Node palette"),
        div(
            button(
                "Agent node",
                type="button",
                class_="btn btn-secondary workflow-palette-item",
                attrs={"data-node-type": "agent"},
            ),
            button(
                "Approval node",
                type="button",
                class_="btn btn-secondary workflow-palette-item",
                attrs={"data-node-type": "approval"},
            ),
            class_="workflow-palette",
        ),
        h3("Available agents"),
        ul(
            *(
                li(
                    span(f"{a.name} "),
                    small(a.code, class_="block-muted"),
                    class_="workflow-agent-item",
                )
                for a in agents
            )
        ),
        class_="surface workflow-palette-panel",
    )
    canvas = div(
        div(id="workflow-editor-canvas", class_="workflow-editor-canvas"),
        class_="workflow-editor-canvas-wrap",
    )
    inspector = div(
        h2("Inspector"),
        div(id="workflow-editor-inspector", class_="workflow-editor-inspector"),
        class_="surface workflow-editor-inspector-panel",
    )
    toolbar = div(
        a("Back to list", href="/admin/workflows", class_="btn btn-secondary"),
        h1(workflow.name),
        small(workflow.code, class_="block-muted"),
        span(
            "Enabled" if workflow.enabled else "Disabled",
            class_="status-badge",
        ),
        div(
            button(
                "Undo",
                type="button",
                class_="btn btn-secondary btn-small",
                id="workflow-editor-undo",
                disabled=not can_write,
            ),
            button(
                "Redo",
                type="button",
                class_="btn btn-secondary btn-small",
                id="workflow-editor-redo",
                disabled=not can_write,
            ),
            button(
                "Auto layout",
                type="button",
                class_="btn btn-secondary btn-small",
                id="workflow-editor-layout",
                disabled=not can_write,
            ),
            button(
                "Fit canvas",
                type="button",
                class_="btn btn-secondary btn-small",
                id="workflow-editor-fit",
                disabled=not can_write,
            ),
            button(
                "JSON",
                type="button",
                class_="btn btn-secondary btn-small",
                id="workflow-editor-json-toggle",
            ),
            class_="workflow-editor-toolbar-actions",
        ),
        class_="workflow-editor-toolbar",
    )
    save_form = (
        form(
            _csrf(csrf),
            label(
                span("Name"),
                input_(name="name", value=workflow.name, required=True),
            ),
            label(
                span("Description"),
                input_(name="description", value=workflow.description or ""),
            ),
            label(
                span("Enabled"),
                select(
                    option("Yes", value="1", selected=bool(workflow.enabled)),
                    option("No", value="0", selected=not bool(workflow.enabled)),
                    name="enabled",
                ),
            ),
            input_(
                type="hidden",
                name="definition",
                id="workflow-editor-definition",
                value=_json.dumps(
                    workflow.definition or {}, ensure_ascii=False
                ),
            ),
            input_(
                type="hidden",
                name="next",
                value=f"/admin/workflows/{workflow.code}",
            ),
            button(
                "Save workflow",
                type="submit",
                class_="btn btn-primary",
                disabled=not can_write,
            ),
            action=f"/admin/workflows/{workflow.code}/update",
            method="post",
            class_="workflow-editor-save-form",
        )
        if can_write
        else div(
            span("Read only", class_="status-badge"),
            class_="workflow-editor-save-form",
        )
    )
    state_block = textarea(
        state_text,
        id="workflow-editor-state",
        name="workflow_editor_state",
        rows=1,
        readonly=True,
        class_="workflow-editor-state",
    )
    json_view = div(
        pre(
            _json.dumps(workflow.definition or {}, ensure_ascii=False, indent=2),
            id="workflow-editor-json",
            class_="workflow-editor-json",
        ),
        class_="surface workflow-editor-json-panel",
        id="workflow-editor-json-panel",
        hidden=True,
    )
    error_bar = div(
        ul(id="workflow-editor-errors", class_="workflow-editor-errors"),
        class_="workflow-editor-errorbar",
        id="workflow-editor-errorbar",
        hidden=True,
    )
    return shell(
        title_text=f"{workflow.name} · Workflow editor",
        principal=principal,
        csrf=csrf,
        theme=theme,
        color_mode=color_mode,
        density=density,
        pages=pages,
        flash=flash,
        flash_kind=flash_kind,
        content=div(
            section(toolbar, class_="surface"),
            div(
                palette,
                canvas,
                inspector,
                class_="workflow-editor-layout",
            ),
            error_bar,
            json_view,
            state_block,
            save_form,
            class_="stack",
        ),
        extra_styles=["/admin/static/workflow-editor.css?v=workflow-i18n-20261110-1"],
        extra_scripts=[
            "/admin/static/workflow-editor-core.js",
            "/admin/static/workflow-editor.js?v=workflow-entry-20261111-1",
        ],
    )
