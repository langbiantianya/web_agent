"""数据驱动的表单字段。

过去同一个实体的「创建表单」与「编辑表单」是**把字段抄两遍**——字段名、
标签、类型都一样，只有少量差异（创建时多一个 ``code``、编辑时 API key 换
标签与 placeholder、textarea 行数不同）。抄两遍意味着改一个字段要记得改
两处，漏一处就是前后端字段对不上。

这里把字段抽成一份 :class:`Field` 描述，创建与编辑共用，差异用
``modes`` / ``edit_label`` / ``edit_placeholder`` / ``edit_rows`` 表达。

**空值的语义交给调用方**：``value`` 为 ``None`` 表示「这个控件不带 value
属性」，``value=""`` 表示「带一个空的 value」。ludic 1.0.3 会丢掉空字符串
属性，所以这两种情况在**元素树**上渲染结果完全一样；本模块的 ``_Input``
只在 **HTML 文本层**把 ``value=""`` 补回去。也就是说这层差别渲染快照守
不到——Task 8 接入真实表单后必须人工逐字核对 HTML。
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

from services.admin_web.views.components import _csrf


@dataclass(frozen=True)
class Field:
    """一个表单字段的声明。

    ``modes`` 决定字段出现在哪些表单里（``"create"`` / ``"edit"）；
    ``edit_label`` / ``edit_placeholder`` / ``edit_rows`` 表达编辑态的差异
    （编辑态取 ``edit_*``，为空时回落到同名默认值）。

    ``kind`` 覆盖现有表单用到的全部普通控件：``text`` / ``password`` /
    ``email`` / ``tel`` / ``number`` / ``url`` / ``textarea`` / ``select``。
    **不覆盖 radio**——主题选择那种带预览卡片与 checked 逻辑的控件是页面
    特有的布局，保持手写，不硬塞进这套抽象。

    **各配置项对哪些 kind 有效**（无效的那部分是**静默丢弃**，不报错）：

    - ``value``：全部 kind（语义见 :func:`render_field`）；
    - ``required``：``text`` 系与 ``textarea``；``select`` 忽略；
    - ``placeholder``：``text`` 系；``textarea`` / ``select`` 忽略；
    - ``autocomplete``：``text`` 系；``textarea`` / ``select`` 忽略；
    - ``min_value`` / ``max_value``：``text`` 系（实际只有 ``number`` 用）；
    - ``rows`` / ``edit_rows``：只有 ``textarea``；
    - ``options``：只有 ``select``。

    另外 ``kind`` 会被原样写进控件的 ``type=`` 属性（``text`` 除外，它走
    HTML 默认类型；``textarea`` / ``select`` 不写 ``type``），所以
    ``kind="checkbox"`` 会生成一个语义错误的控件——这套抽象只覆盖既有
    表单已经用过的那几种，Task 8 接表单时别自造 kind。
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
    edit_label: str = ""


class _Input(input_):
    """能把空字符串 ``value`` 渲染出来的输入框。

    ludic 1.x 的 ``format_attrs`` 只保留非空值，空字符串属性会被整个丢掉，
    于是 ``value=""`` 与「压根没有 value 属性」渲染出的 HTML 完全一样。
    这里把被丢掉的空 ``value`` 补回 HTML 文本层。

    元素树（``aliased_attrs``）不受影响：空 ``value`` 仍然不进树，与既有
    表单（``input_(name="x", value="")``）的树结构保持一致。

    **这是有意为之的私有 API 依赖**：``_format_attributes`` 是 ludic 的私有
    方法（``ludic/base.py``），没有公共 API 能渲染出 ``value=""``——raw
    attrs 通道（``attrs={"value": ""}``）同样被真值判断丢掉，而
    ``value=None`` 会被渲染成字面量 ``value="None"``。``requirements.admin.txt``
    钉的是 ``ludic[full]>=1.0,<2``，一旦 ludic 改了这条规则，本模块的
    ``value=""`` 断言会立刻变红——升级 ludic 之后先跑
    ``tests/test_admin_fields.py``。

    **已知副作用**：

    - ``to_string()`` 用的是类型名，会渲染成 ``<_Input name="x" value="" />``，
      类名泄漏到输出里；
    - ludic 的 ``__eq__`` 要求 ``type(self) is type(other)``，所以
      ``_Input(...) == input_(...)`` 是 **False**。

    仓库现在没有 ``to_string`` 的调用点，快照只取 ``html_name`` 与
    ``aliased_attrs``，所以今天没有影响；但将来写
    ``assert element == expected_element`` 会以极难懂的方式失败。
    """

    def _format_attributes(self, is_html: bool = False) -> str:
        rendered = super()._format_attributes(is_html=is_html)
        if self.attrs.get("value") == "":
            rendered = f'{rendered} value=""' if rendered else 'value=""'
        return rendered


