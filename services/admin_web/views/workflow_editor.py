"""Workflow 画布片段页（V4.10 阶段 D / E）。

画布**不重写**：``workflow-editor.js``（67 KB）+ ``workflow-editor-core.js``
（11 KB）还是原来那套拖拽编排器。Ludic 视图层下线后，本模块是画布**唯一**
的 Python 渲染物——一段不带后台外壳的完整 HTML 文档，由 SvelteKit 的画布
外壳用 ``<iframe>`` 承载。

为什么不把片段直接拼进 SvelteKit 的 DOM：画布脚本会遍历自己的 DOM、依赖
``admin.css`` 里那 283 个 CSS 变量，而 SvelteKit 那一侧是 Tailwind 令牌
体系。``<iframe>`` 给画布一个**隔离的文档**，两套样式不会互相污染——
这是「不重写画布」能成立的前提。
"""

from __future__ import annotations

import json as _json
from typing import Any

from ludic.html import (
    a,
    body,
    button,
    div,
    form,
    h1,
    h2,
    h3,
    head,
    html,
    input as input_,
    label,
    li,
    link,
    meta,
    option,
    pre,
    script,
    section,
    select,
    small,
    span,
    textarea,
    title,
    ul,
)

from services.admin_web.views.shell import ADMIN_STYLESHEET

#: 画布自己的样式与脚本。版本号含义见 ``shell.ADMIN_UI_SCRIPT`` 的注释：
#: 静态资源缓存不认 ETag，改了脚本就要改版本号。
WORKFLOW_EDITOR_STYLESHEET = (
    "/admin/static/workflow-editor.css?v=workflow-i18n-20261110-1"
)
WORKFLOW_EDITOR_CORE_SCRIPT = "/admin/static/workflow-editor-core.js"
WORKFLOW_EDITOR_SCRIPT = (
    "/admin/static/workflow-editor.js?v=workflow-entry-20261111-1"
)


def _csrf(csrf: str) -> Any:
    """返回表单共用的 CSRF 隐藏字段。"""
    return input_(type="hidden", name="csrf_token", value=csrf)


def workflow_editor_page(
    *,
    csrf: str,
    workflow: Any,
    agents: list[Any],
    theme: str,
    color_mode: str,
    density: str,
    can_write: bool,
) -> Any:
    """渲染画布片段页（不带后台外壳）。"""
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
        a(
            "Back to list",
            href="/admin/workflows",
            class_="btn btn-secondary",
            # 画布在 iframe 里，这一条要跳出 iframe：只在 iframe 内跳转的话，
            # 用户会以为点了没反应。
            attrs={"target": "_top"},
        ),
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
    # 保存后的落点留在片段页：画布自己跳回整页的话，会跳出 iframe 的世界。
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
                id="workflow-editor-next",
                value=f"/admin/workflows/{workflow.code}/canvas",
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
    content = div(
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
    )
    return html(
        head(
            meta(charset="utf-8"),
            meta(
                name="viewport",
                content="width=device-width, initial-scale=1",
            ),
            title(f"{workflow.name} · Workflow editor"),
            link(rel="stylesheet", href=ADMIN_STYLESHEET),
            link(rel="stylesheet", href=WORKFLOW_EDITOR_STYLESHEET),
            script(src=WORKFLOW_EDITOR_CORE_SCRIPT),
            script(src=WORKFLOW_EDITOR_SCRIPT),
        ),
        body(
            div(content, class_="workflow-editor-embed"),
            class_="workflow-editor-body",
        ),
        lang="en",
        data_theme=theme,
        data_color_mode=color_mode,
        data_density=density,
    )
