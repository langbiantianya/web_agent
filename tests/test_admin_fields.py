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


def test_edit_required_has_three_states() -> None:
    """``edit_required`` 是三态：继承 / 编辑态必填 / 编辑态显式不必填。

    ``None`` = 继承 ``required``；``True`` = 编辑态必填；``False`` =
    编辑态**显式不必填**（不渲染 ``required`` 属性）。

    ``required`` 只对 ``text`` 系与 ``textarea`` 生效，``select`` 忽略。
    """
    # 1) None ⇒ 继承 required：创建态与编辑态都必填。
    inherited = Field("name", "Name", required=True)
    assert "required" in render_field(inherited, "x", mode="create").to_html()
    assert "required" in render_field(inherited, "x", mode="edit").to_html()

    # 2) True ⇒ 编辑态必填（创建态不需要）。
    edit_only = Field("name", "Name", required=False, edit_required=True)
    assert "required" not in render_field(edit_only, "x", mode="create").to_html()
    assert "required" in render_field(edit_only, "x", mode="edit").to_html()

    # 3) False ⇒ 编辑态显式不必填（即便 required=True）。
    edit_never = Field("name", "Name", required=True, edit_required=False)
    assert "required" in render_field(edit_never, "x", mode="create").to_html()
    assert "required" not in render_field(edit_never, "x", mode="edit").to_html()

    # textarea 同样生效（Skill body 就是这么表达的）。
    textarea_edit_only = Field(
        "body", "Skill body", kind="textarea", edit_required=True
    )
    assert "required" not in render_field(textarea_edit_only, "x", mode="create").to_html()
    assert "required" in render_field(textarea_edit_only, "x", mode="edit").to_html()

    # select 忽略 required（既有规则不变）。
    select_field = Field(
        "status", "Status", kind="select", required=True, edit_required=True
    )
    assert "required" not in render_field(select_field, "x", mode="edit").to_html()


def test_edit_min_value_can_be_cancelled() -> None:
    """``edit_min_value=""`` 表示编辑态**显式取消** ``min`` 属性。

    三态：``None`` = 继承 ``min_value``；``""`` = 取消；其余 = 改用该值。
    """
    # 继承：创建态与编辑态都带 min。
    inherited = Field("priority", "Priority", kind="number", min_value="0")
    assert 'min="0"' in render_field(inherited, "1", mode="create").to_html()
    assert 'min="0"' in render_field(inherited, "1", mode="edit").to_html()

    # 显式取消：创建态带 min，编辑态不带。
    cancelled = Field(
        "priority",
        "Priority",
        kind="number",
        min_value="0",
        edit_min_value="",
    )
    assert 'min="0"' in render_field(cancelled, "1", mode="create").to_html()
    assert "min=" not in render_field(cancelled, "1", mode="edit").to_html()

    # 改用别的值。
    overridden = Field(
        "priority",
        "Priority",
        kind="number",
        min_value="0",
        edit_min_value="5",
    )
    assert 'min="0"' in render_field(overridden, "1", mode="create").to_html()
    assert 'min="5"' in render_field(overridden, "1", mode="edit").to_html()


def test_edit_max_value_can_be_cancelled() -> None:
    """``edit_max_value`` 与 ``edit_min_value`` 同语义。"""
    inherited = Field("timeout_sec", "Timeout", kind="number", max_value="600")
    assert 'max="600"' in render_field(inherited, "30", mode="edit").to_html()

    cancelled = Field(
        "timeout_sec",
        "Timeout",
        kind="number",
        max_value="600",
        edit_max_value="",
    )
    assert 'max="600"' in render_field(cancelled, "30", mode="create").to_html()
    assert "max=" not in render_field(cancelled, "30", mode="edit").to_html()


def test_agent_fields_cover_create_and_edit_without_duplication() -> None:
    """Agent 的字段只声明一次，创建与编辑共用。"""
    from services.admin_web.views.agents import AGENT_CREATE_FIELDS, AGENT_FIELDS

    edit_names = {f.name for f in AGENT_FIELDS}
    create_names = {f.name for f in AGENT_CREATE_FIELDS}
    assert edit_names <= create_names
    # code 只在创建时出现
    assert "code" not in edit_names
    assert "code" in create_names
    # api_key 的编辑态提示单独表达，而不是抄一份字段
    api_key = next(f for f in AGENT_FIELDS if f.name == "api_key")
    assert api_key.edit_placeholder == "Leave blank to keep current key"
    assert api_key.edit_label == "Replacement API key"


