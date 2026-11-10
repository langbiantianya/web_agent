"""管理端页面渲染包。

按业务域拆成独立模块：``shell``（外壳与全局片段）、``components``
（通用 UI 组件）、``fields``（数据驱动的表单字段），以及每个业务域一个
页面模块。

这里做再导出，是为了保持 ``from services.admin_web.views import
workflows_page`` 这类调用点可用——调用方不需要知道页面住在哪个模块。
全部页面都已搬进本包，不再有过渡模块。

**名字占用提醒**：包属性 ``views.shell`` 是**函数** ``shell``，不是
``views/shell.py`` 模块——静态导入时模块属性先置、后置的名字绑定把它覆盖了。
``views.dashboard`` 同理（``dashboard`` 函数遮蔽 ``views/dashboard.py`` 模块）。
要拿那个**模块**请用 ``importlib.import_module("services.admin_web.views.shell")``
或 ``sys.modules["services.admin_web.views.shell"]``；
``from services.admin_web.views.shell import shell`` 取到的仍然是**函数**。
"""

from __future__ import annotations

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

from services.admin_web.views.fields import Field, render_field, render_form

# 资源类页面已经搬进各自的业务域模块，这里静态再导出。
from services.admin_web.views.agents import agents_page
from services.admin_web.views.audit import audit_page
from services.admin_web.views.llm import llm_page
from services.admin_web.views.mcp import mcp_page
from services.admin_web.views.skills import skills_page
from services.admin_web.views.workflow_editor import workflow_editor_page
from services.admin_web.views.workflows import workflows_page

# 账号与配置类页面同样已经搬进各自的业务域模块，这里静态再导出。
from services.admin_web.views.access import access_components_page, access_pages_page
from services.admin_web.views.dashboard import dashboard
from services.admin_web.views.login import login_page
from services.admin_web.views.oauth_providers import oauth_providers_page
from services.admin_web.views.platform_admins import platform_admins_page
from services.admin_web.views.preferences import preferences_page
from services.admin_web.views.role_mappings import mappings_page
from services.admin_web.views.roles import roles_page

__all__ = [
    "ADMIN_STYLESHEET",
    "ADMIN_UI_SCRIPT",
    "Field",
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
    "render_field",
    "render_form",
    "roles_page",
    "shell",
    "skills_page",
    "workflow_editor_page",
    "workflows_page",
]


def __dir__() -> list[str]:
    """列出全部再导出名。"""
    return sorted(set(globals()) | set(__all__))
