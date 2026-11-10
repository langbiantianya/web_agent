"""数据驱动的表单字段。

过去同一个实体的「创建表单」与「编辑表单」是**把字段抄两遍**——字段名、
标签、类型都一样，只有少量差异（创建时多一个 ``code``、编辑时 API key 换
标签与 placeholder、textarea 行数不同）。抄两遍意味着改一个字段要记得改
两处，漏一处就是前后端字段对不上。

这里把字段抽成一份 :class:`Field` 描述，创建与编辑共用，差异用
``modes`` / ``edit_label`` / ``edit_placeholder`` / ``edit_rows`` /
``edit_required`` / ``edit_min_value`` / ``edit_max_value`` 表达。

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
    div,
    form,
    label,
    p,
    select,
    span,
    textarea,
)

from services.admin_web.views.components import _csrf
from services.admin_web.views.field_controls import _Input, _Option


@dataclass(frozen=True)
class Field:
    """一个表单字段的声明。

    ``modes`` 决定字段出现在哪些表单里（``"create"`` / ``"edit"）；
    ``edit_label`` / ``edit_placeholder`` / ``edit_rows`` 表达编辑态的差异
    （编辑态取 ``edit_*``，为空时回落到同名默认值）。

    ``edit_required`` / ``edit_min_value`` / ``edit_max_value`` 是**三态**
    覆盖项：``None`` 表示继承同名默认项；``False`` / ``""`` 表示编辑态
    **显式取消**（不渲染对应属性）；其余取值表示编辑态改用该值。

    ``kind`` 覆盖现有表单用到的全部普通控件：``text`` / ``password`` /
    ``email`` / ``tel`` / ``number`` / ``url`` / ``textarea`` / ``select``。
    **不覆盖 radio**——主题选择那种带预览卡片与 checked 逻辑的控件是页面
    特有的布局，保持手写，不硬塞进这套抽象。

    **各配置项对哪些 kind 有效**（无效的那部分是**静默丢弃**，不报错）：

    - ``value``：全部 kind（语义见 :func:`render_field`）；
    - ``modes``：全部 kind（决定字段出现在哪些表单；``render_form`` 用
      ``mode in field.modes`` 过滤，不在当前模式里的字段整条不渲染）；
    - ``required``：``text`` 系、``textarea`` 与 ``select``；
    - ``edit_required``：``text`` 系、``textarea`` 与 ``select``。
      ``None`` = 继承 ``required``；``True`` = 编辑态必填；``False`` =
      编辑态**显式不必填**（不渲染 ``required`` 属性）；
    - ``placeholder``：``text`` 系；``textarea`` / ``select`` 忽略；
    - ``edit_placeholder``：``text`` 系；``textarea`` / ``select`` 忽略。
      空串 = 继承 ``placeholder``；
    - ``edit_label``：全部 kind（只改标签文案；为空时回落 ``label``）；
    - ``autocomplete``：``text`` 系；``textarea`` / ``select`` 忽略；
    - ``min_value`` / ``max_value``：``text`` 系（实际只有 ``number`` 用）；
    - ``edit_min_value`` / ``edit_max_value``：``text`` 系；
      ``None`` = 继承 ``min_value`` / ``max_value``；``""`` = 编辑态
      **显式取消**该属性（不渲染 ``min`` / ``max``）；
    - ``rows`` / ``edit_rows``：只有 ``textarea``；
    - ``options``：只有 ``select``。

    ``edit_*`` 系列**只在编辑态生效**（``mode="edit"``），创建态一律用
    同名的默认项。

    ``list_id`` 让普通输入框挂上 ``<datalist>``（HTML 原生「下拉候选 +
    自由输入」）：值是要引用的 datalist 的 id，空串表示不挂。每个表单各自
    一份 datalist——同一页上不同端点的模型候选不能互相串。

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
    edit_required: bool | None = None
    edit_min_value: str | None = None
    edit_max_value: str | None = None
    list_id: str = ""


def _control(field: Field, value: object | None, *, mode: str) -> Any:
    """按 kind 造出控件本身（不含 label 包裹）。

    ``value`` 为 ``None`` 表示「这个控件不带 value 属性」；其余取值怎么
    落到 HTML 上见 :func:`render_field`。
    """
    attrs: dict[str, Any] = {}
    current = None if value is None else str(value)

    # 编辑态的三个覆盖项：edit_required 为 None 时继承 required；
    # edit_min_value / edit_max_value 为 None 时继承 min_value / max_value，
    # 为空串时表示「编辑态显式取消该属性」。
    required = field.required
    min_value = field.min_value
    max_value = field.max_value
    if mode == "edit":
        if field.edit_required is not None:
            required = field.edit_required
        if field.edit_min_value is not None:
            min_value = field.edit_min_value
        if field.edit_max_value is not None:
            max_value = field.edit_max_value

    if field.kind == "textarea":
        rows = (
            field.edit_rows
            if mode == "edit" and field.edit_rows is not None
            else field.rows
        )
        attrs.update(name=field.name, rows=rows)
        if required:
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
                _Option(
                    label_text,
                    value=option_value,
                    selected=option_value in matched,
                )
                if current is not None
                else _Option(label_text, value=option_value)
                for label_text, option_value in field.options
            ),
            name=field.name,
            required=required,
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
    if required:
        attrs["required"] = True
    if field.autocomplete:
        attrs["autocomplete"] = field.autocomplete
    if min_value:
        attrs["min"] = min_value
    if max_value:
        attrs["max"] = max_value
    if field.list_id:
        # 只有 input 系支持 list；datalist 本身由调用方渲染。
        attrs["list"] = field.list_id
    if value is not None:
        # bool 交给 ludic 的 bool 分支（False 不渲染该属性、True 渲染成
        # value="value"），与既有表单一致；不能 str() 成 "False"。
        attrs["value"] = value if isinstance(value, bool) else str(value)
    return _Input(**attrs)