def test_agent_create_form_renders_code_but_edit_form_does_not() -> None:
    """接入后的实际渲染结果符合预期。"""
    from services.admin_web.views.agents import AGENT_CREATE_FIELDS, AGENT_FIELDS

    create_html = render_form(
        AGENT_CREATE_FIELDS,
        mode="create",
        values={},
        csrf="t",
        action="/admin/agents",
        submit_label="Create agent",
    ).to_html()
    edit_html = render_form(
        AGENT_FIELDS,
        mode="edit",
        values={},
        csrf="t",
        action="/admin/agents/x/update",
        submit_label="Save changes",
        layout="stack",
    ).to_html()
    assert 'name="code"' in create_html
    assert 'name="code"' not in edit_html
    assert "Create agent" in create_html
    assert "Save changes" in edit_html


def test_skill_fields_cover_create_and_edit_without_duplication() -> None:
    """Skill 的字段只声明一次，创建与编辑共用。"""
    from services.admin_web.views.skills import SKILL_CREATE_FIELDS, SKILL_FIELDS

    edit_names = {f.name for f in SKILL_FIELDS}
    create_names = {f.name for f in SKILL_CREATE_FIELDS}
    assert edit_names <= create_names
    assert "code" not in edit_names
    assert "code" in create_names
    # enabled 只在创建时出现
    assert "enabled" not in edit_names
    assert "enabled" in create_names
    # body 的编辑态行数与必填单独表达
    body = next(f for f in SKILL_FIELDS if f.name == "body")
    assert body.rows == 10
    assert body.edit_rows == 14
    assert body.required is False
    assert body.edit_required is True


def test_skill_create_form_renders_code_but_edit_form_does_not() -> None:
    """接入后的实际渲染结果符合预期。"""
    from services.admin_web.views.skills import SKILL_CREATE_FIELDS, SKILL_FIELDS

    create_html = render_form(
        SKILL_CREATE_FIELDS,
        mode="create",
        values={},
        csrf="t",
        action="/admin/skills",
        submit_label="Create skill",
    ).to_html()
    edit_html = render_form(
        SKILL_FIELDS,
        mode="edit",
        values={},
        csrf="t",
        action="/admin/skills/x/update",
        submit_label="Save skill",
        layout="stack",
    ).to_html()
    assert 'name="code"' in create_html
    assert 'name="code"' not in edit_html
    assert 'name="enabled"' in create_html
    assert 'name="enabled"' not in edit_html
    # body：创建态 10 行且不必填，编辑态 14 行且必填
    assert 'name="body" rows="10"' in create_html
    assert 'name="body" rows="14" required="required"' in edit_html


def test_mcp_fields_cover_create_and_edit_without_duplication() -> None:
    """MCP 的字段只声明一次，创建与编辑共用（顺序不同，各自显式）。"""
    from services.admin_web.views.mcp import MCP_CREATE_FIELDS, MCP_FIELDS

    edit_names = {f.name for f in MCP_FIELDS}
    create_names = {f.name for f in MCP_CREATE_FIELDS}
    assert edit_names <= create_names
    assert "code" not in edit_names
    assert "code" in create_names
    assert "transport" not in edit_names
    assert "transport" in create_names
    # timeout_sec 的编辑态显式取消 min / max
    timeout = next(f for f in MCP_FIELDS if f.name == "timeout_sec")
    assert timeout.min_value == "1"
    assert timeout.max_value == "600"
    assert timeout.edit_min_value == ""
    assert timeout.edit_max_value == ""


