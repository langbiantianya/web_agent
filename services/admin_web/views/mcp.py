"""MCP 服务器管理页。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ludic.html import button, div, form, small, span, td, tr

from app.models.admin_control import AdminPageDefinition
from services.admin_web.accounts import AdminPrincipal
from services.admin_web.views.components import (
    _csrf,
    _management_page,
    _toggle_and_delete,
)
from services.admin_web.views.confirm import undo_panel
from services.admin_web.views.edit_panel import edit_link, edit_panel
from services.admin_web.views.list_controls import list_controls
from services.admin_web.listing import ListParams
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
    tools_by_server: dict[int, list[Any]] | None = None,
    used_by_by_server: dict[str, list[str]] | None = None,
    theme: str,
    color_mode: str,
    density: str,
    can_write: bool,
    can_refresh: bool,
    flash: str | None = None,
    undo_id: int | None = None,
    create_errors: dict[str, str] | None = None,
    create_values: dict[str, str] | None = None,
    edit_errors: dict[str, dict[str, str]] | None = None,
    edit_values: dict[str, dict[str, str]] | None = None,
    list_params: ListParams | None = None,
    list_total: int = 0,
    editing_row: Any | None = None,
) -> Any:
    """渲染 MCP 服务器管理页。"""
    # 校验失败时把已填的值与字段级错误带回表单（UX-03）。
    create_errors = dict(create_errors or {})
    create_values = dict(create_values or {})
    per_row_errors = dict(edit_errors or {})
    per_row_values = dict(edit_values or {})
    tools_map = tools_by_server or {}
    usage_map = used_by_by_server or {}
    table_rows = []
    panel = None

    def _editor_form(row: Any) -> Any:
        """构造某个 MCP 服务器的编辑表单（面板内使用）。"""
        return render_form(
            MCP_FIELDS,
            mode="edit",
            values={
                "name": row.name or row.code,
                "timeout_sec": row.timeout_sec,
                "command": row.command or "",
                "url": row.url or "",
                **per_row_values.get(row.code, {}),
            },
            csrf=csrf,
            action=f"/admin/mcp/{row.code}/update",
            submit_label="Save connection",
            layout="stack",
            errors=per_row_errors.get(row.code),
            form_id=f"mcp-edit-{row.code}",
            error_summary=(
                "The connection could not be saved. Fix the highlighted fields."
                if per_row_errors.get(row.code)
                else ""
            ),
        )

    for row in rows:
        tools = tools_map.get(getattr(row, "id", None), [])
        used_by = usage_map.get(row.code, [])
        in_use_reason = (
            f"Used by agents: {', '.join(used_by)}" if used_by else ""
        )
        actions = _toggle_and_delete(
            csrf=csrf,
            prefix=f"/admin/mcp/{row.code}",
            enabled=bool(row.enabled),
            can_write=can_write,
            label=f"MCP server {row.name or row.code}",
            toggle_disabled_reason=in_use_reason,
            delete_disabled_reason=in_use_reason,
            delete_title=f"Delete MCP server {row.name or row.code}",
            delete_summary=(
                f"MCP server {row.name or row.code} ({row.code}) and its "
                "discovered tool catalog will be permanently removed."
            ),
            delete_impacts=(
                f"Used by agents: {', '.join(used_by)}",
                f"{len(tools)} discovered tool(s) will be removed.",
            ) if used_by else (
                f"{len(tools)} discovered tool(s) will be removed.",
            ),
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
        edit_form = (
            _editor_form(row)
            if can_write
            else None
        )
        if editing_row is not None and editing_row.code == row.code \
                and edit_form is not None:
            panel = edit_panel(
                resource=row.code,
                title=f"Edit connection {row.name or row.code}",
                form=edit_form,
                close_url="/admin/mcp",
            )
            edit = span("")
        else:
            edit = (
                edit_link(
                    action="/admin/mcp",
                    resource=row.code,
                    label="Edit connection",
                )
                if edit_form is not None
                else span("")
            )
        table_rows.append(
            tr(
                td(row.name or row.code, small(row.code, class_="block-muted")),
                td(
                    f"{len(tools)} tools",
                    small(
                        ", ".join(tool.tool_name for tool in tools[:3])
                        or "Refresh to discover tools",
                        class_="block-muted",
                    ),
                ),
                td(
                    div(
                        span(
                            f"{len(used_by)} agent(s)",
                            class_="reference-count",
                            data_reference_count=len(used_by),
                        ),
                        small(", ".join(used_by), class_="block-muted"),
                    )
                    if used_by
                    else span("None")
                ),
                td(row.transport),
                td("Enabled" if row.enabled else "Disabled"),
                td(f"v{row.version}"),
                td(refresh, actions, edit),
            )
        )
    if panel is None and editing_row is not None and can_write:
        # 深链接指向的对象不在当前页时，面板照样打开。
        panel = edit_panel(
            resource=editing_row.code,
            title=f"Edit connection {editing_row.name or editing_row.code}",
            form=_editor_form(editing_row),
            close_url="/admin/mcp",
        )
    create_form = (
        render_form(
            MCP_CREATE_FIELDS,
            mode="create",
            # 既有创建表单把超时写死成 30。
            values={"timeout_sec": "30", **create_values},
            csrf=csrf,
            action="/admin/mcp",
            submit_label="Register server",
            layout="grid",
            errors=create_errors,
            form_id="mcp-create",
            error_summary=(
                "The server could not be registered. Fix the highlighted "
                "fields."
                if create_errors
                else ""
            ),
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
        panel=panel,
        controls=(
            list_controls(
                action="/admin/mcp",
                params=list_params or ListParams(),
                total=list_total,
                sort_options=("name", "code"),
            )
            if list_params is not None
            else None
        ),
        headers=[
            "Server",
            "Tools",
            "Used by agents",
            "Transport",
            "State",
            "Version",
            "Actions",
        ],
        rows=table_rows,
        flash=flash,
        undo=(
            undo_panel(
                csrf=csrf,
                undo_id=undo_id,
                resource_label="MCP server",
            )
            if undo_id is not None
            else None
        ),
    )