def _error_id(field_name: str, form_id: str) -> str:
    """返回字段级错误提示的 DOM id。

    错误提示要能被读屏软件关联到控件上，靠的就是 ``aria-describedby``
    指向这个 id。同一页面上可能有多个表单（每行一个编辑表单），
    所以 id 必须带上表单自身的标识，否则会串到别的表单上。
    """
    safe = "".join(
        char if char.isalnum() or char in "-_" else "-"
        for char in f"{form_id}-{field_name}"
    )
    return f"err-{safe}"


def _apply_error_attrs(control: Any, error: str, error_id: str) -> Any:
    """给控件挂上 ``aria-invalid`` 与 ``aria-describedby``。"""
    control.attrs["aria-invalid"] = "true"
    control.attrs["aria-describedby"] = error_id
    return control


def render_field(
    field: Field,
    value: object | None,
    *,
    mode: str,
    error: str = "",
    form_id: str = "form",
) -> Any:
    """渲染单个字段（``label`` 包裹控件）。

    ``error`` 非空时，控件会被标记为 ``aria-invalid``，并在其下方渲染
    一条**字段级**错误说明（id 由 ``form_id`` + 字段名决定）。这是
    UX-03 的直接修复：校验失败要指到具体字段，而不是一句通用的
    「提交的值无效」。

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
    placeholder / rows 同理回落 ``edit_placeholder`` / ``edit_rows``；
    ``required`` / ``min`` / ``max`` 在编辑态由 ``edit_required`` /
    ``edit_min_value`` / ``edit_max_value`` 覆盖（三态语义见
    :class:`Field` 的配置项生效矩阵）。
    """
    label_text = (
        field.edit_label if mode == "edit" and field.edit_label else field.label
    )
    control = _control(field, value, mode=mode)
    if not error:
        return label(span(label_text), control)
    error_id = _error_id(field.name, form_id)
    _apply_error_attrs(control, error, error_id)
    return div(
        label(span(label_text), control),
        p(error, id=error_id, class_="field-error", role="alert"),
        class_="field field-invalid",
    )


def render_form(
    fields: Iterable[Field],
    *,
    mode: str,
    values: Mapping[str, object],
    csrf: str,
    action: str,
    submit_label: str,
    layout: str = "grid",
    actions: Any | None = None,
    errors: Mapping[str, str] | None = None,
    form_id: str = "form",
    error_summary: str = "",
) -> Any:
    """渲染整个表单。

    ``values`` 里的取值**按 kind 分述**（不是一条通用规则）：

    - **input 系**（``text`` / ``password`` / ``email`` / ``tel`` /
      ``number`` / ``url``）：值为 ``None`` ⇒ 控件**不带** value 属性；
      除 ``False`` 外（见下），其余取值都带上 value 属性。``False`` 走
      ludic 的 bool 分支，**HTML 上没有** ``value`` 属性（元素树里是
      ``"false"``），但语义上是「有这个值」；
    - **textarea**：取值落成**文本子节点**（不是 value 属性）；值为
      ``None`` ⇒ **没有**子节点；
    - **select**：取值只用于**匹配 option**（决定哪个 option 带
      ``selected``），**不产生** value 属性。

    另外 ``values.get()`` 分不清「键没给出」和「键给了但值是 ``None``」——
    两者都渲染成不带 value。

    ``layout`` 是约定值：``"grid"``（创建表单，双列栅格）与 ``"stack"``
    （编辑表单，纵向堆叠）。取值只影响外层 class。

    ``actions`` 是**字段之后、提交按钮之前**的一块可选节点（例如「拉取
    模型列表 / 测试连接」这类页面专有的辅助按钮）。它必须留在 ``form``
    内部，按钮才拿得到同一表单里的 ``base_url`` / ``api_key`` / ``model``；
    默认 ``None`` 时不渲染，其它页面的元素树不变。
    """
    field_errors = dict(errors or {})
    return form(
        _csrf(csrf),
        *(
            [
                div(
                    p(error_summary, class_="form-error-summary"),
                    class_="form-errors",
                    role="alert",
                )
            ]
            if error_summary
            else []
        ),
        *(
            render_field(
                field,
                values.get(field.name),
                mode=mode,
                error=field_errors.get(field.name, ""),
                form_id=form_id,
            )
            for field in fields
            if mode in field.modes
        ),
        *([actions] if actions is not None else []),
        button(submit_label, type="submit", class_="btn btn-primary"),
        action=action,
        method="post",
        class_="form-grid" if layout == "grid" else "stack",
    )
