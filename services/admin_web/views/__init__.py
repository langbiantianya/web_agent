"""管理端页面渲染包。

按业务域拆成独立模块：``shell``（外壳与全局片段）、``components``
（通用 UI 组件）、``fields``（数据驱动的表单字段），以及每个业务域一个
页面模块。

这里做再导出，是为了保持 ``from services.admin_web.views import
workflows_page`` 这类调用点可用——调用方不需要知道页面住在哪个模块。

拆分是逐步进行的：还没搬走的页面暂时从 ``views_legacy`` 再导出，
全部搬完后（Task 7）删掉那个过渡模块。

**名字占用提醒**：包属性 ``views.shell`` 是**函数** ``shell``，不是
``views/shell.py`` 模块——静态导入时模块属性先置、后置的名字绑定把它覆盖了。
要拿那个**模块**请用 ``importlib.import_module("services.admin_web.views.shell")``
或 ``sys.modules["services.admin_web.views.shell"]``；
``from services.admin_web.views.shell import shell`` 取到的仍然是**函数**。
"""

from __future__ import annotations

from typing import Any

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

from services.admin_web.views.components import (
    _csrf,
    _management_page,
    _metric,
    _toggle_and_delete,
)

# 还没搬进 views 包的页面函数，暂时寄住在过渡模块 views_legacy 里。
# 这里惰性解析（PEP 562）：访问时才 import，避免
# views_legacy -> views.shell -> views 包 -> views_legacy 的循环导入。
_LAZY_PAGE_EXPORTS = frozenset(
    {
        "access_components_page",
        "access_pages_page",
        "agents_page",
        "audit_page",
        "dashboard",
        "llm_page",
        "login_page",
        "mappings_page",
        "mcp_page",
        "oauth_providers_page",
        "platform_admins_page",
        "preferences_page",
        "roles_page",
        "skills_page",
        "workflow_editor_page",
        "workflows_page",
    }
)

__all__ = [
    "ADMIN_STYLESHEET",
    "ADMIN_UI_SCRIPT",
    "NAV_GROUP_ORDER",
    "_csrf",
    "_flash",
    "_locale_switch",
    "_management_page",
    "_metric",
    "_nav",
    "_toggle_and_delete",
    "access_components_page",
    "access_pages_page",
    "agents_page",
    "audit_page",
    "dashboard",
    "error_page",
    "llm_page",
    "login_page",
    "mappings_page",
    "mcp_page",
    "oauth_providers_page",
    "platform_admins_page",
    "preferences_page",
    "roles_page",
    "shell",
    "skills_page",
    "workflow_editor_page",
    "workflows_page",
]


def __getattr__(name: str) -> Any:
    """惰性再导出尚未搬走的页面函数（PEP 562 模块级 ``__getattr__``）。

    这些函数住在过渡模块 ``views_legacy`` 里，而 ``views_legacy`` 又要从
    ``views.shell`` 取外壳构件——两边静态互导会形成循环导入。这里改成
    第一次访问时才导入 ``views_legacy``，并把结果写回模块全局缓存，
    第二次访问就走常规查找，不再触发本函数。

    名单外的名字一律抛 ``AttributeError``，让 ``hasattr`` 之类照常返回 False。
    """
    if name in _LAZY_PAGE_EXPORTS:
        from services.admin_web import views_legacy

        value = getattr(views_legacy, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """列出全部再导出名（含尚未加载的惰性页面函数）。"""
    return sorted(set(globals()) | set(__all__) | _LAZY_PAGE_EXPORTS)
