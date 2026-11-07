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
    script,
    small,
    span,
    summary,
    table,
    tbody,
    td,
    textarea,
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


ADMIN_STYLESHEET = "/admin/static/admin.css?v=login-slide-20261107-3"
ADMIN_UI_SCRIPT = "/admin/static/admin-ui.js?v=login-slide-20261107-3"
NAV_GROUP_ORDER = {
    "Overview": 0,
    "Access control": 10,
    "Resources": 20,
    "Security": 30,
    "Settings": 40,
}


def _flash(message: str | None, kind: str = "info") -> Any:
    """返回可选提示条。"""
    if not message:
        return span("", class_="empty")
    return div(message, class_=f"flash {kind}", role="status")


def _locale_switch() -> Any:
    """渲染浏览器语言切换器。"""
    return div(
        button(
            "EN",
            type="button",
            class_="locale-button",
            data_admin_locale="en",
            aria_pressed="true",
        ),
        button(
            "简中",
            type="button",
            class_="locale-button",
            data_admin_locale="zh-CN",
            aria_pressed="false",
        ),
        class_="locale-switch",
        role="group",
        aria_label="Language",
    )


def _nav(pages: Iterable[AdminPageDefinition]) -> Any:
    """生成桌面侧栏和移动端 details 导航。"""
    grouped: dict[str, list[Any]] = {}
    for page in pages:
        link = li(
            a(
                span(page.title),
                href=page.route_key,
                class_="nav-link",
                data_page=page.code,
            )
        )
        grouped.setdefault(page.nav_group, []).append(link)
    sections = [
        div(
            span(group, class_="nav-group-title"),
            ul(*items, class_="nav-list"),
            class_="nav-group",
        )
        for group, items in sorted(
            grouped.items(),
            key=lambda item: (
                NAV_GROUP_ORDER.get(item[0], 100),
                item[0],
            ),
        )
    ]
    return nav(
        details(
            summary("Menu", class_="mobile-menu-summary"),
            div(*sections, class_="nav-groups"),
            class_="mobile-nav",
        ),
        div(*sections, class_="nav-groups desktop-nav"),
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
            link(rel="stylesheet", href=ADMIN_STYLESHEET),
            script(src=ADMIN_UI_SCRIPT),
        ),
        body(
            header(
                div(
                    a(
                        span("web_agent", class_="brand-name"),
                        href="/admin/",
                        class_="brand",
                    ),
                    span("Cloud Control Plane", class_="brand-subtitle"),
                    class_="brand-group",
                ),
                div(
                    span(role_label, class_="user-role"),
                    span(principal.username, class_="user-name"),
                    _locale_switch(),
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
) -> Any:
    """渲染业务管理页共用的标题、创建表单和数据表。"""
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
            section(h1(title_text), p(lead, class_="lead")),
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


def workflows_page(
    *,
    principal: AdminPrincipal,
    csrf: str,
    pages: Iterable[AdminPageDefinition],
    rows: list[Any],
    theme: str,
    color_mode: str,
    density: str,
    can_write: bool,
    flash: str | None = None,
) -> Any:
    """渲染 Workflow 编排管理页。"""
    table_rows = []
    for row in rows:
        actions = _toggle_and_delete(
            csrf=csrf,
            prefix=f"/admin/workflows/{row.code}",
            enabled=bool(row.enabled),
            can_write=can_write,
        )
        editor = (
            details(
                summary("Edit definition"),
                form(
                    _csrf(csrf),
                    label(span("Name"), input_(name="name", value=row.name, required=True)),
                    label(
                        span("Description"),
                        input_(name="description", value=row.description or ""),
                    ),
                    label(
                        span("Definition JSON"),
                        textarea(
                            __import__("json").dumps(
                                row.definition, ensure_ascii=False, indent=2
                            ),
                            name="definition",
                            rows=14,
                            required=True,
                        ),
                    ),
                    button("Save changes", type="submit", class_="btn btn-primary"),
                    action=f"/admin/workflows/{row.code}/update",
                    method="post",
                    class_="stack",
                ),
            )
            if can_write
            else span("")
        )
        table_rows.append(
            tr(
                td(row.name, small(row.code, class_="block-muted")),
                td("Enabled" if row.enabled else "Disabled"),
                td(f"v{row.version}"),
                td(actions, editor),
            )
        )
    create_form = (
        form(
            _csrf(csrf),
            label(span("Code"), input_(name="code", required=True)),
            label(span("Name"), input_(name="name", required=True)),
            label(span("Description"), input_(name="description")),
            label(
                span("Definition JSON"),
                textarea(
                    '{"nodes":[{"id":"node","agentCode":"agent"}],"edges":[],'
                    '"inputTemplate":"{{user_input}}"}',
                    name="definition",
                    rows=10,
                    required=True,
                ),
            ),
            label(
                span("Enabled"),
                select(option("Yes", value="1"), option("No", value="0"), name="enabled"),
            ),
            button("Create workflow", type="submit", class_="btn btn-primary"),
            action="/admin/workflows",
            method="post",
            class_="form-grid",
        )
        if can_write
        else None
    )
    return _management_page(
        title_text="Workflows",
        lead="Manage graph definitions directly against the shared workflow repository.",
        principal=principal,
        csrf=csrf,
        pages=pages,
        theme=theme,
        color_mode=color_mode,
        density=density,
        create_form=create_form,
        headers=["Workflow", "State", "Version", "Actions"],
        rows=table_rows,
        flash=flash,
    )


def agents_page(
    *,
    principal: AdminPrincipal,
    csrf: str,
    pages: Iterable[AdminPageDefinition],
    rows: list[Any],
    theme: str,
    color_mode: str,
    density: str,
    can_write: bool,
    flash: str | None = None,
) -> Any:
    """渲染 Agent 配置管理页。"""
    table_rows = []
    for row in rows:
        actions = _toggle_and_delete(
            csrf=csrf,
            prefix=f"/admin/agents/{row.code}",
            enabled=bool(row.enabled),
            can_write=can_write,
        )
        editor = (
            details(
                summary("Edit configuration"),
                form(
                    _csrf(csrf),
                    label(span("Name"), input_(name="name", value=row.name, required=True)),
                    label(span("Model"), input_(name="model", value=row.model, required=True)),
                    label(span("Base URL"), input_(name="base_url", value=row.base_url or "")),
                    label(
                        span("Replacement API key"),
                        input_(
                            name="api_key",
                            type="password",
                            autocomplete="new-password",
                            placeholder="Leave blank to keep current key",
                        ),
                    ),
                    label(
                        span("Tools (comma separated)"),
                        input_(name="tools", value=", ".join(row.tools or [])),
                    ),
                    label(
                        span("Skills (comma separated)"),
                        input_(name="skills", value=", ".join(row.skills or [])),
                    ),
                    label(
                        span("System prompt"),
                        textarea(row.system_prompt, name="system_prompt", rows=10, required=True),
                    ),
                    button("Save changes", type="submit", class_="btn btn-primary"),
                    action=f"/admin/agents/{row.code}/update",
                    method="post",
                    class_="stack",
                ),
            )
            if can_write
            else span("")
        )
        table_rows.append(
            tr(
                td(row.name, small(row.code, class_="block-muted")),
                td(row.model),
                td("Enabled" if row.enabled else "Disabled"),
                td(f"v{row.version}"),
                td(actions, editor),
            )
        )
    create_form = (
        form(
            _csrf(csrf),
            label(span("Code"), input_(name="code", required=True)),
            label(span("Name"), input_(name="name", required=True)),
            label(span("Model"), input_(name="model", required=True)),
            label(span("Base URL"), input_(name="base_url")),
            label(
                span("API key"),
                input_(name="api_key", type="password", autocomplete="new-password"),
            ),
            label(span("Tools (comma separated)"), input_(name="tools")),
            label(span("Skills (comma separated)"), input_(name="skills")),
            label(
                span("System prompt"),
                textarea(name="system_prompt", rows=8, required=True),
            ),
            label(
                span("Enabled"),
                select(option("Yes", value="1"), option("No", value="0"), name="enabled"),
            ),
            button("Create agent", type="submit", class_="btn btn-primary"),
            action="/admin/agents",
            method="post",
            class_="form-grid",
        )
        if can_write
        else None
    )
    return _management_page(
        title_text="Agents",
        lead="Prompt, model, tool, and skill bindings are managed inside the isolated admin service.",
        principal=principal,
        csrf=csrf,
        pages=pages,
        theme=theme,
        color_mode=color_mode,
        density=density,
        create_form=create_form,
        headers=["Agent", "Model", "State", "Version", "Actions"],
        rows=table_rows,
        flash=flash,
    )


def skills_page(
    *,
    principal: AdminPrincipal,
    csrf: str,
    pages: Iterable[AdminPageDefinition],
    rows: list[Any],
    theme: str,
    color_mode: str,
    density: str,
    can_write: bool,
    flash: str | None = None,
) -> Any:
    """渲染 Skill 定义与发布状态管理页。"""
    table_rows = []
    for row in rows:
        editor = (
            details(
                summary("Edit skill"),
                form(
                    _csrf(csrf),
                    label(
                        span("Description"),
                        input_(name="description", value=row.description or "", required=True),
                    ),
                    label(
                        span("Status"),
                        select(
                            option(
                                "Published",
                                value="published",
                                selected=row.status == "published",
                            ),
                            option(
                                "Draft",
                                value="draft",
                                selected=row.status == "draft",
                            ),
                            name="status",
                        ),
                    ),
                    label(
                        span("Skill body"),
                        textarea(row.body or "", name="body", rows=14, required=True),
                    ),
                    button("Save skill", type="submit", class_="btn btn-primary"),
                    action=f"/admin/skills/{row.code}/update",
                    method="post",
                    class_="stack",
                ),
            )
            if can_write and row.status != "archived"
            else span("")
        )
        archive = (
            form(
                _csrf(csrf),
                button("Archive", type="submit", class_="btn btn-danger btn-small"),
                action=f"/admin/skills/{row.code}/archive",
                method="post",
                class_="inline-form",
            )
            if can_write and row.status != "archived"
            else span("")
        )
        table_rows.append(
            tr(
                td(row.name, small(row.code, class_="block-muted")),
                td(row.status),
                td("Enabled" if row.enabled else "Disabled"),
                td(f"v{row.version}"),
                td(archive, editor),
            )
        )
    create_form = (
        form(
            _csrf(csrf),
            label(span("Code"), input_(name="code", required=True)),
            label(span("Description"), input_(name="description", required=True)),
            label(
                span("Status"),
                select(
                    option("Published", value="published"),
                    option("Draft", value="draft"),
                    name="status",
                ),
            ),
            label(span("Skill body"), textarea(name="body", rows=10)),
            label(
                span("Enabled"),
                select(option("Yes", value="1"), option("No", value="0"), name="enabled"),
            ),
            button("Create skill", type="submit", class_="btn btn-primary"),
            action="/admin/skills",
            method="post",
            class_="form-grid",
        )
        if can_write
        else None
    )
    return _management_page(
        title_text="Skills",
        lead="Review publication state and edit instruction packages without crossing the legacy client API.",
        principal=principal,
        csrf=csrf,
        pages=pages,
        theme=theme,
        color_mode=color_mode,
        density=density,
        create_form=create_form,
        headers=["Skill", "Status", "Injection", "Version", "Actions"],
        rows=table_rows,
        flash=flash,
    )


def mcp_page(
    *,
    principal: AdminPrincipal,
    csrf: str,
    pages: Iterable[AdminPageDefinition],
    rows: list[Any],
    theme: str,
    color_mode: str,
    density: str,
    can_write: bool,
    can_refresh: bool,
    flash: str | None = None,
) -> Any:
    """渲染 MCP 服务器管理页。"""
    table_rows = []
    for row in rows:
        actions = _toggle_and_delete(
            csrf=csrf,
            prefix=f"/admin/mcp/{row.code}",
            enabled=bool(row.enabled),
            can_write=can_write,
        )
        refresh = (
            form(
                _csrf(csrf),
                button("Refresh tools", type="submit", class_="btn btn-secondary btn-small"),
                action=f"/admin/mcp/{row.code}/refresh",
                method="post",
                class_="inline-form",
            )
            if can_refresh
            else span("")
        )
        edit = (
            details(
                summary("Edit connection"),
                form(
                    _csrf(csrf),
                    label(span("Name"), input_(name="name", value=row.name or row.code)),
                    label(
                        span("Timeout seconds"),
                        input_(name="timeout_sec", type="number", value=row.timeout_sec),
                    ),
                    label(
                        span("Command"),
                        input_(name="command", value=row.command or ""),
                    ),
                    label(span("URL"), input_(name="url", value=row.url or "")),
                    button("Save connection", type="submit", class_="btn btn-primary"),
                    action=f"/admin/mcp/{row.code}/update",
                    method="post",
                    class_="stack",
                ),
            )
            if can_write
            else span("")
        )
        table_rows.append(
            tr(
                td(row.name or row.code, small(row.code, class_="block-muted")),
                td(row.transport),
                td("Enabled" if row.enabled else "Disabled"),
                td(f"v{row.version}"),
                td(refresh, actions, edit),
            )
        )
    create_form = (
        form(
            _csrf(csrf),
            label(span("Code"), input_(name="code", required=True)),
            label(span("Name"), input_(name="name")),
            label(
                span("Transport"),
                select(
                    option("HTTP", value="http"),
                    option("stdio", value="stdio"),
                    name="transport",
                ),
            ),
            label(span("HTTP URL"), input_(name="url")),
            label(span("stdio command"), input_(name="command")),
            label(
                span("Timeout seconds"),
                input_(name="timeout_sec", type="number", value="30", min="1", max="600"),
            ),
            button("Register server", type="submit", class_="btn btn-primary"),
            action="/admin/mcp",
            method="post",
            class_="form-grid",
        )
        if can_write
        else None
    )
    return _management_page(
        title_text="MCP servers",
        lead="Connection credentials stay encrypted and are never rendered back into the console.",
        principal=principal,
        csrf=csrf,
        pages=pages,
        theme=theme,
        color_mode=color_mode,
        density=density,
        create_form=create_form,
        headers=["Server", "Transport", "State", "Version", "Actions"],
        rows=table_rows,
        flash=flash,
    )


def llm_page(
    *,
    principal: AdminPrincipal,
    csrf: str,
    pages: Iterable[AdminPageDefinition],
    rows: list[Any],
    theme: str,
    color_mode: str,
    density: str,
    can_write: bool,
    flash: str | None = None,
) -> Any:
    """渲染 LLM 接口优先级与故障切换配置页。"""
    table_rows = []
    for row in rows:
        actions = _toggle_and_delete(
            csrf=csrf,
            prefix=f"/admin/llm/{row.code}",
            enabled=bool(row.enabled),
            can_write=can_write,
        )
        edit = (
            details(
                summary("Edit endpoint"),
                form(
                    _csrf(csrf),
                    label(span("Name"), input_(name="name", value=row.name or row.code)),
                    label(span("Base URL"), input_(name="base_url", value=row.base_url)),
                    label(
                        span("Priority"),
                        input_(name="priority", type="number", value=row.priority),
                    ),
                    label(
                        span("Replacement API key"),
                        input_(
                            name="api_key",
                            type="password",
                            autocomplete="new-password",
                            placeholder="Leave blank to keep current key",
                        ),
                    ),
                    button("Save endpoint", type="submit", class_="btn btn-primary"),
                    action=f"/admin/llm/{row.code}/update",
                    method="post",
                    class_="stack",
                ),
            )
            if can_write
            else span("")
        )
        table_rows.append(
            tr(
                td(row.name or row.code, small(row.code, class_="block-muted")),
                td(row.base_url),
                td(str(row.priority)),
                td("Enabled" if row.enabled else "Disabled"),
                td(actions, edit),
            )
        )
    create_form = (
        form(
            _csrf(csrf),
            label(span("Code"), input_(name="code", required=True)),
            label(span("Name"), input_(name="name")),
            label(span("Base URL"), input_(name="base_url", required=True)),
            label(
                span("API key"),
                input_(
                    name="api_key",
                    type="password",
                    autocomplete="new-password",
                ),
            ),
            label(
                span("Priority"),
                input_(name="priority", type="number", value="100", min="0"),
            ),
            button("Create endpoint", type="submit", class_="btn btn-primary"),
            action="/admin/llm",
            method="post",
            class_="form-grid",
        )
        if can_write
        else None
    )
    return _management_page(
        title_text="LLM endpoints",
        lead="Lower priority numbers are attempted first. API keys remain write-only.",
        principal=principal,
        csrf=csrf,
        pages=pages,
        theme=theme,
        color_mode=color_mode,
        density=density,
        create_form=create_form,
        headers=["Endpoint", "Base URL", "Priority", "State", "Actions"],
        rows=table_rows,
        flash=flash,
    )


def audit_page(
    *,
    principal: AdminPrincipal,
    csrf: str,
    pages: Iterable[AdminPageDefinition],
    rows: list[Any],
    total: int,
    theme: str,
    color_mode: str,
    density: str,
    flash: str | None = None,
) -> Any:
    """渲染工具审计摘要和筛选器。"""
    table_rows = [
        tr(
            td(str(row.id)),
            td(str(row.run_id)),
            td(row.tool_name, small(row.node_id, class_="block-muted")),
            td(row.source),
            td("Success" if row.ok else (row.failure_type or "Failed")),
            td(f"{row.duration_ms} ms"),
            td(str(row.created_at)),
        )
        for row in rows
    ]
    filters = form(
        label(span("Run ID"), input_(name="run_id", type="number", min="1")),
        label(span("Tool name"), input_(name="tool_name")),
        label(
            span("Source"),
            select(
                option("Any", value=""),
                option("Builtin", value="builtin"),
                option("Skill", value="skill"),
                option("MCP", value="mcp"),
                name="source",
            ),
        ),
        label(
            span("Outcome"),
            select(
                option("Any", value=""),
                option("Success", value="true"),
                option("Failed", value="false"),
                name="ok",
            ),
        ),
        button("Apply filters", type="submit", class_="btn btn-primary"),
        action="/admin/audit",
        method="get",
        class_="form-grid",
    )
    return shell(
        title_text="Tool audit",
        principal=principal,
        csrf=csrf,
        theme=theme,
        color_mode=color_mode,
        density=density,
        pages=pages,
        flash=flash,
        content=div(
            section(
                h1("Tool audit"),
                p(
                    f"{total} redacted tool invocation records are visible to this tenant.",
                    class_="lead",
                ),
                filters,
                class_="surface",
            ),
            section(
                h2("Recent invocations"),
                div(
                    table(
                        thead(
                            tr(
                                th("ID"),
                                th("Run"),
                                th("Tool"),
                                th("Source"),
                                th("Outcome"),
                                th("Duration"),
                                th("Created"),
                            )
                        ),
                        tbody(
                            *(table_rows or [tr(td("No audit records found."))])
                        ),
                        class_="data-table",
                    ),
                    class_="table-scroll",
                ),
                class_="surface",
            ),
            class_="stack",
        ),
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
            link(rel="stylesheet", href=ADMIN_STYLESHEET),
            script(src=ADMIN_UI_SCRIPT),
        ),
        body(
            main(
                section(
                    div(_locale_switch(), class_="login-toolbar"),
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
