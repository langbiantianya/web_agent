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
    header,
    html,
    input as input_,
    label,
    li,
    link,
    main,
    meta,
    nav,
    option,
    p,
    section,
    select,
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
    ul,
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


def _flash(message: str | None, kind: str = "info") -> Any:
    """返回可选提示条。"""
    if not message:
        return span("", class_="empty")
    return div(message, class_=f"flash {kind}", role="status")


def _nav(pages: Iterable[AdminPageDefinition]) -> Any:
    """生成桌面侧栏和移动端 details 导航。"""
    items = [
        li(
            a(
                page.title,
                href=page.route_key,
                class_="nav-link",
            )
        )
        for page in pages
    ]
    return nav(
        details(
            summary("Menu", class_="mobile-menu-summary"),
            ul(*items, class_="nav-list"),
            class_="mobile-nav",
            open=True,
        ),
        ul(*items, class_="nav-list desktop-nav"),
        class_="sidebar",
        aria_label="Admin navigation",
    )


def shell(
    *,
    title_text: str,
    principal: AdminPrincipal,
    csrf: str,
    theme: str,
    color_mode: str,
    density: str,
    pages: Iterable[AdminPageDefinition],
    content: Any,
    flash: str | None = None,
    flash_kind: str = "info",
) -> Any:
    """渲染三主题共用的管理端 HTML 外壳。"""
    role_label = (
        "Platform owner"
        if principal.is_platform_super_admin and principal.is_owner
        else "Platform super admin"
        if principal.is_platform_super_admin
        else "OAuth administrator"
    )
    logout_form = form(
        input_(type="hidden", name="csrf_token", value=csrf),
        button("Sign out", type="submit", class_="btn btn-secondary"),
        action="/admin/logout",
        method="post",
        class_="inline-form",
    )
    return html(
        head(
            meta(charset="utf-8"),
            meta(
                name="viewport",
                content="width=device-width, initial-scale=1",
            ),
            title(f"{title_text} · web_agent Admin"),
            link(rel="stylesheet", href="/admin/static/admin.css"),
        ),
        body(
            header(
                div(
                    a("web_agent", href="/admin/", class_="brand"),
                    span("Cloud Control Plane", class_="brand-subtitle"),
                    class_="brand-group",
                ),
                div(
                    span(role_label, class_="user-role"),
                    span(principal.username, class_="user-name"),
                    a(
                        "Preferences",
                        href="/admin/preferences",
                        class_="header-link",
                    ),
                    logout_form,
                    class_="header-actions",
                ),
                class_="topbar",
            ),
            div(
                _nav(pages),
                main(
                    _flash(flash, flash_kind),
                    content,
                    class_="main-content",
                    id="main-content",
                ),
                class_="admin-layout",
            ),
        ),
        lang="en",
        data_theme=theme,
        data_color_mode=color_mode,
        data_density=density,
    )


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
            link(rel="stylesheet", href="/admin/static/admin.css"),
        ),
        body(
            main(
                section(
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
                            class_="login-card",
                        ),
                        section(
                            h2("Platform super admin"),
                            p(
                                "Reserved for the built-in emergency and platform owner "
                                "accounts.",
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
                                    "Forgot password",
                                    type="submit",
                                    class_="btn btn-link",
                                ),
                                action="/admin/super-admin/forgot-password",
                                method="post",
                                class_="password-reset-form",
                            ),
                            class_="login-card",
                        ),
                        class_="login-grid",
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


def _metric(label_text: str, value: int) -> Any:
    """渲染概览指标卡。"""
    return div(
        span(str(value), class_="metric-value"),
        span(label_text, class_="metric-label"),
        class_="metric-card",
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
                    select(
                        option("Fluent", value="fluent", selected=theme == "fluent"),
                        option(
                            "Cloudscape",
                            value="cloudscape",
                            selected=theme == "cloudscape",
                        ),
                        option(
                            "Material",
                            value="material",
                            selected=theme == "material",
                        ),
                        name="theme",
                    ),
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


def error_page(
    *,
    title_text: str,
    message: str,
    status_hint: int = 403,
) -> Any:
    """渲染匿名错误页。"""
    return html(
        head(
            meta(charset="utf-8"),
            meta(name="viewport", content="width=device-width, initial-scale=1"),
            title(f"{title_text} · web_agent Admin"),
            link(rel="stylesheet", href="/admin/static/admin.css"),
        ),
        body(
            main(
                section(
                    p(f"HTTP {status_hint}", class_="eyebrow"),
                    h1(title_text),
                    p(message, class_="lead"),
                    a("Return to sign in", href="/admin/login", class_="btn btn-primary"),
                    class_="error-card",
                ),
                class_="error-shell",
            )
        ),
        lang="en",
        data_theme="fluent",
        data_color_mode="system",
    )
