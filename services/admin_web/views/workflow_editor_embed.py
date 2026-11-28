"""Workflow 画布的 island 片段页（阶段 D）。

SvelteKit 那一层负责导航与主题，画布本身仍旧是原来那套 Ludic 渲染 +
`workflow-editor.js`。这个模块只提供**一个不带外壳的完整 HTML 文档**，
让 SvelteKit 可以用 ``<iframe>`` 把它整块承载进来。

拆成独立模块的原因：画布页与片段页关心的是同一件事的两面——「画布长什么样」
和「画布被谁包着」——放一起会让那个文件顶到行数上限。
"""

from __future__ import annotations

from typing import Any

from ludic.html import (
    body,
    div,
    head,
    html,
    link,
    meta,
    script,
    title,
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


def embed_document(
    *,
    title_text: str,
    content: Any,
    flash: str | None = None,
    flash_kind: str = "info",
    theme: str = "fluent",
    color_mode: str = "system",
    density: str = "comfortable",
) -> Any:
    """渲染只含画布的片段页（阶段 D 的 island 载体）。

    它是一个**完整 HTML 文档**——SvelteKit 那边用 ``<iframe>`` 承载，
    所以自带 ``admin.css``（画布的 CSS 变量都在里面）与两份画布脚本。

    不加载 ``admin-ui.js``：那个脚本管的是导航与主题切换，画布用不到，
    少了它可以避免旧脚本在新外壳里乱改 DOM。
    """
    flash_block = (
        div(flash, class_=f"flash {flash_kind}", role="status")
        if flash
        else div("", class_="empty")
    )
    return html(
        head(
            meta(charset="utf-8"),
            meta(
                name="viewport",
                content="width=device-width, initial-scale=1",
            ),
            title(title_text),
            link(rel="stylesheet", href=ADMIN_STYLESHEET),
            link(rel="stylesheet", href=WORKFLOW_EDITOR_STYLESHEET),
            script(src=WORKFLOW_EDITOR_CORE_SCRIPT),
            script(src=WORKFLOW_EDITOR_SCRIPT),
        ),
        body(
            div(flash_block, content, class_="workflow-editor-embed"),
            class_="workflow-editor-body",
        ),
        lang="en",
        data_theme=theme,
        data_color_mode=color_mode,
        data_density=density,
    )
