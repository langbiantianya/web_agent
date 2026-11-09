"""管理端应用装配。

路由与页面已按业务域拆到 ``services.admin_web.routes`` 与
``services.admin_web.views`` 两个包里；这里只负责把应用组装起来：
创建 app、装生命周期、加中间件、挂静态资源、注册路由。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from ludic.web import LudicApp, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.staticfiles import StaticFiles

from services.admin_web.accounts import (
    ensure_owner_super_admin,
    rotate_expired_super_admins,
)
from services.admin_web.db import AdminSessionLocal, dispose_admin_engine
from services.admin_web.notifications import notify_password
from services.admin_web.rbac import casbin_service
from services.admin_web.registry import seed_registry

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).with_name("static")


async def _rotation_loop() -> None:
    """每小时执行一次 30 天超级管理员密码轮换。"""
    while True:
        try:
            async with AdminSessionLocal() as db:
                count = await rotate_expired_super_admins(db)
            if count:
                logger.info("已轮换 %s 个平台超级管理员密码", count)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("平台超级管理员密码轮换失败")
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(_: LudicApp):
    """初始化 owner、registry/Casbin，并启动密码轮换任务。"""
    async with AdminSessionLocal() as db:
        initial_password = await ensure_owner_super_admin(db)
        if initial_password is not None:
            logger.warning(
                "首次安装已创建平台超级管理员 superAdmin；"
                "初始密码仅本次日志输出：%s",
                initial_password,
            )
        await seed_registry(db)
        await casbin_service.refresh(db, force=True)
    task = asyncio.create_task(_rotation_loop())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await dispose_admin_engine()


async def _notify_password(**kwargs):
    """把密码通知转发到当前生效的发送器。

    路由模块不能反向 import 本模块（会成环），所以密码发送器挂在应用
    对象上，由路由在调用时取。转发而不是直接绑定，是为了让
    ``notify_password`` 这个模块级名字在运行期被替换时依然生效。
    """
    return await notify_password(**kwargs)


app = LudicApp(lifespan=lifespan)
app.mount("/admin/static", StaticFiles(directory=str(STATIC_DIR)), name="admin-static")
app.state.admin_password_sender = _notify_password


async def security_headers(request: Request, call_next):
    """添加后台安全响应头。"""
    response: Response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self'; "
        "script-src 'self'; connect-src 'self'; frame-ancestors 'none'",
    )
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


app.add_middleware(BaseHTTPMiddleware, dispatch=security_headers)

from services.admin_web import routes

# 既有测试从本模块导入这两个纯函数；它们已随工作流域搬到 workflows 模块，
# 这里保留导入点，避免既有调用方失效。
from services.admin_web.routes.workflows import (  # noqa: F401
    _definition_errors,
    _pydantic_message,
)

routes.register_all(app)
