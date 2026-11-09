"""角色与 OAuth 声明映射页。

Casbin 规则展示与「OAuth 外部声明 → 租户角色」的映射配置放在一个模块里：
两者都是**角色**语义，映射配置最终落到 Casbin 角色上。
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
    option,
    p,
    section,
    select,
    span,
    table,
    tbody,
    td,
    th,
    thead,
    tr,
)

from app.models.admin_control import AdminPageDefinition, OAuthRoleMapping
from app.models.oauth import OAuthProvider
from services.admin_web.accounts import AdminPrincipal
from services.admin_web.views.shell import shell


def roles_page(
    *,
    principal: AdminPrincipal,
    csrf: str,
    pages: Iterable[AdminPageDefinition],
    rules: list[Any],
    theme: str,
    color_mode: str,
    density: str,
) -> Any:
    """展示 Casbin p/g 规则。"""
    rows = [
        tr(
            td(rule.ptype),
            td(rule.v0 or ""),
            td(rule.v1 or ""),
            td(rule.v2 or ""),
            td(rule.v3 or ""),
        )
        for rule in rules
    ]
    return shell(
        title_text="Casbin roles",
        principal=principal,
        csrf=csrf,
        theme=theme,
        color_mode=color_mode,
        density=density,
        pages=pages,
        content=section(
            h1("Casbin policy"),
            p(
                "OAuth administrators are authorized through Casbin. Platform "
                "super admins bypass role checks.",
                class_="lead",
            ),
            div(
                table(
                    thead(tr(th("Type"), th("v0"), th("v1"), th("v2"), th("v3"))),
                    tbody(*rows),
                    class_="data-table",
                ),
                class_="table-scroll",
            ),
            class_="surface",
        ),
    )


def mappings_page(
    *,
    principal: AdminPrincipal,
    csrf: str,
    pages: Iterable[AdminPageDefinition],
    mappings: list[OAuthRoleMapping],
    providers: list[OAuthProvider],
    theme: str,
    color_mode: str,
    density: str,
    flash: str | None = None,
) -> Any:
    """渲染 OAuth 外部角色映射。"""
    options = [
        option(provider.display_name, value=str(provider.id)) for provider in providers
    ]
    rows = [
        tr(
            td(str(row.provider_id)),
            td(row.claim_name),
            td(row.external_value),
            td(row.role_code),
            td("enabled" if row.enabled else "disabled"),
        )
        for row in mappings
    ]
    return shell(
        title_text="OAuth role mappings",
        principal=principal,
        csrf=csrf,
        theme=theme,
        color_mode=color_mode,
        density=density,
        pages=pages,
        flash=flash,
        content=div(
            section(
                h1("OAuth role mappings"),
                p(
                    "Only exact external values are accepted. Mapping changes apply "
                    "on the administrator's next OAuth synchronization.",
                    class_="lead",
                ),
                form(
                    input_(type="hidden", name="csrf_token", value=csrf),
                    label(span("Provider"), select(*options, name="provider_id")),
                    label(
                        span("Claim"),
                        input_(name="claim_name", value="groups", required=True),
                    ),
                    label(
                        span("External role"),
                        input_(name="external_value", required=True),
                    ),
                    label(
                        span("Casbin role"),
                        input_(
                            name="role_code",
                            list="role-codes",
                            required=True,
                        ),
                    ),
                    button("Add mapping", type="submit", class_="btn btn-primary"),
                    action="/admin/access/mappings",
                    method="post",
                    class_="form-grid",
                ),
                class_="surface",
            ),
            section(
                h2("Current mappings"),
                div(
                    table(
                        thead(
                            tr(
                                th("Provider"),
                                th("Claim"),
                                th("External value"),
                                th("Role"),
                                th("Status"),
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
