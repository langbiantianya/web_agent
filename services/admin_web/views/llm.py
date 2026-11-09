"""LLM 接口优先级与故障切换配置页。"""

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

# 字段**只声明一次**：创建表单与编辑表单共用同一份描述。
#
# 但两者的**字段顺序不同**（创建态是 code/name/base_url/api_key/priority，
# 编辑态是 name/base_url/priority/api_key），而渲染快照比的是元素树——
# children 的顺序变了就是差异。所以用「字段字典 + 两个显式顺序」表达，
# 而不是「一份元组 + 展开」。
_LLM_FIELD_MAP = {
    "code": Field("code", "Code", required=True, modes=("create",)),
    "name": Field("name", "Name"),
    "base_url": Field("base_url", "Base URL", required=True, edit_required=False),
    "api_key": Field(
        "api_key",
        "API key",
        kind="password",
        autocomplete="new-password",
        edit_placeholder="Leave blank to keep current key",
        edit_label="Replacement API key",
    ),
    "priority": Field(
        "priority",
        "Priority",
        kind="number",
        min_value="0",
        # 编辑态显式取消 min（既有编辑表单没有这个属性）。
        edit_min_value="",
    ),
}

LLM_FIELDS = tuple(
    _LLM_FIELD_MAP[name] for name in ("name", "base_url", "priority", "api_key")
)

LLM_CREATE_FIELDS = tuple(
    _LLM_FIELD_MAP[name]
    for name in ("code", "name", "base_url", "api_key", "priority")
)


def llm_page(
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
    """渲染 LLM 接口优先级与故障切换配置页。"""
    table_rows = []
    for row in rows:
        actions = _toggle_and_delete(
            csrf=csrf,
            prefix=f"/admin/llm/{row.code}",
            enabled=bool(row.enabled),
            can_write=can_write,
        )
        edit = (
            details(
                summary("Edit endpoint"),
                render_form(
                    LLM_FIELDS,
                    mode="edit",
                    values={
                        "name": row.name or row.code,
                        "base_url": row.base_url,
                        "priority": row.priority,
                    },
                    csrf=csrf,
                    action=f"/admin/llm/{row.code}/update",
                    submit_label="Save endpoint",
                    layout="stack",
                ),
            )
            if can_write
            else span("")
        )
        table_rows.append(
            tr(
                td(row.name or row.code, small(row.code, class_="block-muted")),
                td(row.base_url),
                td(str(row.priority)),
                td("Enabled" if row.enabled else "Disabled"),
                td(actions, edit),
            )
        )
    create_form = (
        render_form(
            LLM_CREATE_FIELDS,
            mode="create",
            # 既有创建表单把优先级写死成 100。
            values={"priority": "100"},
            csrf=csrf,
            action="/admin/llm",
            submit_label="Create endpoint",
            layout="grid",
        )
        if can_write
        else None
    )
    return _management_page(
        title_text="LLM endpoints",
        lead="Lower priority numbers are attempted first. API keys remain write-only.",
        principal=principal,
        csrf=csrf,
        pages=pages,
        theme=theme,
        color_mode=color_mode,
        density=density,
        create_form=create_form,
        headers=["Endpoint", "Base URL", "Priority", "State", "Actions"],
        rows=table_rows,
        flash=flash,
    )
