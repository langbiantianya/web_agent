"""管理端剩下的渲染物。

Ludic 视图层下线后（V4.10 阶段 E），后台页面全部由 SvelteKit 渲染，
Python 侧只剩三样必须留在那里的东西：

- ``login_page``——会话 Cookie 与 CSRF 令牌都在 Python 侧，登录页必须由
  Python 渲染；
- ``error_page``——未登录 / 无权限时的兜底页；
- ``workflow_editor_page``——画布 island 的片段页。

**曾经在这里的几十个业务页面已经退休**：它们的读写入口由
``/admin/api/*`` 管理面 JSON API 承担，页面由 SvelteKit 渲染。
"""

from __future__ import annotations

from services.admin_web.views.shell import (
    ADMIN_STYLESHEET,
    ADMIN_UI_SCRIPT,
    _flash,
    _locale_switch,
    error_page,
)
from services.admin_web.views.login import login_page
from services.admin_web.views.workflow_editor import workflow_editor_page

__all__ = [
    "ADMIN_STYLESHEET",
    "ADMIN_UI_SCRIPT",
    "_flash",
    "_locale_switch",
    "error_page",
    "login_page",
    "workflow_editor_page",
]


def __dir__() -> list[str]:
    """列出全部再导出名。"""
    return sorted(set(globals()) | set(__all__))
