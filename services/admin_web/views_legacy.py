"""Ludic SSR 页面与可复用组件。"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from ludic.html import (
    a,
    body,
    button,
    details,
    div,
    form,
    h1,
    h2,
    head,
    html,
    input as input_,
    label,
    link,
    main,
    meta,
    option,
    p,
    section,
    select,
    script,
    small,
    span,
    summary,
    table,
    tbody,
    td,
    th,
    thead,
    title,
    tr,
)

from app.models.admin_control import (
    AdminComponentDefinition,
    AdminPageDefinition,
    AdminPagePermission,
    AdminComponentPermission,
    OAuthRoleMapping,
    SuperAdminAccount,
)
from app.models.admin_user import AdminUser
from app.models.oauth import OAuthProvider
from services.admin_web.accounts import AdminPrincipal
from services.admin_web.views.agents import agents_page
from services.admin_web.views.audit import audit_page
from services.admin_web.views.components import (
    _csrf,
    _management_page,
    _metric,
    _toggle_and_delete,
)
from services.admin_web.views.llm import llm_page
from services.admin_web.views.mcp import mcp_page
from services.admin_web.views.shell import (
    ADMIN_STYLESHEET,
    ADMIN_UI_SCRIPT,
    NAV_GROUP_ORDER,
    _flash,
    _locale_switch,
    _nav,
    error_page,
    shell,
)
from services.admin_web.views.skills import skills_page
from services.admin_web.views.workflows import workflow_editor_page, workflows_page


def login_page(
    *,
    providers: list[OAuthProvider],
    flash: str | None = None,
    flash_kind: str = "info",
) -> Any:
    """渲染 OAuth 和平台超级管理员登录页面。"""
    oauth_buttons = [
        form(
            button(
                f"Continue with {provider.display_name}",
                type="submit",
                class_="btn btn-oauth",
            ),
            action=f"/admin/login/oauth/{provider.code}",
            method="post",
            class_="oauth-form",
        )
        for provider in providers
    ] or [p("No OAuth provider is configured.", class_="muted")]
    return html(
        head(
            meta(charset="utf-8"),
            meta(name="viewport", content="width=device-width, initial-scale=1"),
            title("Sign in · web_agent Admin"),
            link(rel="stylesheet", href=ADMIN_STYLESHEET),
            script(src=ADMIN_UI_SCRIPT),
        ),
        body(
            main(
                section(
                    div(_locale_switch(), class_="login-toolbar"),
                    p("WEB_AGENT CLOUD", class_="eyebrow"),
                    h1("Administration"),
                    p(
                        "Manage platform configuration, access policy, and operations.",
                        class_="lead",
                    ),
                    _flash(flash, flash_kind),
                    div(
                        section(
                            h2("Administrator identity"),
                            p(
                                "Use your organization identity. Roles are synchronized "
                                "from OAuth claims.",
                                class_="muted",
                            ),
                            *oauth_buttons,
                            div(
                                button(
                                    "Use username and password",
                                    type="button",
                                    id="toggle-super-admin-login",
                                    class_="btn btn-oauth login-disclosure-button",
                                    aria_controls="super-admin-login-panel",
                                    aria_expanded="false",
                                    data_admin_login_toggle="super-admin-login-panel",
                                ),
                                class_="login-disclosure",
                            ),
                            class_="login-card primary-login-card",
                        ),
                        section(
                            div(
                                h2(
                                    "Platform super admin",
                                    id="super-admin-sign-in-title",
                                ),
                                button(
                                    "Back to OAuth",
                                    type="button",
                                    class_="btn btn-link login-panel-back",
                                    aria_controls="super-admin-login-panel",
                                    aria_expanded="true",
                                    data_admin_login_toggle="super-admin-login-panel",
                                ),
                                class_="login-panel-header",
                            ),
                            div(
                                p(
                                    "Reserved for the built-in emergency and platform "
                                    "owner accounts.",
                                    class_="muted",
                                ),
                                form(
                                    label(
                                        span("Username"),
                                        input_(
                                            name="username",
                                            autocomplete="username",
                                            required=True,
                                        ),
                                    ),
                                    label(
                                        span("Password"),
                                        input_(
                                            name="password",
                                            type="password",
                                            autocomplete="current-password",
                                            minlength=8,
                                            maxlength=8,
                                            required=True,
                                        ),
                                    ),
                                    input_(
                                        type="hidden",
                                        name="next",
                                        value="/admin/",
                                    ),
                                    button(
                                        "Sign in",
                                        type="submit",
                                        class_="btn btn-primary",
                                    ),
                                    action="/admin/login/super-admin",
                                    method="post",
                                    class_="stack",
                                ),
                                div(
                                    button(
                                        "Reset password",
                                        type="button",
                                        class_="btn btn-link",
                                        aria_controls="super-admin-password-reset-view",
                                        aria_expanded="false",
                                        data_admin_view_show=(
                                            "super-admin-password-reset-view"
                                        ),
                                        data_admin_view_hide="super-admin-sign-in-view",
                                    ),
                                    class_="login-view-actions",
                                ),
                                id="super-admin-sign-in-view",
                                class_="login-view",
                            ),
                            div(
                                h2(
                                    "Reset password",
                                    id="super-admin-password-reset-title",
                                ),
                                p(
                                    "Enter your username and we will send a new password "
                                    "to the registered contact channel.",
                                    class_="muted",
                                ),
                                form(
                                    label(
                                        span("Username"),
                                        input_(
                                            name="username",
                                            autocomplete="username",
                                            required=True,
                                        ),
                                    ),
                                    button(
                                        "Send new password",
                                        type="submit",
                                        class_="btn btn-primary",
                                    ),
                                    action="/admin/super-admin/forgot-password",
                                    method="post",
                                    class_="password-reset-form",
                                ),
                                div(
                                    button(
                                        "Back to sign in",
                                        type="button",
                                        class_="btn btn-secondary",
                                        data_admin_view_show="super-admin-sign-in-view",
                                        data_admin_view_hide=(
                                            "super-admin-password-reset-view"
                                        ),
                                    ),
                                    class_="login-view-actions",
                                ),
                                id="super-admin-password-reset-view",
                                class_="login-view password-reset-view",
                                hidden=True,
                            ),
                            id="super-admin-login-panel",
                            class_="login-card super-admin-login-card",
                            hidden=True,
                        ),
                        class_="login-methods login-stage",
                    ),
                    class_="login-shell",
                ),
                class_="login-page",
            ),
        ),
        lang="en",
        data_theme="fluent",
        data_color_mode="system",
    )

def dashboard(
    *,
    principal: AdminPrincipal,
    csrf: str,
    pages: Iterable[AdminPageDefinition],
    theme: str,
    color_mode: str,
    density: str,
    counts: dict[str, int],
    overview_visible: bool,
    quick_actions_visible: bool,
) -> Any:
    """渲染管理后台概览。"""
    metric_cards = div(
        _metric("Conversations", counts.get("conversations", 0)),
        _metric("Runs", counts.get("runs", 0)),
        _metric("Active OAuth admins", counts.get("admins", 0)),
        _metric("Platform admins", counts.get("platform_admins", 0)),
        class_="metric-grid",
    )
    actions = div(
        h2("Quick actions"),
        div(
            a("Edit page permissions", href="/admin/access/pages", class_="card-link"),
            a(
                "Edit component permissions",
                href="/admin/access/components",
                class_="card-link",
            ),
            a("Review Casbin roles", href="/admin/access/roles", class_="card-link"),
            class_="quick-actions",
        ),
        class_="surface",
    )
    sections = []
    if overview_visible:
        sections.append(section(h2("Platform overview"), metric_cards, class_="surface"))
    if quick_actions_visible:
        sections.append(actions)
    if not sections:
        sections.append(
            section(
                h2("No dashboard components available"),
                p(
                    "Your administrator account is active, but all dashboard "
                    "components are hidden by the current UI policy.",
                    class_="muted",
                ),
                class_="surface",
            )
        )
    return shell(
        title_text="Overview",
        principal=principal,
        csrf=csrf,
        theme=theme,
        color_mode=color_mode,
        density=density,
        pages=pages,
        content=sections[0] if len(sections) == 1 else div(*sections, class_="stack"),
    )


def access_pages_page(
    *,
    principal: AdminPrincipal,
    csrf: str,
    pages: Iterable[AdminPageDefinition],
    definitions: list[AdminPageDefinition],
    bindings: dict[str, list[AdminPagePermission]],
    theme: str,
    color_mode: str,
    density: str,
    flash: str | None = None,
) -> Any:
    """渲染页面权限配置器。"""
    rows = []
    for definition in definitions:
        rows.append(
            tr(
                td(definition.title, small(definition.code, class_="block-muted")),
                td(definition.nav_group),
                td(
                    _permission_editor(
                        action=(
                            f"/admin/access/pages/{definition.code}/permissions"
                        ),
                        csrf=csrf,
                        rows=bindings.get(definition.code, []),
                        is_platform_super_admin=principal.is_platform_super_admin,
                        tenant_id=principal.tenant_id,
                        aria_label=definition.title,
                    )
                ),
            )
        )
    return shell(
        title_text="Page permissions",
        principal=principal,
        csrf=csrf,
        theme=theme,
        color_mode=color_mode,
        density=density,
        pages=pages,
        flash=flash,
        content=section(
            h1("Page permissions"),
            p(
                "Bind known pages to RBAC object/action permissions. UI bindings "
                "never replace API authorization.",
                class_="lead",
            ),
            _permission_datalist(bindings),
            div(
                table(
                    thead(tr(th("Page"), th("Group"), th("Permissions"))),
                    tbody(*rows),
                    class_="data-table",
                ),
                class_="table-scroll",
            ),
            class_="surface",
        ),
    )


def _permission_datalist(bindings: dict[str, list[AdminPagePermission]]) -> Any:
    """提供权限输入提示。"""
    values = sorted(
        {
            f"{row.object_name}.{row.action_name}"
            for rows in bindings.values()
            for row in rows
        }
    )
    return div(
        *[
            span(value, class_="chip")
            for value in values
        ],
        class_="chip-list",
    )


def _permission_editor(
    *,
    action: str,
    csrf: str,
    rows: list[Any],
    is_platform_super_admin: bool,
    tenant_id: str | None,
    aria_label: str,
) -> Any:
    """渲染现有作用域和新增租户覆盖的权限编辑表单。"""
    grouped: dict[str, list[Any]] = {}
    for row in rows:
        grouped.setdefault(row.binding_scope, []).append(row)
    editors = []
    for _, scoped_rows in sorted(grouped.items()):
        current = ", ".join(
            f"{row.object_name}.{row.action_name}"
            for row in scoped_rows
        )
        scope_name = scoped_rows[0].tenant_id or "global"
        editors.append(
            form(
                input_(type="hidden", name="csrf_token", value=csrf),
                (
                    input_(
                        type="text",
                        name="tenant_id",
                        value=scoped_rows[0].tenant_id or "",
                        placeholder="blank = global",
                        aria_label=f"Tenant scope for {aria_label}",
                    )
                    if is_platform_super_admin
                    else input_(
                        type="hidden",
                        name="tenant_id",
                        value=tenant_id or "",
                    )
                ),
                input_(
                    type="text",
                    name="permissions",
                    value=current,
                    list="permission-list",
                    aria_label=f"{aria_label}, {scope_name} permissions",
                ),
                button("Save", type="submit", class_="btn btn-small"),
                action=action,
                method="post",
                class_="inline-edit",
            )
        )
    if is_platform_super_admin:
        editors.append(
            form(
                input_(type="hidden", name="csrf_token", value=csrf),
                input_(
                    type="text",
                    name="tenant_id",
                    placeholder="Tenant override (blank = global)",
                    aria_label=f"New tenant scope for {aria_label}",
                ),
                input_(
                    type="text",
                    name="permissions",
                    list="permission-list",
                    placeholder="workflow.read, admin_ui.manage",
                    aria_label=f"New scope permissions for {aria_label}",
                ),
                button("Upsert scope", type="submit", class_="btn btn-small"),
                action=action,
                method="post",
                class_="inline-edit",
            )
        )
    return div(*editors, class_="scope-editors")


def access_components_page(
    *,
    principal: AdminPrincipal,
    csrf: str,
    pages: Iterable[AdminPageDefinition],
    components: list[AdminComponentDefinition],
    bindings: dict[tuple[str, str], list[AdminComponentPermission]],
    theme: str,
    color_mode: str,
    density: str,
    flash: str | None = None,
) -> Any:
    """渲染组件权限配置器。"""
    rows = []
    for component in components:
        key = (component.page_code, component.code)
        rows.append(
            tr(
                td(
                    component.label,
                    small(
                        f"{component.page_code}/{component.code}",
                        class_="block-muted",
                    ),
                ),
                td(component.kind),
                td(
                    _permission_editor(
                        action=(
                            f"/admin/access/components/{component.page_code}/"
                            f"{component.code}/permissions"
                        ),
                        csrf=csrf,
                        rows=bindings.get(key, []),
                        is_platform_super_admin=principal.is_platform_super_admin,
                        tenant_id=principal.tenant_id,
                        aria_label=component.label,
                    )
                ),
            )
        )
    return shell(
        title_text="Component permissions",
        principal=principal,
        csrf=csrf,
        theme=theme,
        color_mode=color_mode,
        density=density,
        pages=pages,
        flash=flash,
        content=section(
            h1("Component permissions"),
            p(
                "Components without bindings are hidden. Hidden components are not "
                "an API authorization boundary.",
                class_="lead",
            ),
            div(
                table(
                    thead(tr(th("Component"), th("Type"), th("Permissions"))),
                    tbody(*rows),
                    class_="data-table",
                ),
                class_="table-scroll",
            ),
            class_="surface",
        ),
    )


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


def preferences_page(
    *,
    principal: AdminPrincipal,
    csrf: str,
    pages: Iterable[AdminPageDefinition],
    theme: str,
    color_mode: str,
    density: str,
    flash: str | None = None,
) -> Any:
    """渲染主题、色温和密度设置。"""
    theme_options = [
        (
            "fluent",
            "Fluent Design",
            "Layered surfaces, acrylic accents, and Segoe-inspired controls.",
        ),
        (
            "cloudscape",
            "AWS Cloudscape",
            "Dense operations layout, flat panels, and a dark command bar.",
        ),
        (
            "material",
            "Google Material",
            "Soft elevation, expressive shapes, and Google blue actions.",
        ),
    ]
    theme_previews = [
        label(
            input_(
                type="radio",
                name="theme",
                value=value,
                checked=theme == value,
                required=True,
            ),
            div(
                div(class_="theme-preview-sidebar"),
                div(
                    div(class_="theme-preview-bar"),
                    div(class_="theme-preview-card"),
                    class_="theme-preview-main",
                ),
                class_="theme-preview",
                aria_hidden="true",
            ),
            div(
                span(label_text),
                small(description),
                class_="theme-copy",
            ),
            class_=f"theme-option theme-option-{value}",
        )
        for value, label_text, description in theme_options
    ]
    return shell(
        title_text="Appearance",
        principal=principal,
        csrf=csrf,
        theme=theme,
        color_mode=color_mode,
        density=density,
        pages=pages,
        flash=flash,
        content=section(
            h1("Appearance"),
            p(
                "Choose a cloud-console visual language. Preferences are stored "
                "server-side for this administrator.",
                class_="lead",
            ),
            form(
                input_(type="hidden", name="csrf_token", value=csrf),
                label(
                    span("Theme"),
                    div(
                        *theme_previews,
                        class_="theme-previews",
                        role="radiogroup",
                        aria_label="Admin visual theme",
                    ),
                    class_="theme-field",
                ),
                label(
                    span("Color mode"),
                    select(
                        option(
                            "System",
                            value="system",
                            selected=color_mode == "system",
                        ),
                        option("Light", value="light", selected=color_mode == "light"),
                        option("Dark", value="dark", selected=color_mode == "dark"),
                        name="color_mode",
                    ),
                ),
                label(
                    span("Density"),
                    select(
                        option(
                            "Comfortable",
                            value="comfortable",
                            selected=density == "comfortable",
                        ),
                        option(
                            "Compact",
                            value="compact",
                            selected=density == "compact",
                        ),
                        name="density",
                    ),
                ),
                button("Save preferences", type="submit", class_="btn btn-primary"),
                action="/admin/preferences",
                method="post",
                class_="form-grid",
            ),
            class_="surface",
        ),
    )