def _control(field: Field, value: object | None, *, mode: str) -> Any:
    """按 kind 造出控件本身（不含 label 包裹）。

    ``value`` 为 ``None`` 表示「这个控件不带 value 属性」；其余取值怎么
    落到 HTML 上见 :func:`render_field`。
    """
    attrs: dict[str, Any] = {}
    current = None if value is None else str(value)

    if field.kind == "textarea":
        rows = field.edit_rows if mode == "edit" and field.edit_rows else field.rows
        attrs.update(name=field.name, rows=rows)
        if field.required:
            attrs["required"] = True
        # 没有值时不放空文本子节点：既有表单里的 textarea 有的是空内容、
        # 有的是压根没有子节点，这个差别会体现在渲染快照上。
        return textarea(*([current] if current is not None else []), **attrs)

    if field.kind == "select":
        # **比较基准是字符串**：option 的值一律是字符串，所以布尔当前值先按
        # 既有表单的约定归一——True 命中 "1"（同时也接受 "True"），False 命中
        # "0"（同时也接受 "False"）；其余取值按 str(value) 比较。
        #
        # 不归一会退化成「零个 option 命中」，浏览器于是默认选中第一个
        # option（本该是 False 的字段被存成 True），所以这里必须归一。
        if isinstance(value, bool):
            matched = {"1", "True"} if value else {"0", "False"}
        else:
            matched = {current} if current is not None else set()
        # 有当前值时逐个 option 标 selected（True 与 False 都显式传，
        # 与既有表单 `option(..., selected=...)` 的写法一致）；没有当前值
        # （创建态）则一个 selected 都不发。
        return select(
            *(
                option(label_text, value=option_value, selected=option_value in matched)
                if current is not None
                else option(label_text, value=option_value)
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
    if value is not None:
        # bool 交给 ludic 的 bool 分支（False 不渲染该属性、True 渲染成
        # value="value"），与既有表单一致；不能 str() 成 "False"。
        attrs["value"] = value if isinstance(value, bool) else str(value)
    return _Input(**attrs)


def render_field(field: Field, value: object | None, *, mode: str) -> Any:
    """渲染单个字段（``label`` 包裹控件）。

    ``value`` 接受 ``str`` / ``int`` / ``float`` / ``bool`` / ``None``，
    但**布尔值的语义按 kind 而不同**，不能当成一条通用规则：

    - ``None`` → 控件**不带** value（输入框没有 ``value`` 属性，textarea
      没有文本子节点）；
    - ``kind`` 是 input 系（``text`` / ``password`` / ``email`` / ``tel`` /
      ``number`` / ``url``）且 ``value`` 是 ``bool`` → 走 ludic 的 bool 分支，
      和既有表单一致：``False`` 在 HTML 上不渲染该属性（元素树里是
      ``"false"``），``True`` 在 HTML 上是 ``value="value"``（元素树里是
      ``"true"``）；
    - ``kind="textarea"`` 且 ``value`` 是 ``bool`` → 文本 ``True`` / ``False``
      会**原样渲染进 textarea 的内容**（textarea 只有文本子节点，没有
      value 属性）；
    - ``kind="select"`` 且 ``value`` 是 ``bool`` → 当前值按既有表单约定归一
      后与 option 的值比较（``True`` 命中 ``"1"``，``False`` 命中 ``"0"``），
      详见 :func:`_control` 里 select 分支的注释；
    - 其它 → ``str(value)``。

    编辑态的标签用 ``field.edit_label``（为空时回落 ``field.label``），
    placeholder / rows 同理回落 ``edit_placeholder`` / ``edit_rows``。
    """
    label_text = (
        field.edit_label if mode == "edit" and field.edit_label else field.label
    )
    return label(span(label_text), _control(field, value, mode=mode))


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

    ``values`` 里**只有 ``None`` 才是缺省**：值为 ``None`` 的键渲染成不带
    value 的输入框，其余取值都会带上 value 属性。注意 ``False`` 不是
    缺省——它走 ludic 的 bool 分支，**HTML 上没有** ``value`` 属性
    （元素树里是 ``"false"``），但语义上是「有这个值」。另外
    ``values.get()`` 分不清「键没给出」和「键给了但值是 ``None``」——
    两者都渲染成不带 value。

    ``layout`` 是约定值：``"grid"``（创建表单，双列栅格）与 ``"stack"``
    （编辑表单，纵向堆叠）。取值只影响外层 class。
    """
    return form(
        _csrf(csrf),
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
