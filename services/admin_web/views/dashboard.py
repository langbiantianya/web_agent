"""管理后台概览页。

指标卡与快捷入口的聚合页，页面片段可按策略整体隐藏。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ludic.html import a, div, h1, h2, p, section

from app.models.admin_control import AdminPageDefinition
from services.admin_web.accounts import AdminPrincipal
from services.admin_web.views.components import _metric
from services.admin_web.views.shell import shell


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
        content=div(
            section(h1("Overview")),
            sections[0] if len(sections) == 1 else div(*sections, class_="stack"),
            class_="stack",
        ),
    )
