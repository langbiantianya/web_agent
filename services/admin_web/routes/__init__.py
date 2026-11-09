"""管理端路由包。

每个业务域一个模块，模块暴露 ``register(app)``；``register_all`` 负责
按**固定顺序**注册全部路由。

顺序是行为的一部分：Starlette 首个匹配 path 的路由胜出。域模块不 import
app 对象（由参数传入），避免与 ``services.admin_web.app`` 形成循环导入。

两个体量大的域拆成了两个模块（单文件行数上限是 350 行）：工作流的写
操作在 ``workflows_write``，组件权限与角色策略在 ``access_components``。
拆分只切代码位置，路由的路径、方法与注册顺序都没变。
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
        access_components,
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
        workflows_write,
    )

    for module in (
        auth,
        dashboard,
        workflows,
        workflows_write,
        agents,
        skills,
        mcp,
        llm,
        audit,
        oauth_providers,
        access,
        access_components,
        platform_admins,
        preferences,
    ):
        module.register(app)