def test_mcp_create_form_renders_code_but_edit_form_does_not() -> None:
    """接入后的实际渲染结果符合预期。"""
    from services.admin_web.views.mcp import MCP_CREATE_FIELDS, MCP_FIELDS

    create_html = render_form(
        MCP_CREATE_FIELDS,
        mode="create",
        values={"timeout_sec": "30"},
        csrf="t",
        action="/admin/mcp",
        submit_label="Register server",
    ).to_html()
    edit_html = render_form(
        MCP_FIELDS,
        mode="edit",
        values={},
        csrf="t",
        action="/admin/mcp/x/update",
        submit_label="Save connection",
        layout="stack",
    ).to_html()
    assert 'name="code"' in create_html
    assert 'name="code"' not in edit_html
    assert 'name="transport"' in create_html
    assert 'name="transport"' not in edit_html
    # 创建态带 min / max，编辑态不带
    assert 'min="1"' in create_html
    assert 'max="600"' in create_html
    assert "min=" not in edit_html
    assert "max=" not in edit_html


def test_llm_fields_cover_create_and_edit_without_duplication() -> None:
    """LLM 的字段只声明一次，创建与编辑共用（顺序不同，各自显式）。"""
    from services.admin_web.views.llm import LLM_CREATE_FIELDS, LLM_FIELDS

    edit_names = {f.name for f in LLM_FIELDS}
    create_names = {f.name for f in LLM_CREATE_FIELDS}
    assert edit_names <= create_names
    assert "code" not in edit_names
    assert "code" in create_names
    # base_url：创建态必填，编辑态显式不必填
    base_url = next(f for f in LLM_FIELDS if f.name == "base_url")
    assert base_url.required is True
    assert base_url.edit_required is False
    # priority：编辑态显式取消 min
    priority = next(f for f in LLM_FIELDS if f.name == "priority")
    assert priority.min_value == "0"
    assert priority.edit_min_value == ""


def test_llm_create_form_renders_code_but_edit_form_does_not() -> None:
    """接入后的实际渲染结果符合预期。"""
    from services.admin_web.views.llm import LLM_CREATE_FIELDS, LLM_FIELDS

    create_html = render_form(
        LLM_CREATE_FIELDS,
        mode="create",
        values={"priority": "100"},
        csrf="t",
        action="/admin/llm",
        submit_label="Create endpoint",
    ).to_html()
    edit_html = render_form(
        LLM_FIELDS,
        mode="edit",
        values={},
        csrf="t",
        action="/admin/llm/x/update",
        submit_label="Save endpoint",
        layout="stack",
    ).to_html()
    assert 'name="code"' in create_html
    assert 'name="code"' not in edit_html
    # base_url：创建态 required，编辑态没有
    assert 'name="base_url" required="required"' in create_html
    assert 'name="base_url" required' not in edit_html
    # priority：创建态带 min，编辑态不带
    assert 'min="0"' in create_html
    assert "min=" not in edit_html


def test_field_order_is_explicit_for_every_resource() -> None:
    """字段顺序是渲染结构的一部分，必须逐资源显式钉住。

    渲染快照比的是元素树，**children 的顺序变了就是差异**。llm 与 mcp 的
    创建态与编辑态顺序不同，所以两个顺序都要写死。
    """
    from services.admin_web.views.agents import AGENT_CREATE_FIELDS, AGENT_FIELDS
    from services.admin_web.views.llm import LLM_CREATE_FIELDS, LLM_FIELDS
    from services.admin_web.views.mcp import MCP_CREATE_FIELDS, MCP_FIELDS
    from services.admin_web.views.skills import SKILL_CREATE_FIELDS, SKILL_FIELDS

    assert [f.name for f in AGENT_FIELDS] == [
        "name",
        "model",
        "base_url",
        "api_key",
        "tools",
        "skills",
        "system_prompt",
    ]
    assert [f.name for f in AGENT_CREATE_FIELDS] == [
        "code",
        "name",
        "model",
        "base_url",
        "api_key",
        "tools",
        "skills",
        "system_prompt",
        "enabled",
    ]
    assert [f.name for f in SKILL_FIELDS] == ["description", "status", "body"]
    assert [f.name for f in SKILL_CREATE_FIELDS] == [
        "code",
        "description",
        "status",
        "body",
        "enabled",
    ]
    assert [f.name for f in MCP_FIELDS] == [
        "name",
        "timeout_sec",
        "command",
        "url",
    ]
    assert [f.name for f in MCP_CREATE_FIELDS] == [
        "code",
        "name",
        "transport",
        "url",
        "command",
        "timeout_sec",
    ]
    assert [f.name for f in LLM_FIELDS] == ["name", "base_url", "priority", "api_key"]
    assert [f.name for f in LLM_CREATE_FIELDS] == [
        "code",
        "name",
        "base_url",
        "api_key",
        "priority",
    ]


