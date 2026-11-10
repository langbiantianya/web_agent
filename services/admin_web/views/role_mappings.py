"""OAuth 外部角色映射页面。"""

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
    small,
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
from services.admin_web.views.confirm import delete_trigger
from services.admin_web.views.role_cards import domain_datalist
from services.admin_web.views.shell import shell


def mappings_page(
    *,
    principal: AdminPrincipal,
    csrf: str,
    pages: Iterable[AdminPageDefinition],
    mappings: list[OAuthRoleMapping],
    providers: list[OAuthProvider],
    role_codes: list[str] | None = None,
    theme: str,
    color_mode: str,
    density: str,
    flash: str | None = None,
    flash_kind: str = "info",
) -> Any:
    """渲染可新增、修改、启停和删除的 OAuth 角色映射。"""
    options = [
        option(
            f"{provider.display_name} · {getattr(provider, 'tenant_id', '')}",
            value=str(provider.id),
        )
        for provider in providers
    ]
    provider_names = {provider.id: provider.display_name for provider in providers}
    rows = []
    for row in mappings:
        provider_name = provider_names.get(row.provider_id, f"#{row.provider_id}")
        mapping_id = getattr(row, "id", 0)
        rows.append(
            tr(
                td(provider_name, small(f"provider #{row.provider_id}", class_="block-muted")),
                td(row.claim_name),
                td(row.external_value),
                td(
                    form(
                        input_(type="hidden", name="csrf_token", value=csrf),
                        div(
                            input_(
                                name="role_code",
                                value=row.role_code,
                                list="mapping-role-codes",
                                aria_label=f"Role for {row.external_value}",
                                required=True,
                            ),
                            label(
                                input_(
                                    type="checkbox",
                                    name="enabled",
                                    value="1",
                                    checked=bool(row.enabled),
                                ),
                                span("Enabled"),
                                class_="permission-choice compact",
                            ),
                            button("Save", type="submit", class_="btn btn-small"),
                            class_="mapping-actions",
                        ),
                        action=f"/admin/access/mappings/{mapping_id}/update",
                        method="post",
                        class_="mapping-edit-form",
                    )
                ),
                td(
                    delete_trigger(
                        csrf=csrf,
                        action=f"/admin/access/mappings/{mapping_id}/delete",
                        label=f"mapping {row.external_value}",
                        title=f"Delete mapping {row.external_value}",
                        summary="Future OAuth synchronizations stop granting this role.",
                        impacts=(
                            f"Provider: {provider_name}",
                            f"External value: {row.external_value}",
                            f"Role: {row.role_code}",
                        ),
                    )
                ),
            )
        )
    return shell(
        title_text="OAuth role mappings",
        principal=principal,
        csrf=csrf,
        theme=theme,
        color_mode=color_mode,
        density=density,
        pages=pages,
        flash=flash,
        flash_kind=flash_kind,
        content=div(
            section(
                p("IDENTITY MAPPING", class_="eyebrow"),
                h1("OAuth role mappings"),
                p(
                    "Map exact external claim values to managed roles. Updates apply "
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
                            list="mapping-role-codes",
                            required=True,
                        ),
                    ),
                    button("Add mapping", type="submit", class_="btn btn-primary"),
                    action="/admin/access/mappings",
                    method="post",
                    class_="form-grid",
                ),
                domain_datalist("mapping-role-codes", role_codes or []),
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
                                th("Role and status"),
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
