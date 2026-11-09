"""通用 UI 组件。

这些是**多个页面共用**的片段：指标卡、CSRF 隐藏域、启停与删除按钮组、
「标题 + 创建表单 + 数据表」的业务页骨架。抽出来是为了让页面函数只写
「这个页面有什么」，不重复「每个管理页都长什么样」。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ludic.html import (
    button,
    div,
    form,
    h1,
    h2,
    input as input_,
    p,
    section,
    span,
    table,
    tbody,
    td,
    th,
    thead,
    tr,
)

from app.models.admin_control import AdminPageDefinition
from services.admin_web.accounts import AdminPrincipal
from services.admin_web.views.shell import shell


def _metric(label_text: str, value: int) -> Any:
    """渲染概览指标卡。"""
    return div(
        span(str(value), class_="metric-value"),
        span(label_text, class_="metric-label"),
        class_="metric-card",
    )


def _management_page(
    *,
    title_text: str,
    lead: str,
    principal: AdminPrincipal,
    csrf: str,
    pages: Iterable[AdminPageDefinition],
    theme: str,
    color_mode: str,
    density: str,
    create_form: Any | None,
    headers: list[str],
    rows: list[Any],
    flash: str | None = None,
    flash_kind: str = "info",
    notes: Iterable[Any] = (),
) -> Any:
    """渲染业务管理页共用的标题、创建表单和数据表。

    ``notes`` 是标题下方、创建表单上方的**页面级补充说明**（默认空，
    不渲染任何节点）。它是页面自己的内容，只有需要「在标题区交代协议、
    预设或前置条件」的页面才传；传空元组的页面元素树与以前完全一致。
    """
    table_section = section(
        h2("Configured resources"),
        div(
            table(
                thead(tr(*(th(item) for item in headers))),
                tbody(*(rows or [tr(td("No resources configured."))])),
                class_="data-table",
            ),
            class_="table-scroll",
        ),
        class_="surface",
    )
    sections = []
    if create_form is not None:
        sections.append(section(h2("Create resource"), create_form, class_="surface"))
    sections.append(table_section)
    return shell(
        title_text=title_text,
        principal=principal,
        csrf=csrf,
        theme=theme,
        color_mode=color_mode,
        density=density,
        pages=pages,
        flash=flash,
        flash_kind=flash_kind,
        content=div(
            section(h1(title_text), p(lead, class_="lead"), *notes),
            *sections,
            class_="stack",
        ),
    )


def _csrf(csrf: str) -> Any:
    """返回后台表单共用的 CSRF 隐藏字段。"""
    return input_(type="hidden", name="csrf_token", value=csrf)


def _toggle_and_delete(
    *,
    csrf: str,
    prefix: str,
    enabled: bool,
    can_write: bool,
) -> Any:
    """渲染详情页内通用的启用切换和删除按钮。"""
    if not can_write:
        return span("Read only", class_="status-badge")
    return div(
        form(
            _csrf(csrf),
            input_(type="hidden", name="enabled", value="0" if enabled else "1"),
            button(
                "Disable" if enabled else "Enable",
                type="submit",
                class_="btn btn-secondary btn-small",
            ),
            action=f"{prefix}/toggle",
            method="post",
            class_="inline-form",
        ),
        form(
            _csrf(csrf),
            button(
                "Delete",
                type="submit",
                class_="btn btn-danger btn-small",
            ),
            action=f"{prefix}/delete",
            method="post",
            class_="inline-form",
        ),
        class_="row-actions",
    )
