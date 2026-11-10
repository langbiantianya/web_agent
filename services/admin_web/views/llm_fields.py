"""LLM 端点表单的字段、预设与探测控件。

从 ``llm.py`` 里拆出来：表单细节（字段、预设下拉、探测按钮）本身
就不短，再和列表渲染挤在一个文件里会突破单文件行数上限。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
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
)

from services.admin_web.views.fields import Field


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
        span(
            "",
            class_="block-muted",
            data_llm_status=list_id,
            role="status",
            aria_live="polite",
        ),
        # 排查指引：探测失败时才显示（UX-07）。
        details(
            summary("Troubleshooting"),
            p(
                "Check the base URL, the API key and whether the service "
                "accepts the OpenAI-compatible protocol "
                "(POST /v1/chat/completions).",
                class_="block-muted",
            ),
            p(
                "A 401 means the key is wrong, a 404 means the base URL is "
                "wrong, and a timeout means the host is unreachable.",
                class_="block-muted",
            ),
            class_="llm-probe-help",
            data_llm_help=list_id,
            hidden=True,
        ),
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
