"""OAuth Provider 配置页。

客户端用户与管理员共用同一套 Provider 配置，页面用
``views/components.py`` 的通用骨架渲染。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ludic.html import (
    button,
    details,
    form,
    input as input_,
    label,
    option,
    select,
    small,
    span,
    summary,
    td,
    tr,
)

from app.models.admin_control import AdminPageDefinition
from app.models.oauth import OAuthProvider
from services.admin_web.accounts import AdminPrincipal
from services.admin_web.views.components import (
    _csrf,
    _management_page,
    _toggle_and_delete,
)


def _provider_form(
    *,
    csrf: str,
    provider: OAuthProvider | None = None,
    create: bool,
) -> Any:
    """渲染 OAuth Provider 创建或编辑表单；secret 永远不回显。"""
    code_field = (
        label(
            span("Provider code"),
            input_(name="code", value=provider.code if provider else "", required=True),
        )
        if create
        else input_(type="hidden", name="code", value=provider.code if provider else "")
    )
    enabled = bool(provider.enabled) if provider else True
    client_enabled = bool(provider.client_login_enabled) if provider else True
    admin_enabled = bool(provider.admin_login_enabled) if provider else True
    role_required = bool(provider.role_claim_required) if provider else True
    auto_create = bool(provider.auto_create) if provider else True
    return form(
        _csrf(csrf),
        code_field,
        label(
            span("Display name"),
            input_(
                name="display_name",
                value=provider.display_name if provider else "",
                required=True,
            ),
        ),
        label(
            span("Issuer"),
            input_(
                name="issuer",
                type="url",
                value=provider.issuer if provider else "",
                placeholder="https://id.example.com",
                required=True,
            ),
        ),
        label(
            span("Client ID"),
            input_(
                name="client_id",
                value=provider.client_id if provider else "",
                required=True,
            ),
        ),
        label(
            span("Client secret"),
            input_(
                name="client_secret",
                type="password",
                autocomplete="new-password",
                placeholder=(
                    "Leave blank to keep current secret"
                    if provider
                    else "Optional for public clients"
                ),
            ),
        ),
        label(
            span("Discovery URL"),
            input_(
                name="discovery_url",
                type="url",
                value=(provider.discovery_url or "") if provider else "",
            ),
        ),
        label(
            span("Authorization endpoint"),
            input_(
                name="authorization_endpoint",
                type="url",
                value=(provider.authorization_endpoint or "") if provider else "",
            ),
        ),
        label(
            span("Token endpoint"),
            input_(
                name="token_endpoint",
                type="url",
                value=(provider.token_endpoint or "") if provider else "",
            ),
        ),
        label(
            span("JWKS URI"),
            input_(
                name="jwks_uri",
                type="url",
                value=(provider.jwks_uri or "") if provider else "",
            ),
        ),
        label(
            span("Token auth method"),
            select(
                option(
                    "Client secret basic",
                    value="client_secret_basic",
                    selected=(
                        provider.token_auth_method == "client_secret_basic"
                        if provider
                        else True
                    ),
                ),
                option(
                    "Client secret post",
                    value="client_secret_post",
                    selected=(
                        provider.token_auth_method == "client_secret_post"
                        if provider
                        else False
                    ),
                ),
                name="token_auth_method",
            ),
        ),
        label(
            span("Scopes"),
            input_(
                name="scopes",
                value=(provider.scopes if provider else "openid profile email phone"),
            ),
        ),
        label(
            span("Username claim"),
            input_(
                name="username_claim",
                value=provider.username_claim if provider else "preferred_username",
            ),
        ),
        label(
            span("Role claim"),
            input_(
                name="role_claim",
                value=provider.role_claim if provider else "groups",
            ),
        ),
        label(
            span("Role sync max age (seconds)"),
            input_(
                name="role_sync_max_age_seconds",
                type="number",
                min="1",
                value=provider.role_sync_max_age_seconds if provider else "1800",
            ),
        ),
        label(
            span("Client login"),
            select(
                option("Enabled", value="1", selected=client_enabled),
                option("Disabled", value="0", selected=not client_enabled),
                name="client_login_enabled",
            ),
        ),
        label(
            span("Admin login"),
            select(
                option("Enabled", value="1", selected=admin_enabled),
                option("Disabled", value="0", selected=not admin_enabled),
                name="admin_login_enabled",
            ),
        ),
        label(
            span("Role claim required"),
            select(
                option("Yes", value="1", selected=role_required),
                option("No", value="0", selected=not role_required),
                name="role_claim_required",
            ),
        ),
        label(
            span("Auto create client users"),
            select(
                option("Yes", value="1", selected=auto_create),
                option("No", value="0", selected=not auto_create),
                name="auto_create",
            ),
        ),
        label(
            span("Provider enabled"),
            select(
                option("Yes", value="1", selected=enabled),
                option("No", value="0", selected=not enabled),
                name="enabled",
            ),
        ),
        (
            label(
                span("Clear existing client secret"),
                select(
                    option("No", value="0"),
                    option("Yes", value="1"),
                    name="clear_client_secret",
                ),
            )
            if provider is not None
            else span("")
        ),
        button(
            "Create provider" if create else "Save provider",
            type="submit",
            class_="btn btn-primary",
        ),
        action=(
            "/admin/oauth-providers"
            if create
            else f"/admin/oauth-providers/{provider.code}/update"
        ),
        method="post",
        class_="form-grid",
    )


def oauth_providers_page(
    *,
    principal: AdminPrincipal,
    csrf: str,
    pages: Iterable[AdminPageDefinition],
    providers: list[OAuthProvider],
    theme: str,
    color_mode: str,
    density: str,
    can_manage: bool,
    flash: str | None = None,
) -> Any:
    """渲染客户端和管理端共用的动态 OAuth Provider 配置页。"""
    rows = []
    for provider in providers:
        actions = (
            _toggle_and_delete(
                csrf=csrf,
                prefix=f"/admin/oauth-providers/{provider.code}",
                enabled=bool(provider.enabled),
                can_write=can_manage,
            )
            if can_manage
            else span("Read only", class_="status-badge")
        )
        editor = (
            details(
                summary("Edit provider"),
                _provider_form(csrf=csrf, provider=provider, create=False),
            )
            if can_manage
            else span("")
        )
        rows.append(
            tr(
                td(
                    provider.display_name,
                    small(provider.code, class_="block-muted"),
                ),
                td(
                    provider.issuer,
                    small(provider.client_id, class_="block-muted"),
                ),
                td(
                    "Client" if provider.client_login_enabled else "—",
                    small(
                        "Admin"
                        if provider.admin_login_enabled
                        else "No admin login",
                        class_="block-muted",
                    ),
                ),
                td(
                    provider.role_claim,
                    small(
                        f"{provider.role_sync_max_age_seconds}s sync age",
                        class_="block-muted",
                    ),
                ),
                td(
                    "Enabled" if provider.enabled else "Disabled",
                    small(
                        "Secret configured"
                        if provider.client_secret_cipher
                        else "No client secret",
                        class_="block-muted",
                    ),
                ),
                td(actions, editor),
            )
        )
    create_form = _provider_form(csrf=csrf, create=True) if can_manage else None
    return _management_page(
        title_text="OAuth providers",
        lead=(
            "Configure OIDC once and choose whether each provider may authenticate "
            "client users, administrators, or both. Client secrets are write-only."
        ),
        principal=principal,
        csrf=csrf,
        pages=pages,
        theme=theme,
        color_mode=color_mode,
        density=density,
        create_form=create_form,
        headers=["Provider", "Issuer", "Login scopes", "Role sync", "State", "Actions"],
        rows=rows,
        flash=flash,
    )
