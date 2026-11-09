"""Agent 配置管理页。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ludic.html import details, small, span, summary, td, tr

from app.models.admin_control import AdminPageDefinition
from services.admin_web.accounts import AdminPrincipal
from services.admin_web.views.components import (
    _management_page,
    _toggle_and_delete,
)
from services.admin_web.views.fields import Field, render_form

# 创建表单与编辑表单**共用**同一份字段描述：差异用 modes / edit_label /
# edit_placeholder / edit_rows 表达，不再把字段抄两遍。
#
# 字段顺序是渲染结构的一部分（渲染快照比的是元素树，children 顺序变了
# 就是差异），所以创建态与编辑态的顺序各自写死。
AGENT_FIELDS = (
    Field("name", "Name", required=True),
    Field("model", "Model", required=True),
    Field("base_url", "Base URL"),
    Field(
        "api_key",
        "API key",
        kind="password",
        autocomplete="new-password",
        edit_placeholder="Leave blank to keep current key",
        edit_label="Replacement API key",
    ),
    Field("tools", "Tools (comma separated)"),
    Field("skills", "Skills (comma separated)"),
    Field(
        "system_prompt",
        "System prompt",
        kind="textarea",
        required=True,
        rows=8,
        edit_rows=10,
    ),
)

AGENT_CREATE_FIELDS = (
    Field("code", "Code", required=True, modes=("create",)),
    *AGENT_FIELDS,
    Field(
        "enabled",
        "Enabled",
        kind="select",
        modes=("create",),
        options=(("Yes", "1"), ("No", "0")),
    ),
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
                render_form(
                    AGENT_FIELDS,
                    mode="edit",
                    values={
                        "name": row.name,
                        "model": row.model,
                        "base_url": row.base_url or "",
                        "tools": ", ".join(row.tools or []),
                        "skills": ", ".join(row.skills or []),
                        # **有意**的 or ""：既有代码写的是 textarea(row.system_prompt, ...)，
                        # 而 ludic 会把 None 渲染成字面量 "None" 的文本。or "" 让
                        # 内容为空而不是 "None"（与 skills 的 row.body or "" 一致）。
                        "system_prompt": row.system_prompt or "",
                    },
                    csrf=csrf,
                    action=f"/admin/agents/{row.code}/update",
                    submit_label="Save changes",
                    layout="stack",
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
        render_form(
            AGENT_CREATE_FIELDS,
            mode="create",
            values={},
            csrf=csrf,
            action="/admin/agents",
            submit_label="Create agent",
            layout="grid",
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
