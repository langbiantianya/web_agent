"""数据驱动的表单字段。

过去同一个实体的「创建表单」与「编辑表单」是**把字段抄两遍**——字段名、
标签、类型都一样，只有少量差异（创建时多一个 ``code``、编辑时 API key 换
placeholder、textarea 行数不同）。抄两遍意味着改一个字段要记得改两处，
漏一处就是前后端字段对不上。

这里把字段抽成一份 :class:`Field` 描述，创建与编辑共用，差异用
``modes`` / ``edit_placeholder`` / ``edit_rows`` 表达。

**空值的语义是硬要求**：``value`` 为 ``None`` 表示「这个控件不带 value
属性」，``value=""`` 表示「带一个空的 value」。两者渲染出的 HTML 不等价，
所以由调用方显式决定，渲染层不做任何猜测。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ludic.html import (
    button,
    form,
    input as input_,
    label,
    option,
    select,
    span,
    textarea,
)


@dataclass(frozen=True)
class Field:
    """一个表单字段的声明。

    ``modes`` 决定字段出现在哪些表单里（``"create"`` / ``"edit"``）；
    ``edit_placeholder`` / ``edit_rows`` 表达编辑态的差异。

    ``kind`` 覆盖现有表单用到的全部普通控件：``text`` / ``password`` /
    ``email`` / ``tel`` / ``number`` / ``url`` / ``textarea`` / ``select``。
    **不覆盖 radio**——主题选择那种带预览卡片与 checked 逻辑的控件是页面
    特有的布局，保持手写，不硬塞进这套抽象。
    """

    name: str
    label: str
    kind: str = "text"
    required: bool = False
    options: tuple[tuple[str, str], ...] = ()
    placeholder: str = ""
    rows: int = 8
    autocomplete: str = ""
    min_value: str | None = None
    max_value: str | None = None
    modes: tuple[str, ...] = ("create", "edit")
    edit_placeholder: str = ""
    edit_rows: int | None = None


class _Input(input_):
    """能把空字符串 ``value`` 渲染出来的输入框。

    ludic 1.x 的 ``format_attrs`` 只保留非空值，空字符串属性会被整个丢掉，
    于是 ``value=""`` 与「压根没有 value 属性」渲染出的 HTML 完全一样。
    表单语义要求区分这两者，所以这里把被丢掉的空 ``value`` 补回 HTML。

    元素树（``aliased_attrs``）不受影响：空 ``value`` 仍然不进树，与既有
    表单（``input_(name="x", value="")``）的树结构保持一致。
    """

    def _format_attributes(self, is_html: bool = False) -> str:
        rendered = super()._format_attributes(is_html=is_html)
        if self.attrs.get("value") == "":
            rendered = f'{rendered} value=""' if rendered else 'value=""'
        return rendered


def _control(field: Field, value: object | None, *, mode: str) -> Any:
    """按 kind 造出控件本身（不含 label 包裹）。

    ``value`` 为 ``None`` 表示「这个控件不带 value 属性」——现有表单里
    ``input_(name="tools")`` 与 ``input_(name="tools", value="")`` 渲染出的
    HTML 不等价，必须让调用方显式区分。
    """
    text = None if value is None else str(value)
    attrs: dict[str, Any] = {}

    if field.kind == "textarea":
        rows = field.edit_rows if mode == "edit" and field.edit_rows else field.rows
        attrs.update(name=field.name, rows=rows)
        if field.required:
            attrs["required"] = True
        # 没有值时不放空文本子节点：既有表单里的 textarea 有的是空内容、
        # 有的是压根没有子节点，这个差别会体现在渲染快照上。
        return textarea(*([text] if text is not None else []), **attrs)

    if field.kind == "select":
        return select(
            *(
                option(label_text, value=option_value)
                for label_text, option_value in field.options
            ),
            name=field.name,
        )

    placeholder = (
        field.edit_placeholder
        if mode == "edit" and field.edit_placeholder
        else field.placeholder
    )
    attrs["name"] = field.name
    # 普通文本框不写 type——HTML 默认就是 text，既有表单也是这么写的。
    if field.kind != "text":
        attrs["type"] = field.kind
    if placeholder:
        attrs["placeholder"] = placeholder
    if field.required:
        attrs["required"] = True
    if field.autocomplete:
        attrs["autocomplete"] = field.autocomplete
    if field.min_value is not None:
        attrs["min"] = field.min_value
    if field.max_value is not None:
        attrs["max"] = field.max_value
    if text is not None:
        attrs["value"] = text
    return _Input(**attrs)


def render_field(field: Field, value: object | None, *, mode: str) -> Any:
    """渲染单个字段（``label`` 包裹控件）。"""
    return label(span(field.label), _control(field, value, mode=mode))


def render_form(
    fields: Iterable[Field],
    *,
    mode: str,
    values: Mapping[str, object],
    csrf: str,
    action: str,
    submit_label: str,
    layout: str = "grid",
) -> Any:
    """渲染整个表单。

    ``values`` 里**显式给出**的键才会带上 ``value`` 属性；没给出的键渲染成
    不带 value 的输入框。这不是细节：``<input name="x">`` 与
    ``<input name="x" value="">`` 在 HTML 上不等价。

    ``layout`` 是约定值：``"grid"``（创建表单，双列栅格）与 ``"stack"``
    （编辑表单，纵向堆叠）。取值只影响外层 class。
    """
    return form(
        input_(type="hidden", name="csrf_token", value=csrf),
        *(
            render_field(field, values.get(field.name), mode=mode)
            for field in fields
            if mode in field.modes
        ),
        button(submit_label, type="submit", class_="btn btn-primary"),
        action=action,
        method="post",
        class_="form-grid" if layout == "grid" else "stack",
    )
