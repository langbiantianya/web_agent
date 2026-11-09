"""数据驱动的表单字段。"""

from __future__ import annotations

import re

from services.admin_web.views.fields import Field, render_field, render_form


def test_field_renders_text_input() -> None:
    element = render_field(Field("name", "Name", required=True), "abc", mode="edit")
    html = element.to_html()
    assert 'name="name"' in html
    assert 'value="abc"' in html
    assert "required" in html
    assert "Name" in html


def test_field_renders_password_without_value_leak() -> None:
    element = render_field(
        Field("api_key", "API key", kind="password"), "", mode="edit"
    )
    html = element.to_html()
    assert 'type="password"' in html
    assert 'name="api_key"' in html


def test_field_renders_textarea_with_rows() -> None:
    element = render_field(
        Field("prompt", "Prompt", kind="textarea", rows=10), "hi", mode="create"
    )
    html = element.to_html()
    assert "<textarea" in html
    assert 'rows="10"' in html


def test_field_renders_select_with_options() -> None:
    element = render_field(
        Field("enabled", "Enabled", kind="select", options=(("Yes", "1"), ("No", "0"))),
        "1",
        mode="create",
    )
    html = element.to_html()
    assert "<select" in html
    assert 'value="1"' in html
    assert 'value="0"' in html


def test_field_passes_through_autocomplete_and_bounds() -> None:
    """autocomplete / min / max 必须能表达，否则密码框与数字框无法原样还原。"""
    element = render_field(
        Field(
            "api_key",
            "API key",
            kind="password",
            autocomplete="new-password",
        ),
        None,
        mode="create",
    )
    assert 'autocomplete="new-password"' in element.to_html()

    number = render_field(
        Field("timeout_sec", "Timeout", kind="number", min_value="1", max_value="600"),
        None,
        mode="create",
    ).to_html()
    assert 'type="number"' in number
    assert 'min="1"' in number
    assert 'max="600"' in number


def test_absent_value_renders_no_value_attribute() -> None:
    """值缺失时**不**渲染 ``value=""``。

    ludic 1.0.3 会丢掉空字符串属性，所以「空 value」与「没有 value」在
    **元素树**上完全一样——渲染快照比对的是元素树，守不到这层差别。组件的
    ``_Input`` 只在 HTML 文本层补回 ``value=""``，所以这里断言的是 HTML：
    值缺失时不带 ``value``，值是空字符串时带上 ``value=""``。

    Task 8 接入真实表单后，这层差别要人工逐字核对 HTML。
    """
    element = render_field(Field("tools", "Tools"), None, mode="create")
    assert "value=" not in element.to_html()

    with_value = render_field(Field("tools", "Tools"), "", mode="edit")
    assert 'value=""' in with_value.to_html()


def test_edit_mode_uses_edit_placeholder_and_rows() -> None:
    """编辑态用 edit_placeholder / edit_rows，创建态用默认值。"""
    field = Field(
        "api_key",
        "API key",
        kind="password",
        placeholder="创建时的提示",
        edit_placeholder="Leave blank to keep current key",
        rows=8,
        edit_rows=10,
    )
    edit_html = render_field(field, "", mode="edit").to_html()
    create_html = render_field(field, "", mode="create").to_html()
    assert "Leave blank to keep current key" in edit_html
    assert "创建时的提示" in create_html

    textarea_field = Field("prompt", "Prompt", kind="textarea", rows=8, edit_rows=10)
    assert 'rows="10"' in render_field(textarea_field, "", mode="edit").to_html()
    assert 'rows="8"' in render_field(textarea_field, "", mode="create").to_html()


def test_edit_mode_uses_edit_label() -> None:
    """编辑态用 edit_label，创建态用 label。

    既有表单里 API key 的标签在编辑态是「Replacement API key」、创建态是
    「API key」——差别必须能表达，否则只能把字段抄两遍。
    """
    field = Field(
        "api_key",
        "API key",
        kind="password",
        edit_label="Replacement API key",
    )
    edit_html = render_field(field, "", mode="edit").to_html()
    create_html = render_field(field, "", mode="create").to_html()
    assert "Replacement API key" in edit_html
    assert "Replacement API key" not in create_html
    assert "API key" in create_html


def test_select_marks_current_value_as_selected() -> None:
    """编辑态的 select 必须标出当前选中项，否则保存会把值改掉。

    既有表单用 ``option(..., selected=...)``，True 与 False 都显式传；
    创建态没有当前值，一个 selected 都不发。
    """
    field = Field(
        "status",
        "Status",
        kind="select",
        options=(("Published", "published"), ("Draft", "draft")),
    )
    edit_html = render_field(field, "draft", mode="edit").to_html()
    assert re.findall(r'<option value="([^"]+)"([^>]*)>', edit_html) == [
        ("published", ""),
        ("draft", ' selected="selected"'),
    ]

    create_html = render_field(field, None, mode="create").to_html()
    assert "selected" not in create_html


