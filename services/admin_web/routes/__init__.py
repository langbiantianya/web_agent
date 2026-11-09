"""管理端路由包。

每个业务域一个模块，模块暴露 ``register(app)``；``register_all`` 负责
按**固定顺序**注册全部路由。

顺序是行为的一部分：Starlette 首个匹配 path 的路由胜出。域模块不 import
app 对象（由参数传入），避免与 ``services.admin_web.app`` 形成循环导入。
"""

from __future__ import annotations

from typing import Any


def register_all(app: Any) -> None:
    """按重构前的装饰器顺序注册各域路由。

    顺序必须与重构前**逐条一致**——``tests/snapshots/admin/routes.json``
    会把顺序一起比对，改动顺序会让那条测试变红。
    """
    from services.admin_web.routes import (
        access,
        agents,
        audit,
        auth,
        dashboard,
        llm,
        mcp,
        oauth_providers,
        platform_admins,
        preferences,
        skills,
        workflows,
    )

    for module in (
        auth,
        dashboard,
        workflows,
        agents,
        skills,
        mcp,
        llm,
        audit,
        oauth_providers,
        access,
        platform_admins,
        preferences,
    ):
        module.register(app)
