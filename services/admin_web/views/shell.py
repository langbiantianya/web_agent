"""管理端外壳与全局片段。

``shell()`` 是所有页面共用的 HTML 外壳（导航、主题、flash 消息）；
``_nav`` / ``_locale_switch`` / ``_flash`` 是它的零件。单独成模块是因为
它们被**每个**页面依赖，放在页面模块里会形成反向依赖。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ludic.html import (
    a,
    body,
    button,
    details,
    div,
    form,
    h1,
    head,
    header,
    html,
    input as input_,
    li,
    link,
    main,
    meta,
    nav,
    p,
    script,
    section,
    span,
    summary,
    title,
    ul,
)

from app.models.admin_control import AdminPageDefinition
from services.admin_web.accounts import AdminPrincipal
from services.admin_web.views.confirm import confirm_dialog

ADMIN_STYLESHEET = "/admin/static/admin.css?v=admin-responsive-20261110-7"
# 改 admin-ui.js 时必须换这个版本号：静态资源虽然带 ETag，但服务端不发明
# Cache-Control 时浏览器会按启发式新鲜度直接用缓存，页面于是继续跑旧脚本
# （表现就是「下拉框不填、按钮点了没反应」）。服务端另有 /admin/static 的
# no-cache 头兜底，版本号是为了让**已经缓存过旧文件**的浏览器立刻换新。
ADMIN_UI_SCRIPT = "/admin/static/admin-ui.js?v=admin-ui-20261110-11"
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
    extra_scripts: list[str] | None = None,
    extra_styles: list[str] | None = None,
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
            *(link(rel="stylesheet", href=href) for href in (extra_styles or [])),
            script(src=ADMIN_UI_SCRIPT),
            *(script(src=src) for src in (extra_scripts or [])),
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
                    confirm_dialog(),
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
