"""管理端页面渲染包。

按业务域拆成独立模块：``shell``（外壳与全局片段）、``components``
（通用 UI 组件）、``fields``（数据驱动的表单字段），以及每个业务域一个
页面模块。

这里做再导出，是为了保持 ``from services.admin_web.views import
workflows_page`` 这类调用点可用——调用方不需要知道页面住在哪个模块。

拆分是逐步进行的：还没搬走的页面暂时从 ``views_legacy`` 再导出，
全部搬完后（Task 7）删掉那个过渡模块。
"""

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
from services.admin_web.views_legacy import (
    access_components_page,
    access_pages_page,
    agents_page,
    audit_page,
    dashboard,
    llm_page,
    login_page,
    mappings_page,
    mcp_page,
    oauth_providers_page,
    platform_admins_page,
    preferences_page,
    roles_page,
    skills_page,
    workflow_editor_page,
    workflows_page,
)

__all__ = [
    "ADMIN_STYLESHEET",
    "ADMIN_UI_SCRIPT",
    "NAV_GROUP_ORDER",
    "_flash",
    "_locale_switch",
    "_nav",
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
