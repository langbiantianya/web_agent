"""MCP 服务器管理页。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ludic.html import button, details, form, small, span, summary, td, tr

from app.models.admin_control import AdminPageDefinition
from services.admin_web.accounts import AdminPrincipal
from services.admin_web.views.components import (
    _csrf,
    _management_page,
    _toggle_and_delete,
)
from services.admin_web.views.fields import Field, render_form

# 字段**只声明一次**：创建表单与编辑表单共用同一份描述。
#
# 但两者的**字段顺序不同**（创建态是 code/name/transport/url/command/
# timeout_sec，编辑态是 name/timeout_sec/command/url），而渲染快照比的是
# 元素树——children 的顺序变了就是差异。所以用「字段字典 + 两个显式
# 顺序」表达，而不是「一份元组 + 展开」。
_MCP_FIELD_MAP = {
    "code": Field("code", "Code", required=True, modes=("create",)),
    "name": Field("name", "Name"),
    "transport": Field(
        "transport",
        "Transport",
        kind="select",
        modes=("create",),
        options=(("HTTP", "http"), ("stdio", "stdio")),
    ),
    "url": Field("url", "HTTP URL", edit_label="URL"),
    "command": Field("command", "stdio command", edit_label="Command"),
    "timeout_sec": Field(
        "timeout_sec",
        "Timeout seconds",
        kind="number",
        min_value="1",
        max_value="600",
        # 编辑态显式取消 min / max（既有编辑表单没有这两个属性）。
        edit_min_value="",
        edit_max_value="",
    ),
}

MCP_FIELDS = tuple(
    _MCP_FIELD_MAP[name]
    for name in ("name", "timeout_sec", "command", "url")
)

MCP_CREATE_FIELDS = tuple(
    _MCP_FIELD_MAP[name]
    for name in ("code", "name", "transport", "url", "command", "timeout_sec")
)


def mcp_page(
    *,
    principal: AdminPrincipal,
    csrf: str,
    pages: Iterable[AdminPageDefinition],
    rows: list[Any],
    theme: str,
    color_mode: str,
    density: str,
    can_write: bool,
    can_refresh: bool,
    flash: str | None = None,
) -> Any:
    """渲染 MCP 服务器管理页。"""
    table_rows = []
    for row in rows:
        actions = _toggle_and_delete(
            csrf=csrf,
            prefix=f"/admin/mcp/{row.code}",
            enabled=bool(row.enabled),
            can_write=can_write,
        )
        refresh = (
            form(
                _csrf(csrf),
                button("Refresh tools", type="submit", class_="btn btn-secondary btn-small"),
                action=f"/admin/mcp/{row.code}/refresh",
                method="post",
                class_="inline-form",
            )
            if can_refresh
            else span("")
        )
        edit = (
            details(
                summary("Edit connection"),
                render_form(
                    MCP_FIELDS,
                    mode="edit",
                    values={
                        "name": row.name or row.code,
                        "timeout_sec": row.timeout_sec,
                        "command": row.command or "",
                        "url": row.url or "",
                    },
                    csrf=csrf,
                    action=f"/admin/mcp/{row.code}/update",
                    submit_label="Save connection",
                    layout="stack",
                ),
            )
            if can_write
            else span("")
        )
        table_rows.append(
            tr(
                td(row.name or row.code, small(row.code, class_="block-muted")),
                td(row.transport),
                td("Enabled" if row.enabled else "Disabled"),
                td(f"v{row.version}"),
                td(refresh, actions, edit),
            )
        )
    create_form = (
        render_form(
            MCP_CREATE_FIELDS,
            mode="create",
            # 既有创建表单把超时写死成 30。
            values={"timeout_sec": "30"},
            csrf=csrf,
            action="/admin/mcp",
            submit_label="Register server",
            layout="grid",
        )
        if can_write
        else None
    )
    return _management_page(
        title_text="MCP servers",
        lead="Connection credentials stay encrypted and are never rendered back into the console.",
        principal=principal,
        csrf=csrf,
        pages=pages,
        theme=theme,
        color_mode=color_mode,
        density=density,
        create_form=create_form,
        headers=["Server", "Transport", "State", "Version", "Actions"],
        rows=table_rows,
        flash=flash,
    )
