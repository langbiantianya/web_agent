"""删除确认对话框与撤销入口的共用片段。

删除是不可逆操作，所以「单击立即提交」不够：用户点错一次就可能让
正在运行的工作流失效。这里提供两件东西：

## 确认对话框

一个**页面级**的原生 ``<dialog>``：页面里只渲染一个，行内的删除按钮
只是触发器（``data_confirm_trigger``）。触发器上带着对象名与影响摘要，
JS 负责把它们搬进对话框并弹出。

- 标题写明**对象名**（"Delete Agent e2e-order"），避免「删的是谁」不清；
- 正文给出**影响摘要**（被几个工作流引用、删掉会怎样）；
- 按钮文案是「Cancel」/「Delete」，不是中性的「OK」；
- 原生 ``<dialog>`` 自带焦点陷阱与 Esc 关闭，JS 只做搬运。

## 不用 ``window.confirm``

``window.confirm`` 无法呈现影响摘要，也无法做样式化布局；更重要的是
它会阻塞整个渲染进程，截图与自动化测试都难处理。

## 撤销入口

删除成功后服务端会把这一行的快照留存一小段时间（见
:mod:`services.admin_web.deletion`），列表页据此渲染一个「撤销」
按钮——点一下就能把资源原样恢复，不必凭记忆重填。
"""

from __future__ import annotations

from typing import Any

from ludic.html import (
    button,
    div,
    form,
    h2,
    input as input_,
    p,
    section,
    small,
    span,
)


def confirm_dialog() -> Any:
    """渲染页面级的删除确认对话框。

    对话框内容是**空壳**：标题、正文与影响摘要在弹出时由 JS 从触发
    按钮的 data 属性填入。页面里只需要一个实例。
    """
    return section(
        div(
            h2("", id="confirm-dialog-title"),
            p("", class_="lead", data_confirm_dialog_summary="1"),
            div("", class_="confirm-impacts", data_confirm_dialog_impacts="1"),
            form(
                input_(type="hidden", name="csrf_token", value=""),
                button(
                    "Cancel",
                    type="button",
                    class_="btn btn-secondary",
                    data_confirm_dialog_close="1",
                ),
                button(
                    "Delete",
                    type="submit",
                    class_="btn btn-danger",
                    data_confirm_dialog_submit="1",
                ),
                action="",
                method="post",
                class_="confirm-actions",
            ),
            class_="confirm-body",
        ),
        class_="confirm-dialog",
        role="dialog",
        aria_modelledby="confirm-dialog-title",
        data_confirm_dialog="1",
    )


def delete_trigger(
    *,
    csrf: str,
    action: str,
    label: str,
    title: str,
    summary: str,
    impacts: tuple[str, ...] = (),
    disabled_reason: str = "",
    button_label: str = "Delete",
    accessible_label: str = "",
) -> Any:
    """渲染「打开删除确认对话框」的触发按钮。

    触发按钮**不是提交按钮**：它只负责打开对话框。对话框里才是真正的
    提交入口。于是「单击立即提交」变成「单击 → 阅读影响摘要 → 确认」。

    按钮仍带着自己的表单（action + CSRF），对话框的 JS 从最近的表单
    里取这两个值填进对话框的表单——这样删除请求的构造方式与直接提交
    完全一致，CSRF 校验也走同一条路径。

    ``disabled_reason`` 非空时按钮禁用（并把原因写进 ``title``），
    用于「这个资源正被引用，删不掉」这类行内锁定。
    """
    return form(
        input_(type="hidden", name="csrf_token", value=csrf),
        button(
            button_label,
            type="button",
            class_="btn btn-danger btn-small",
            disabled=bool(disabled_reason),
            # 没有禁用原因时**不要**写 title 属性：ludic 会把 Python 的
            # ``None`` 渲染成字符串 "None"，页面上就会出现 title="None"。
            **({"title": disabled_reason} if disabled_reason else {}),
            aria_label=accessible_label or f"Delete {label}",
            data_confirm_trigger="1",
            data_confirm_title=title,
            data_confirm_summary=summary,
            data_confirm_impacts="\n".join(impacts),
        ),
        action=action,
        method="post",
        class_="inline-form",
    )


def undo_panel(
    *,
    csrf: str,
    undo_id: int,
    resource_label: str,
) -> Any:
    """渲染删除成功后的「撤销」入口。"""
    return div(
        p(
            f"{resource_label} was deleted.",
            class_="block-muted",
        ),
        form(
            input_(type="hidden", name="csrf_token", value=csrf),
            button(
                "Undo delete",
                type="submit",
                class_="btn btn-secondary btn-small",
                aria_label=f"Undo delete {resource_label}",
            ),
            action=f"/admin/undo/{undo_id}",
            method="post",
            class_="inline-form",
        ),
        class_="undo-panel",
        role="status",
        data_undo_panel="1",
    )


__all__ = ["confirm_dialog", "delete_trigger", "undo_panel"]
