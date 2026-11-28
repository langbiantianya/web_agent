"""管理端路由包。

每个业务域一个模块，模块暴露 ``register(app)``；``register_all`` 负责
按**固定顺序**注册全部路由。

顺序是行为的一部分：Starlette 首个匹配 path 的路由胜出。域模块不 import
app 对象（由参数传入），避免与 ``services.admin_web.app`` 形成循环导入。

Ludic 视图层下线后（V4.10 阶段 E），这里只剩**管理面 JSON API** 与两样
必须留在 Python 侧的东西：登录/登出（``auth``）与画布 island 的三个端点
（``workflow_canvas``）。业务页面的读写全部走 ``/admin/api/*``，页面由
SvelteKit 渲染。
"""

from __future__ import annotations

from typing import Any


def register_all(app: Any) -> None:
    """按既定顺序注册各域路由。

    顺序必须与重构前**逐条一致**——``tests/snapshots/admin/routes.json``
    会把顺序一起比对，改动顺序会让那条测试变红。
    """
    from services.admin_web.routes import (
        api,
        api_access,
        api_access_components,
        api_access_mappings,
        api_access_roles,
        api_access_roles_write,
        api_agents,
        api_agents_write,
        api_audit,
        api_llm,
        api_mcp,
        api_mcp_write,
        api_oauth_providers,
        api_platform_admins,
        api_preferences,
        api_skills,
        api_undo,
        api_workflows,
        auth,
        workflow_canvas,
    )

    for module in (
        auth,
        workflow_canvas,
        api,
        api_agents,
        api_llm,
        api_skills,
        api_mcp,
        api_workflows,
        api_oauth_providers,
        api_access,
        api_access_components,
        api_access_roles,
        api_access_roles_write,
        api_access_mappings,
        api_platform_admins,
        api_preferences,
        api_audit,
        api_undo,
    ):
        module.register(app)
