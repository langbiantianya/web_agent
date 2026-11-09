"""数据驱动的表单字段。"""

from __future__ import annotations

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

    现有表单里 ``input_(name="tools")`` 渲染成 ``<input name="tools">``，
    而不是 ``<input name="tools" value="">``——两者在 HTML 上不等价，
    渲染快照会判为差异。所以「有没有值」必须由调用方显式决定。
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
