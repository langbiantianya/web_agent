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
    small,
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
from services.admin_web.views.confirm import confirm_dialog, delete_trigger
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
    controls: Any | None = None,
    footer: Any | None = None,
    undo: Any | None = None,
    panel: Any | None = None,
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
    if undo is not None:
        sections.append(section(undo, class_="surface undo-surface"))
    if controls is not None:
        sections.append(section(controls, class_="surface list-controls"))
    if create_form is not None:
        sections.append(section(h2("Create resource"), create_form, class_="surface"))
    if panel is not None:
        sections.append(panel)
    sections.append(table_section)
    if footer is not None:
        sections.append(section(footer, class_="surface list-footer"))
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
    label: str,
    toggle_disabled_reason: str = "",
    delete_disabled_reason: str = "",
    delete_title: str = "",
    delete_summary: str = "",
    delete_impacts: tuple[str, ...] = (),
) -> Any:
    """渲染详情页内通用的启用切换和删除按钮。

    删除按钮**不是提交按钮**——它只打开确认对话框（见
    :func:`services.admin_web.views.confirm.delete_trigger`）。对话框里
    才是真正的提交入口，避免「单击立即提交」把资源误删。

    ``delete_title`` / ``delete_summary`` / ``delete_impacts`` 是确认
    对话框要展示的内容：对象名、这次删除会发生什么、影响到谁。
    """
    if not can_write:
        return span("Read only", class_="status-badge")
    toggle_text = "Disable" if enabled else "Enable"
    locks = [
        reason
        for reason in (toggle_disabled_reason, delete_disabled_reason)
        if reason
    ]
    return div(
        form(
            _csrf(csrf),
            input_(type="hidden", name="enabled", value="0" if enabled else "1"),
            button(
                toggle_text,
                type="submit",
                class_="btn btn-secondary btn-small",
                disabled=bool(toggle_disabled_reason),
                # 同上：没有原因就不写 title，避免渲染出 title="None"。
                **(
                    {"title": toggle_disabled_reason}
                    if toggle_disabled_reason
                    else {}
                ),
                aria_label=f"{toggle_text} {label}",
            ),
            action=f"{prefix}/toggle",
            method="post",
            class_="inline-form",
        ),
        delete_trigger(
            csrf=csrf,
            action=f"{prefix}/delete",
            label=label,
            title=delete_title or f"Delete {label}",
            summary=delete_summary
            or f"Delete {label}? This action cannot be undone.",
            impacts=delete_impacts,
            disabled_reason=delete_disabled_reason,
        ),
        *(
            [small(" ".join(dict.fromkeys(locks)), class_="warning-text")]
            if locks
            else []
        ),
        class_="row-actions",
    )