def test_edit_overrides_are_absent_from_element_tree() -> None:
    """三态覆盖项必须在**元素树**层区分「属性不存在」与「属性是假值」。

    HTML 层区分不了这两者：``<input name="x">`` 既可能是「压根不发
    required 属性」，也可能是「发了 required 但值是假值」，渲染出的
    HTML 完全一样。但元素树不同：``{'name': 'x'}`` vs
    ``{'name': 'x', 'required': 'false'}``——渲染快照比的是元素树，所以
    这层形态必须单独锁住。

    （本轮只追加用例，不改既有的三态用例。）
    """

    def tree_attrs(element: object) -> dict[str, str]:
        """控件（``render_field`` 返回值的第二个子节点）的元素树属性。"""
        return element.children[1].aliased_attrs  # type: ignore[attr-defined]

    # 1) edit_required=False ⇒ 树里**没有** required 键（不是 "false"）。
    never = render_field(
        Field("name", "Name", required=True, edit_required=False), "x", mode="edit"
    )
    assert tree_attrs(never) == {"name": "name", "value": "x"}
    assert "required" not in tree_attrs(never)

    # 2) edit_required=True ⇒ 树里是 required: "true"。
    edit_only = render_field(
        Field("name", "Name", required=False, edit_required=True), "x", mode="edit"
    )
    assert tree_attrs(edit_only)["required"] == "true"

    # textarea 同样成立（Skill body 就是这么表达的）。
    area = render_field(
        Field("body", "Body", kind="textarea", edit_required=True), "x", mode="edit"
    )
    assert tree_attrs(area)["required"] == "true"

    # 3) edit_min_value="" ⇒ 树里**没有** min 键（创建态仍有）。
    min_field = Field(
        "priority", "Priority", kind="number", min_value="0", edit_min_value=""
    )
    min_edit = render_field(min_field, "1", mode="edit")
    min_create = render_field(min_field, "1", mode="create")
    assert "min" not in tree_attrs(min_edit)
    assert tree_attrs(min_create)["min"] == "0"

    # 4) edit_max_value="" ⇒ 树里**没有** max 键（创建态仍有）。
    max_field = Field(
        "timeout_sec", "Timeout", kind="number", max_value="600", edit_max_value=""
    )
    max_edit = render_field(max_field, "30", mode="edit")
    max_create = render_field(max_field, "30", mode="create")
    assert "max" not in tree_attrs(max_edit)
    assert tree_attrs(max_create)["max"] == "600"


def test_agent_edit_form_renders_empty_system_prompt_for_none() -> None:
    """``system_prompt=None`` 必须渲染成**空内容**，不是字面量 ``"None"``。

    这是 Task 8 唯一被声明的**行为修正**：旧实现写的是
    ``textarea(row.system_prompt, ...)``，ludic 会把 ``None`` 渲染成
    字面量 ``"None"`` 的文本内容；现在 ``row.system_prompt or ""`` 让内容
    为空。ORM 里该列是 ``nullable=False``（真实环境不可达），但这条差异
    值得一条测试钉住。
    """
    from types import SimpleNamespace

    from app.core.admin_auth import AdminPrincipal
    from services.admin_web.views.agents import agents_page

    row = SimpleNamespace(
        code="agent-none-prompt",
        name="Agent None Prompt",
        model="gpt-4.1-mini",
        base_url=None,
        tools=[],
        skills=[],
        system_prompt=None,
        enabled=True,
        version=1,
    )
    html = agents_page(
        principal=AdminPrincipal(
            kind="platform_super_admin",
            principal_id=1,
            user_id=None,
            tenant_id=None,
            username="superAdmin",
            session_id=1,
            is_owner=True,
        ),
        csrf="csrf-token",
        pages=[],
        theme="fluent",
        color_mode="light",
        density="comfortable",
        rows=[row],
        can_write=True,
    ).to_html()

    match = re.search(
        r'<textarea[^>]*name="system_prompt"[^>]*>(.*?)</textarea>', html, re.DOTALL
    )
    assert match is not None, "agents 编辑表单里没有 system_prompt 的 textarea"
    content = match.group(1)
    assert content == ""
    assert "None" not in content
