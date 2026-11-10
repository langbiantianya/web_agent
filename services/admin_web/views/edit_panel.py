"""独立编辑面板（UX-09）。

UX-09 记录的问题：行内 ``<details>`` 展开编辑，表单能长达整屏，多个行
可以同时展开，也没有深链接——想分享「正在编辑哪个对象」只能截图。

这里把编辑表单从表格行里挪出来，改成**页面顶部的独立面板**：

- 同一时刻**只有一个**编辑面板存在（面板由 URL 上的 ``?resource=code``
  决定），不会再出现多行同时展开的巨型页面；
- ``?resource=code`` 是**深链接**：把这个链接发给别人，对方打开就是
  同一个对象的编辑面板；
- 面板里有明确的「关闭」入口，回到列表不会丢上下文。

未保存离开保护（``beforeunload``）在 ``admin-ui.js`` 里实现，靠
``data-edit-panel`` 标记定位面板内的表单。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from ludic.html import a, h2, header, section


def edit_link(
    *,
    action: str,
    resource: str,
    label: str = "Edit",
) -> Any:
    """渲染一个指向编辑面板的深链接。"""
    separator = "&" if "?" in action else "?"
    return a(
        label,
        href=f"{action}{separator}resource={quote(resource, safe='')}",
        class_="btn btn-secondary btn-small",
        data_edit_link=resource,
        aria_label=f"{label} {resource}",
    )


def edit_panel(
    *,
    resource: str,
    title: str,
    form: Any,
    close_url: str,
) -> Any:
    """渲染独立编辑面板。"""
    return section(
        header(
            h2(title),
            a(
                "Close",
                href=close_url,
                class_="btn btn-secondary btn-small",
                data_edit_close=resource,
            ),
        ),
        form,
        id=f"edit-{quote(resource, safe='')}",
        class_="surface edit-panel",
        data_edit_panel=resource,
        aria_label=title,
    )


__all__ = ["edit_link", "edit_panel"]
