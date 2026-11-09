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
from dataclasses import dataclass, replace
from typing import Any

from ludic.html import (
    button,
    datalist,
    details,
    div,
    label,
    option,
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
from services.admin_web.views.fields import Field, render_form


@dataclass(frozen=True)
class LlmPreset:
    """一条内置的 OpenAI 兼容接入预设。

    ``code`` / ``name`` / ``base_url`` 是**无需保密**的默认值，可以写进
    代码；API Key 不在预设里，否则等于把凭据打进镜像。
    """

    value: str
    label: str
    code: str
    name: str
    base_url: str


# 只收录官方提供 OpenAI 兼容端点（``/v1/chat/completions``）的服务商，
# 地址取各家文档里的「兼容模式」入口；同一服务商国内/国际站地址不同，
# 这里取国内站，国际站可在创建后按行编辑修改。
LLM_PRESETS: tuple[LlmPreset, ...] = (
    LlmPreset(
        value="deepseek",
        label="DeepSeek (api.deepseek.com)",
        code="deepseek",
        name="DeepSeek",
        base_url="https://api.deepseek.com/v1",
    ),
    LlmPreset(
        value="bailian",
        label="Alibaba Bailian (DashScope)",
        code="bailian",
        name="Alibaba Bailian",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    ),
)

# 字段**只声明一次**：创建表单与编辑表单共用同一份描述。
#
# 但两者的**字段顺序不同**（创建态是 code/name/base_url/model/api_key/priority，
# 编辑态是 name/base_url/model/priority/api_key），而渲染快照比的是元素树——
# children 的顺序变了就是差异。所以用「字段字典 + 两个显式顺序」表达，
# 而不是「一份元组 + 展开」。
#
# ``model.list_id`` 故意留空：模型候选按**表单**隔离（见 ``_form_fields``），
# 共享一个 datalist 会让 A 端点的模型名出现在 B 端点的候选里。
_LLM_FIELD_MAP = {
    "code": Field("code", "Code", required=True, modes=("create",)),
    "name": Field("name", "Name"),
    "base_url": Field("base_url", "Base URL", required=True, edit_required=False),
    "model": Field("model", "Model", placeholder="deepseek-chat"),
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
    _LLM_FIELD_MAP[name]
    for name in ("name", "base_url", "model", "priority", "api_key")
)

LLM_CREATE_FIELDS = tuple(
    _LLM_FIELD_MAP[name]
    for name in ("code", "name", "base_url", "model", "api_key", "priority")
)

# 创建表单的模型候选容器 id；每行编辑表单用 ``llm-model-{code}``。
CREATE_MODEL_LIST_ID = "llm-model-new"


def _form_fields(fields: tuple[Field, ...], list_id: str) -> tuple[Field, ...]:
    """把模型输入框挂到**该表单自己的** datalist 上。"""
    return tuple(
        replace(field, list_id=list_id) if field.name == "model" else field
        for field in fields
    )


def _model_tools(list_id: str, *, endpoint_code: str) -> Any:
    """模型候选容器 + 「获取模型 / 测试连接」按钮 + 结果位。

    按钮一律 ``type="button"``，不会提交表单；真正的请求由
    ``admin-ui.js`` 发到 ``/admin/llm/probe``。``data-llm-endpoint`` 让
    服务端知道「编辑态留空的 API Key 该用哪个端点已存的密钥」——创建态
    传空串，表示必须用表单里现填的 Key。
    """
    return div(
        datalist(id=list_id),
        button(
            "Fetch models",
            type="button",
            class_="btn btn-secondary btn-small",
            data_llm_fetch=list_id,
            data_llm_endpoint=endpoint_code,
        ),
        button(
            "Test connection",
            type="button",
            class_="btn btn-secondary btn-small",
            data_llm_test=list_id,
            data_llm_endpoint=endpoint_code,
        ),
        span("", class_="block-muted", data_llm_status=list_id),
        class_="row-actions",
    )


def _preset_picker() -> Any:
    """渲染「内置预设」下拉框。

    选中的 ``option`` 用 ``data-*`` 携带编码、名称与地址，由
    ``admin-ui.js`` 写进下面创建表单的对应输入框；服务端**不接收**这个
    下拉框的值（没有 ``name``），所以预设不是一条独立业务数据，也不会
    多出一个需要校验的字段。

    ``data-llm-preset`` 是纯前端挂钩：ludic 会丢掉空属性值，所以这里必须
    给一个非空值，否则 JS 的选择器匹配不到它。
    """
    return label(
        span("Provider preset"),
        select(
            option("Custom endpoint (fill in manually)", value="custom"),
            *(
                option(
                    preset.label,
                    value=preset.value,
                    data_code=preset.code,
                    data_name=preset.name,
                    data_base_url=preset.base_url,
                )
                for preset in LLM_PRESETS
            ),
            data_llm_preset="picker",
        ),
        span(
            "Choosing a preset fills the endpoint code, name and base URL; "
            "it never fills the API key or the model.",
            class_="block-muted",
        ),
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
        list_id = f"llm-model-{row.code}"
        edit = (
            details(
                summary("Edit endpoint"),
                render_form(
                    _form_fields(LLM_FIELDS, list_id),
                    mode="edit",
                    values={
                        "name": row.name or row.code,
                        "base_url": row.base_url,
                        "model": row.model,
                        "priority": row.priority,
                    },
                    csrf=csrf,
                    action=f"/admin/llm/{row.code}/update",
                    submit_label="Save endpoint",
                    layout="stack",
                    actions=_model_tools(list_id, endpoint_code=row.code),
                ),
            )
            if can_write
            else span("")
        )
        table_rows.append(
            tr(
                td(row.name or row.code, small(row.code, class_="block-muted")),
                td(row.base_url),
                td(row.model or "—"),
                td(str(row.priority)),
                td("Enabled" if row.enabled else "Disabled"),
                td(actions, edit),
            )
        )
    create_form = (
        render_form(
            _form_fields(LLM_CREATE_FIELDS, CREATE_MODEL_LIST_ID),
            mode="create",
            # 既有创建表单把优先级写死成 100。
            values={"priority": "100"},
            csrf=csrf,
            action="/admin/llm",
            submit_label="Create endpoint",
            layout="grid",
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
        headers=[
            "Endpoint",
            "Base URL",
            "Model",
            "Priority",
            "State",
            "Actions",
        ],
        rows=table_rows,
        flash=flash,
    )
