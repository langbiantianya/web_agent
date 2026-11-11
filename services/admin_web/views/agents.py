"""Agent 配置管理页。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ludic.html import a, div, small, span, td, tr

from app.models.admin_control import AdminPageDefinition
from services.admin_web.accounts import AdminPrincipal
from services.admin_web.views.agent_connection import (
    AGENT_CREATE_FIELDS,
    AGENT_FIELDS,
    _connection_cell,
    agent_create_fields,
    agent_fields,
)
from services.admin_web.views.agent_capabilities import (
    capability_selector,
    capability_summary,
)
from services.admin_web.views.components import (
    _management_page,
    _toggle_and_delete,
)
from services.admin_web.views.confirm import undo_panel
from services.admin_web.views.edit_panel import edit_link, edit_panel
from services.admin_web.views.list_controls import list_controls
from services.admin_web.listing import ListParams
from services.admin_web.views.fields import render_form


def agents_page(
    *,
    principal: AdminPrincipal,
    csrf: str,
    pages: Iterable[AdminPageDefinition],
    rows: list[Any],
    llm_endpoints: Iterable[Any] = (),
    skills: Iterable[Any] = (),
    mcp_servers: Iterable[Any] = (),
    mcp_tools: Iterable[Any] = (),
    used_by_workflows: dict[str, list[str]] | None = None,
    theme: str,
    color_mode: str,
    density: str,
    can_write: bool,
    flash: str | None = None,
    flash_kind: str = "info",
    undo_id: int | None = None,
    create_errors: dict[str, str] | None = None,
    create_values: dict[str, str] | None = None,
    edit_errors: dict[str, dict[str, str]] | None = None,
    edit_values: dict[str, dict[str, str]] | None = None,
    list_params: ListParams | None = None,
    list_total: int = 0,
    editing_row: Any | None = None,
) -> Any:
    """渲染 Agent 配置管理页。"""
    # 校验失败时把已填的值与字段级错误带回表单（UX-03）。
    create_errors = dict(create_errors or {})
    create_values = dict(create_values or {})
    per_row_errors = dict(edit_errors or {})
    per_row_values = dict(edit_values or {})
    endpoint_list = list(llm_endpoints)
    skill_list = list(skills)
    server_list = list(mcp_servers)
    tool_list = list(mcp_tools)
    workflow_bindings = used_by_workflows or {}
    endpoints_by_code = {endpoint.code: endpoint for endpoint in endpoint_list}
    skills_by_code = {skill.code: skill for skill in skill_list}
    servers_by_code = {server.id: server for server in server_list}
    tools_by_name = {tool.qualified_name: tool for tool in tool_list}
    table_rows = []
    panel = None

    def _editor_form(row: Any) -> Any:
        """构造某个 Agent 的编辑表单（面板内使用）。"""
        return render_form(
            agent_fields(endpoint_list),
            mode="edit",
            values={
                "name": row.name,
                "llm_endpoint_code": (
                    getattr(row, "llm_endpoint_code", None) or ""
                ),
                "model": row.model,
                "system_prompt": row.system_prompt or "",
                **per_row_values.get(row.code, {}),
            },
            csrf=csrf,
            action=f"/admin/agents/{row.code}/update",
            submit_label="Save changes",
            layout="stack",
            errors=per_row_errors.get(row.code),
            form_id=f"agent-edit-{row.code}",
            error_summary=(
                "The agent could not be saved. Fix the highlighted fields."
                if per_row_errors.get(row.code)
                else ""
            ),
            actions=capability_selector(
                skills=skill_list,
                mcp_servers=server_list,
                mcp_tools=tool_list,
                selected_skills=row.skills or (),
                selected_tools=row.tools or (),
            ),
        )

    for row in rows:
        workflow_codes = workflow_bindings.get(row.code, [])
        in_use_reason = (
            f"Referenced by workflows: {', '.join(workflow_codes)}"
            if workflow_codes
            else ""
        )
        actions = _toggle_and_delete(
            csrf=csrf,
            prefix=f"/admin/agents/{row.code}",
            enabled=bool(row.enabled),
            can_write=can_write,
            label=f"Agent {row.name}",
            toggle_disabled_reason=in_use_reason,
            delete_disabled_reason=in_use_reason,
            delete_title=f"Delete Agent {row.name}",
            delete_summary=(
                f"Agent {row.name} ({row.code}) will be permanently removed."
            ),
            delete_impacts=(
                f"Referenced by workflows: {', '.join(workflow_codes)}",
                "Agents keep their configuration; the agent itself is gone.",
            ) if workflow_codes else (
                "No workflow references this agent.",
                "Agents keep their configuration; the agent itself is gone.",
            ),
        )
        editor_form = (
            _editor_form(row)
            if can_write
            else None
        )
        if editing_row is not None and editing_row.code == row.code \
                and editor_form is not None:
            panel = edit_panel(
                resource=row.code,
                title=f"Edit agent {row.name}",
                form=editor_form,
                close_url="/admin/agents",
            )
            editor = span("")
        else:
            editor = (
                edit_link(
                    action="/admin/agents",
                    resource=row.code,
                    label="Edit agent",
                )
                if editor_form is not None
                else span("")
            )
        table_rows.append(
            tr(
                td(row.name, small(row.code, class_="block-muted")),
                td(_connection_cell(row, endpoints_by_code)),
                td(row.model),
                td(
                    capability_summary(
                        row,
                        skills_by_code=skills_by_code,
                        servers_by_code=servers_by_code,
                        tools_by_name=tools_by_name,
                    )
                ),
                td(", ".join(workflow_codes) if workflow_codes else "None"),
                td("Enabled" if row.enabled else "Disabled"),
                td(f"v{row.version}"),
                td(actions, editor),
            )
        )
    if panel is None and editing_row is not None and can_write:
        # 深链接指向的对象不在当前页时，面板照样打开。
        panel = edit_panel(
            resource=editing_row.code,
            title=f"Edit agent {editing_row.name}",
            form=_editor_form(editing_row),
            close_url="/admin/agents",
        )
    create_form = (
        render_form(
            agent_create_fields(endpoint_list),
            mode="create",
            values=create_values,
            csrf=csrf,
            action="/admin/agents",
            submit_label="Create agent",
            layout="grid",
            errors=create_errors,
            form_id="agent-create",
            error_summary=(
                "The agent could not be created. Fix the highlighted fields."
                if create_errors
                else ""
            ),
            actions=capability_selector(
                skills=skill_list,
                mcp_servers=server_list,
                mcp_tools=tool_list,
            ),
        )
        if can_write and any(endpoint.enabled for endpoint in endpoint_list)
        else None
    )
    notes = []
    if can_write and not any(endpoint.enabled for endpoint in endpoint_list):
        notes.append(
            div(
                span("Configure an LLM connection before creating an Agent. "),
                a("Open LLM endpoints", href="/admin/llm"),
                class_="surface-callout",
            )
        )
    if can_write:
        notes.append(
            div(
                span("Capabilities are managed on "),
                a("Skills", href="/admin/skills"),
                span(" and "),
                a("MCP servers", href="/admin/mcp"),
                span("."),
                class_="surface-callout",
            )
        )
    return _management_page(
        title_text="Agents",
        lead=(
            "Each Agent selects a configured LLM connection and explicit "
            "Skills and tools. Tool hints from a Skill remain suggestions; "
            "only checked tools are authorized."
        ),
        principal=principal,
        csrf=csrf,
        pages=pages,
        theme=theme,
        color_mode=color_mode,
        density=density,
        create_form=create_form,
        panel=panel,
        notes=notes,
        controls=(
            list_controls(
                action="/admin/agents",
                params=list_params or ListParams(),
                total=list_total,
                sort_options=("name", "code", "created"),
            )
            if list_params is not None
            else None
        ),
        headers=[
            "Agent",
            "LLM connection",
            "Model",
            "Capabilities",
            "Used by workflows",
            "State",
            "Version",
            "Actions",
        ],
        rows=table_rows,
        flash=flash,
        flash_kind=flash_kind,
        undo=(
            undo_panel(
                csrf=csrf,
                undo_id=undo_id,
                resource_label="Agent",
            )
            if undo_id is not None
            else None
        ),
    )
