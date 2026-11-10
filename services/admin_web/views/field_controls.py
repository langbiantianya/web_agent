"""表单控件的**私有**渲染补丁。

ludic 1.x 的 ``format_attrs`` 只保留非空值，空字符串属性会被整个丢掉，
于是 ``value=""`` 与「压根没有 value 属性」渲染出的 HTML 完全一样；空的
``option`` 值也会被浏览器当成「显示文案即提交值」。

这里放两个子类，把被丢掉的空 ``value`` 补回 HTML 文本层。它们是**私有**
实现细节，单独成模块是为了让 ``fields.py`` 保持在行数上限内。
"""

from __future__ import annotations

from ludic.html import input as input_, option


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


class _Option(option):
    """保留空字符串 option 值，避免浏览器把显示文案当成提交值。"""

    def _format_attributes(self, is_html: bool = False) -> str:
        rendered = super()._format_attributes(is_html=is_html)
        if self.attrs.get("value") == "":
            rendered = f'{rendered} value=""' if rendered else 'value=""'
        return rendered


__all__ = ["_Input", "_Option"]
