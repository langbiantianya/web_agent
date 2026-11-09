"""平台超级管理员管理页。

只有 ``superAdmin`` 能增删这些无租户角色的平台账号，页面本身不做权限判定，
判定在路由层完成。
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
    label,
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

from app.models.admin_control import AdminPageDefinition, SuperAdminAccount
from services.admin_web.accounts import AdminPrincipal
from services.admin_web.views.shell import shell


def platform_admins_page(
    *,
    principal: AdminPrincipal,
    csrf: str,
    pages: Iterable[AdminPageDefinition],
    accounts: list[SuperAdminAccount],
    theme: str,
    color_mode: str,
    density: str,
    flash: str | None = None,
) -> Any:
    """渲染 owner 专属平台超级管理员管理页。"""
    rows = []
    for account in accounts:
        actions = (
            span("owner", class_="status-badge")
            if account.owner_slot == "superAdmin"
            else form(
                input_(type="hidden", name="csrf_token", value=csrf),
                button(
                    "Delete",
                    type="submit",
                    class_="btn btn-danger btn-small",
                ),
                action=f"/admin/platform-admins/{account.id}/delete",
                method="post",
                class_="inline-form",
            )
        )
        rows.append(
            tr(
                td(account.username),
                td(account.email or "—"),
                td(account.phone or "—"),
                td(account.status),
                td(account.password_expires_at or "—"),
                td(actions),
            )
        )
    return shell(
        title_text="Platform super admins",
        principal=principal,
        csrf=csrf,
        theme=theme,
        color_mode=color_mode,
        density=density,
        pages=pages,
        flash=flash,
        content=div(
            section(
                h1("Platform super admins"),
                p(
                    "Only superAdmin can add or delete these role-less platform "
                    "accounts. Passwords are generated and delivered automatically.",
                    class_="lead",
                ),
                form(
                    input_(type="hidden", name="csrf_token", value=csrf),
                    label(span("Username"), input_(name="username", required=True)),
                    label(
                        span("Email"),
                        input_(name="email", type="email"),
                    ),
                    label(
                        span("Phone"),
                        input_(name="phone", type="tel"),
                    ),
                    button(
                        "Create and send password",
                        type="submit",
                        class_="btn btn-primary",
                    ),
                    action="/admin/platform-admins",
                    method="post",
                    class_="form-grid",
                ),
                class_="surface",
            ),
            section(
                h2("Accounts"),
                div(
                    table(
                        thead(
                            tr(
                                th("Username"),
                                th("Email"),
                                th("Phone"),
                                th("Status"),
                                th("Password expires"),
                                th("Actions"),
                            )
                        ),
                        tbody(*rows),
                        class_="data-table",
                    ),
                    class_="table-scroll",
                ),
                class_="surface",
            ),
            class_="stack",
        ),
    )
