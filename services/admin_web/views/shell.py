"""后台剩下的两个片段：登录页与错误页。

Ludic 视图层下线后（V4.10 阶段 E），``admin-web`` 只剩三件事：

1. **登录页**（``login.py``）——会话 Cookie 与 CSRF 令牌都在 Python 侧，
   登录页必须由 Python 渲染；
2. **错误页**（本模块的 ``error_page``）——未登录、无权限等场景的兜底；
3. **画布片段页**（``workflow_editor.py``）——画布 island 的载体。

后台外壳（导航、顶栏、主题切换）已经交给 SvelteKit，所以 ``shell()``
连同 ``admin-ui.js`` 那一整套导航脚本一起退休了。
"""

from __future__ import annotations

from typing import Any

from ludic.html import (
    a,
    body,
    button,
    div,
    h1,
    head,
    html,
    link,
    main,
    meta,
    p,
    script,
    section,
    span,
    title,
)

ADMIN_STYLESHEET = "/admin/static/admin.css?v=admin-responsive-20261111-2"
# 改 admin-ui.js 时必须换这个版本号：静态资源虽然带 ETag，但服务端不发明
# Cache-Control 时浏览器会按启发式新鲜度直接用缓存，页面于是继续跑旧脚本
# （表现就是「登录页的语言切换点了没反应」）。服务端另有 /admin/static 的
# no-cache 头兜底，版本号是为了让**已经缓存过旧文件**的浏览器立刻换新。
ADMIN_UI_SCRIPT = "/admin/static/admin-ui.js?v=admin-ui-20261111-1"


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
