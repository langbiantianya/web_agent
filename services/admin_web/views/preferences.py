"""管理员外观偏好页。

主题、色温与密度的设置页，预览图由纯 CSS 的占位元素拼出来。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ludic.html import (
    button,
    div,
    form,
    h1,
    input as input_,
    label,
    option,
    p,
    section,
    select,
    small,
    span,
)

from app.models.admin_control import AdminPageDefinition
from services.admin_web.accounts import AdminPrincipal
from services.admin_web.views.shell import shell


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
