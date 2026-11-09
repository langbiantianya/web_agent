"""管理端登录页。

OAuth 登录、平台超级管理员登录与密码重置共用这一个页面。它**不**套
``views/shell.py`` 的外壳——登录页没有导航，也不需要侧边栏。
"""

from __future__ import annotations

from typing import Any

from ludic.html import (
    body,
    button,
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
    p,
    script,
    section,
    span,
    title,
)

from app.models.oauth import OAuthProvider
from services.admin_web.views.shell import (
    ADMIN_STYLESHEET,
    ADMIN_UI_SCRIPT,
    _flash,
    _locale_switch,
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
