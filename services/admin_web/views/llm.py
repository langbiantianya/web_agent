"""LLM 接口优先级与故障切换配置页。

页面顶部交代两件事，因为它们决定了「该填什么」：

1. 所有端点都按 **OpenAI 兼容协议**（``POST /v1/chat/completions``）调用，
   所以 DeepSeek、阿里百炼、Qwen、vLLM 或自建服务填同一个表单；
2. DeepSeek 与阿里百炼的接入地址**内置成预设**，从下拉框选一项就能把
   编码、名称、基础 URL 填好，省掉手抄地址这一步。

每个表单（创建态与每行的编辑态）还带三件配套控件：

- **模型要填 ``<datalist>`` 挂着的输入框**：既能从「获取模型」拉回来的
  列表里下拉选，也能直接手输（自建网关的模型名往往不在列表里）；
- **Fetch models**：按当前表单的地址+密钥 ``GET /models``；
- **Test connection**：先拉列表，再对选中的模型发一次最小对话。

预设与探测都只是「填表/验表的助手」：预设不进数据库，也不携带密钥；
落库的仍是表单提交的字段，API Key 永远由使用者自己粘贴。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ludic.html import (
    button,
    datalist,
    details,
    div,
    label,
    option,
    p,
    select,
    small,
    span,
    summary,
    td,
    tr,
)

from app.models.admin_control import AdminPageDefinition
from services.admin_web.accounts import AdminPrincipal
from services.admin_web.views.components import (
    _management_page,
    _toggle_and_delete,
)
from services.admin_web.views.confirm import undo_panel
from services.admin_web.views.edit_panel import edit_link, edit_panel
from services.admin_web.views.list_controls import list_controls
from services.admin_web.listing import ListParams
from services.admin_web.views.fields import render_form
from services.admin_web.views.llm_fields import (
    CREATE_MODEL_LIST_ID,
    LLM_CREATE_FIELDS,
    LLM_FIELDS,
    LlmPreset,
    _form_fields,
    _model_tools,
    _preset_picker,
)


def llm_page(
    *,
    principal: AdminPrincipal,
    csrf: str,
    pages: Iterable[AdminPageDefinition],
    rows: list[Any],
    used_by_by_endpoint: dict[str, list[str]] | None = None,
    theme: str,
    color_mode: str,
    density: str,
    can_write: bool,
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
    """渲染 LLM 接口优先级与故障切换配置页。"""
    # 校验失败时把已填的值与字段级错误带回表单（UX-03）。
    create_errors = dict(create_errors or {})
    create_values = dict(create_values or {})
    per_row_errors = dict(edit_errors or {})
    per_row_values = dict(edit_values or {})
    usage_map = used_by_by_endpoint or {}
    table_rows = []
    panel = None

    def _editor_form(row: Any, list_id: str) -> Any:
        """构造某个 LLM 端点的编辑表单（面板内使用）。"""
        return render_form(
            _form_fields(LLM_FIELDS, list_id),
            mode="edit",
            values={
                "name": row.name or row.code,
                "base_url": row.base_url,
                "model": row.model,
                "priority": row.priority,
                **per_row_values.get(row.code, {}),
            },
            csrf=csrf,
            action=f"/admin/llm/{row.code}/update",
            submit_label="Save endpoint",
            layout="stack",
            errors=per_row_errors.get(row.code),
            form_id=f"llm-edit-{row.code}",
            error_summary=(
                "The endpoint could not be saved. Fix the highlighted fields."
                if per_row_errors.get(row.code)
                else ""
            ),
            actions=_model_tools(list_id, endpoint_code=row.code),
        )

    for row in rows:
        agents = usage_map.get(row.code, [])
        in_use_reason = (
            f"Used by agents: {', '.join(agents)}" if agents else ""
        )
        actions = _toggle_and_delete(
            csrf=csrf,
            prefix=f"/admin/llm/{row.code}",
            enabled=bool(row.enabled),
            can_write=can_write,
            label=f"LLM endpoint {row.name or row.code}",
            toggle_disabled_reason=in_use_reason,
            delete_disabled_reason=in_use_reason,
            delete_title=f"Delete LLM endpoint {row.name or row.code}",
            delete_summary=(
                f"LLM endpoint {row.name or row.code} ({row.code}) and its "
                "stored API key will be permanently removed."
            ),
            delete_impacts=(
                f"Used by agents: {', '.join(agents)}",
                "Agents bound to it will fail to resolve an LLM connection.",
            ) if agents else (
                "No agent uses this endpoint.",
            ),
        )
        list_id = f"llm-model-{row.code}"
        edit_form = (
            _editor_form(row, list_id)
            if can_write
            else None
        )
        if editing_row is not None and editing_row.code == row.code \
                and edit_form is not None:
            panel = edit_panel(
                resource=row.code,
                title=f"Edit endpoint {row.name or row.code}",
                form=edit_form,
                close_url="/admin/llm",
            )
            edit = span("")
        else:
            edit = (
                edit_link(
                    action="/admin/llm",
                    resource=row.code,
                    label="Edit endpoint",
                )
                if edit_form is not None
                else span("")
            )
        table_rows.append(
            tr(
                td(row.name or row.code, small(row.code, class_="block-muted")),
                td(row.base_url),
                td(row.model or "—"),
                td(
                    div(
                        span(
                            f"{len(agents)} agent(s)",
                            class_="reference-count",
                            data_reference_count=len(agents),
                        ),
                        small(
                            ", ".join(agents),
                            class_="block-muted",
                        ),
                    )
                    if agents
                    else span("None")
                ),
                td(str(row.priority)),
                td("Enabled" if row.enabled else "Disabled"),
                td(actions, edit),
            )
        )
    if panel is None and editing_row is not None and can_write:
        # 深链接指向的对象不在当前页时，面板照样打开。
        panel = edit_panel(
            resource=editing_row.code,
            title=f"Edit endpoint {editing_row.name or editing_row.code}",
            form=_editor_form(editing_row, f"llm-model-{editing_row.code}"),
            close_url="/admin/llm",
        )
    create_form = (
        render_form(
            _form_fields(LLM_CREATE_FIELDS, CREATE_MODEL_LIST_ID),
            mode="create",
            # 既有创建表单把优先级写死成 100。
            values={"priority": "100", **create_values},
            csrf=csrf,
            action="/admin/llm",
            submit_label="Create endpoint",
            layout="grid",
            errors=create_errors,
            form_id="llm-create",
            error_summary=(
                "The endpoint could not be created. Fix the highlighted fields."
                if create_errors
                else ""
            ),
            actions=_model_tools(CREATE_MODEL_LIST_ID, endpoint_code=""),
        )
        if can_write
        else None
    )
    # 下拉框只对「能写」的管理员有意义（它填的是创建表单）；只读管理员
    # 仍然能从 lead 里读到「内置预设」这件事，因此不会有信息缺口。
    notes = [_preset_picker()] if can_write else []
    return _management_page(
        title_text="LLM endpoints",
        lead=(
            "Endpoints are called with the OpenAI-compatible protocol "
            "(POST /v1/chat/completions), so DeepSeek, Alibaba Bailian, Qwen, "
            "vLLM and self-hosted services all work. Lower priority numbers "
            "are attempted first. API keys remain write-only."
        ),
        notes=notes,
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
                action="/admin/llm",
                params=list_params or ListParams(),
                total=list_total,
                sort_options=("name", "code", "priority"),
            )
            if list_params is not None
            else None
        ),
        headers=[
            "Endpoint",
            "Base URL",
            "Model",
            "Used by agents",
            "Priority",
            "State",
            "Actions",
        ],
        rows=table_rows,
        flash=flash,
        undo=(
            undo_panel(
                csrf=csrf,
                undo_id=undo_id,
                resource_label="LLM endpoint",
            )
            if undo_id is not None
            else None
        ),
    )