def test_boolean_value_uses_ludic_bool_branch() -> None:
    """bool 值不能变成 ``value="False"``，要走 ludic 的 bool 分支。

    既有表单里 ``input_(name="x", value=False)`` 渲染成 ``<input name="x">``，
    ``value=True`` 渲染成 ``<input name="x" value="value">``。
    """
    false_html = render_field(Field("flag", "Flag"), False, mode="edit").to_html()
    assert "value=" not in false_html

    true_html = render_field(Field("flag", "Flag"), True, mode="edit").to_html()
    assert 'value="value"' in true_html


def _option_attrs(element: object) -> list[dict[str, str]]:
    """取渲染结果里 select 各 option 的**元素树**属性。

    ``element`` 是 ``render_field`` 的返回值（``label`` 包裹控件），第二层
    子节点才是 ``select``。
    """
    control = element.children[1]  # type: ignore[attr-defined]
    return [child.aliased_attrs for child in control.children]  # type: ignore[attr-defined]


def test_select_option_tree_carries_explicit_selected_flags() -> None:
    """**元素树**层必须两个 option 都带 ``selected``（一真一假）。

    HTML 层区分不了「显式 ``selected=False``」与「压根不发 selected」
    （两者渲染出的 HTML 完全相同），但元素树不同：
    ``{'value': 'published', 'selected': 'false'}`` vs
    ``{'value': 'published'}``。渲染快照基线比的是元素树，所以这层形态
    必须单独锁住。

    既有表单写的是 ``option(..., selected=...)``，True 与 False 都显式传；
    组件必须逐层一致。
    """
    field = Field(
        "status",
        "Status",
        kind="select",
        options=(("Published", "published"), ("Draft", "draft")),
    )

    edit_attrs = _option_attrs(render_field(field, "draft", mode="edit"))
    assert edit_attrs == [
        {"value": "published", "selected": "false"},
        {"value": "draft", "selected": "true"},
    ]

    # 创建态没有当前值：一个 selected 都不发（属性整个缺失，不是 'false'）。
    create_attrs = _option_attrs(render_field(field, None, mode="create"))
    assert create_attrs == [
        {"value": "published"},
        {"value": "draft"},
    ]


def test_boolean_select_normalizes_to_legacy_option_values() -> None:
    """布尔当前值必须命中 option，否则会退化成「浏览器默认选中第一项」。

    既有表单（``views_legacy.py``）的布尔型下拉一律是
    ``option("Yes", value="1", selected=bool(x))`` /
    ``option("No", value="0", selected=not bool(x))``——option 的值是字符串
    ``"1"`` / ``"0"``。组件把 bool 归一后与之比较。

    不归一的话 ``True`` 会变成 ``"True"``，与 ``"1"`` 不相等 ⇒ 零个
    selected ⇒ 浏览器默认选中第一项（本该是 False 的字段被存成 True）。
    """
    field = Field(
        "enabled",
        "Enabled",
        kind="select",
        options=(("Yes", "1"), ("No", "0")),
    )

    true_attrs = _option_attrs(render_field(field, True, mode="edit"))
    assert true_attrs == [
        {"value": "1", "selected": "true"},
        {"value": "0", "selected": "false"},
    ]

    false_attrs = _option_attrs(render_field(field, False, mode="edit"))
    assert false_attrs == [
        {"value": "1", "selected": "false"},
        {"value": "0", "selected": "true"},
    ]

    # HTML 层也要与既有写法一致：命中项带 selected="selected"，
    # 未命中项不带（但元素树里仍然有 'selected': 'false'）。
    true_html = render_field(field, True, mode="edit").to_html()
    assert '<option value="1" selected="selected">Yes</option>' in true_html
    assert '<option value="0">No</option>' in true_html


def test_form_skips_fields_not_in_mode() -> None:
    """modes 不含当前模式的字段不渲染。"""
    fields = [
        Field("code", "Code", required=True, modes=("create",)),
        Field("name", "Name", required=True),
    ]
    create_html = render_form(
        fields, mode="create", values={}, csrf="t", action="/x", submit_label="创建"
    ).to_html()
    edit_html = render_form(
        fields, mode="edit", values={}, csrf="t", action="/x", submit_label="保存"
    ).to_html()
    assert 'name="code"' in create_html
    assert 'name="code"' not in edit_html
    assert 'name="name"' in edit_html


def test_form_contains_csrf_action_and_submit_label() -> None:
    element = render_form(
        [Field("name", "Name")],
        mode="create",
        values={},
        csrf="token-abc",
        action="/admin/agents",
        submit_label="Create agent",
        layout="grid",
    )
    html = element.to_html()
    assert 'name="csrf_token"' in html
    assert 'value="token-abc"' in html
    assert 'action="/admin/agents"' in html
    assert "Create agent" in html
    assert "form-grid" in html


def test_form_stack_layout() -> None:
    element = render_form(
        [Field("name", "Name")],
        mode="edit",
        values={},
        csrf="t",
        action="/x",
        submit_label="Save",
        layout="stack",
    )
    assert "stack" in element.to_html()
