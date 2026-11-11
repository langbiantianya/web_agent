"""OAuth Provider 配置页。

客户端用户与管理员共用同一套 Provider 配置，页面用
``views/components.py`` 的通用骨架渲染。

表单改成用 ``views/fields.py`` 的数据驱动字段渲染，因此：

- 校验失败可以**保留已填内容并逐字段标错**（UX-03）；
- 编辑表单放进独立面板，同一时刻只展开一个 Provider，并且可以用
  ``?resource=<code>`` 深链接直接打开（UX-09）。

``client_secret`` 永远是只写字段：失败回显时也不会被带回来。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ludic.html import small, span, td, tr

from app.models.admin_control import AdminPageDefinition
from app.models.oauth import OAuthProvider
from services.admin_web.accounts import AdminPrincipal
from services.admin_web.views.components import (
    _management_page,
    _toggle_and_delete,
)
from services.admin_web.views.edit_panel import edit_link, edit_panel
from services.admin_web.views.fields import render_form
from services.admin_web.views.oauth_fields import (
    PROVIDER_CREATE_FIELDS,
    PROVIDER_EDIT_FIELDS,
    PROVIDER_FIELDS,
)

_CREATE_VALUES = {
    "display_name": "",
    "issuer": "",
    "client_id": "",
    "client_secret": "",
    "discovery_url": "",
    "authorization_endpoint": "",
    "token_endpoint": "",
    "jwks_uri": "",
    "token_auth_method": "client_secret_basic",
    "scopes": "openid profile email phone",
    "username_claim": "preferred_username",
    "role_claim": "groups",
    "role_sync_max_age_seconds": "1800",
    "client_login_enabled": "1",
    "admin_login_enabled": "1",
    "role_claim_required": "1",
    "auto_create": "1",
    "enabled": "1",
}


def _edit_values(provider: OAuthProvider) -> dict[str, Any]:
    """把 Provider 行转成编辑表单的取值。"""
    return {
        "display_name": provider.display_name,
        "issuer": provider.issuer,
        "client_id": provider.client_id,
        "client_secret": "",
        "discovery_url": provider.discovery_url or "",
        "authorization_endpoint": provider.authorization_endpoint or "",
        "token_endpoint": provider.token_endpoint or "",
        "jwks_uri": provider.jwks_uri or "",
        "token_auth_method": provider.token_auth_method,
        "scopes": provider.scopes,
        "username_claim": provider.username_claim,
        "role_claim": provider.role_claim,
        "role_sync_max_age_seconds": str(
            provider.role_sync_max_age_seconds
        ),
        "client_login_enabled": "1" if provider.client_login_enabled else "0",
        "admin_login_enabled": "1" if provider.admin_login_enabled else "0",
        "role_claim_required": "1" if provider.role_claim_required else "0",
        "auto_create": "1" if provider.auto_create else "0",
        "enabled": "1" if provider.enabled else "0",
        "clear_client_secret": "0",
    }


def _provider_form(
    *,
    csrf: str,
    provider: OAuthProvider | None = None,
    create: bool,
    values: dict[str, Any] | None = None,
    errors: dict[str, str] | None = None,
    form_id: str = "oauth-provider-form",
) -> Any:
    """渲染 OAuth Provider 创建或编辑表单；secret 永远不回显。"""
    if create:
        fields = PROVIDER_CREATE_FIELDS
        current = {**_CREATE_VALUES, **(values or {})}
    else:
        fields = PROVIDER_EDIT_FIELDS
        assert provider is not None
        current = {
            **_edit_values(provider),
            **(values or {}),
        }
    return render_form(
        fields,
        mode="create" if create else "edit",
        values=current,
        csrf=csrf,
        action=(
            "/admin/oauth-providers"
            if create
            else f"/admin/oauth-providers/{provider.code}/update"
        ),
        submit_label="Create provider" if create else "Save provider",
        layout="grid",
        errors=errors,
        form_id=form_id,
        error_summary=(
            "The provider could not be saved. Fix the highlighted fields."
            if errors
            else ""
        ),
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
    flash_kind: str = "info",
    create_errors: dict[str, str] | None = None,
    create_values: dict[str, str] | None = None,
    edit_errors: dict[str, dict[str, str]] | None = None,
    edit_values: dict[str, dict[str, str]] | None = None,
    editing_code: str | None = None,
) -> Any:
    """渲染客户端和管理端共用的动态 OAuth Provider 配置页。"""
    per_row_errors = dict(edit_errors or {})
    per_row_values = dict(edit_values or {})
    rows = []
    panel = None
    for provider in providers:
        actions = (
            _toggle_and_delete(
                csrf=csrf,
                prefix=f"/admin/oauth-providers/{provider.code}",
                enabled=bool(provider.enabled),
                can_write=can_manage,
                label=f"OAuth provider {provider.display_name}",
            )
            if can_manage
            else span("Read only", class_="status-badge")
        )
        if editing_code == provider.code and can_manage:
            panel = edit_panel(
                resource=provider.code,
                title=f"Edit provider {provider.display_name}",
                form=_provider_form(
                    csrf=csrf,
                    provider=provider,
                    create=False,
                    values=per_row_values.get(provider.code),
                    errors=per_row_errors.get(provider.code),
                    form_id=f"oauth-provider-edit-{provider.code}",
                ),
                close_url="/admin/oauth-providers",
            )
            editor = span("")
        else:
            editor = (
                edit_link(
                    action="/admin/oauth-providers",
                    resource=provider.code,
                    label="Edit provider",
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
    create_form = (
        _provider_form(
            csrf=csrf,
            create=True,
            values=create_values,
            errors=create_errors,
            form_id="oauth-provider-create",
        )
        if can_manage
        else None
    )
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
        panel=panel,
        headers=["Provider", "Issuer", "Login scopes", "Role sync", "State", "Actions"],
        rows=rows,
        flash=flash,
        flash_kind=flash_kind,
    )
