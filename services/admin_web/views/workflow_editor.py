"""Workflow 画布编排页。

从 ``workflows.py`` 拆出来：画布页本身要渲染节点面板、属性面板、JSON
视图与保存表单，和列表页挤在一个文件里会突破单文件行数上限。

**两种渲染模式**：

- 默认：完整的 Ludic 页面（导航 + 画布）。这是**回退路径**——SvelteKit
  的 ``admin-ui`` 容器关掉后，管理端仍旧可用。
- ``embed=True``：只给画布的**片段页**，供 SvelteKit 阶段 D 以 iframe 的
  方式承载这个 island。外壳（导航、顶栏、主题切换）由 SvelteKit 那一层
  负责，画布本身一行代码都没有改。

两种模式共用同一份画布 DOM 与同一份脚本，差别只有外壳——所以阶段 D 的
「只换外壳」不是靠复制一份画布代码实现的。
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
from services.admin_web.views.workflow_editor_embed import (
    WORKFLOW_EDITOR_CORE_SCRIPT,
    WORKFLOW_EDITOR_SCRIPT,
    WORKFLOW_EDITOR_STYLESHEET,
    embed_document,
)


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
    embed: bool = False,
) -> Any:
    """渲染 Workflow 画布编排页。

    ``embed=True`` 时只返回画布片段（见模块文档的「两种渲染模式」）。
    """
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
        a(
            "Back to list",
            href="/admin/workflows",
            class_="btn btn-secondary",
            # island 模式下这一条要跳出 iframe：画布在 iframe 里，
            # 点「返回列表」若只在 iframe 内跳转，用户会以为点了没反应。
            attrs={"target": "_top"} if embed else None,
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
    # 保存后的落点：island 模式下留在片段页，否则 iframe 里会跳到整页
    # （整页会被 SvelteKit 的同名路由接管，画布就套进了自己）。
    next_path = (
        f"/admin/workflows/{workflow.code}/canvas"
        if embed
        else f"/admin/workflows/{workflow.code}"
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
                id="workflow-editor-next",
                value=next_path,
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
    if embed:
        return embed_document(
            title_text=f"{workflow.name} · Workflow editor",
            content=content,
            flash=flash,
            flash_kind=flash_kind,
            theme=theme,
            color_mode=color_mode,
            density=density,
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
        content=content,
        extra_styles=[WORKFLOW_EDITOR_STYLESHEET],
        extra_scripts=[
            WORKFLOW_EDITOR_CORE_SCRIPT,
            WORKFLOW_EDITOR_SCRIPT,
        ],
    )
