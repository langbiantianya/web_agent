"""管理面 JSON API：SvelteKit 前端的服务端网关（V4.10）。

**为什么单独有一层**：后台前端从 Ludic SSR 迁到 SvelteKit，但**鉴权与授权
仍然只有这一份**——会话 Cookie、CSRF 三方校验、Casbin 页面/组件权限、审计
写入都不重写。SvelteKit 只做渲染与交互，把请求转发到这里；越权判定永远发生
在 Python 侧，浏览器里的任何东西都绕不过它。

**为什么不让 SvelteKit 直连数据库**：那意味着在 TypeScript 里重建 Argon2
校验、Casbin 策略求值与审计事务——安全关键逻辑出现第二份实现，两边的判定
迟早不一致。

与页面路由的差别只有两点：

1. 返回 JSON 而不是 HTML；未认证返回 **401**（不是 302），跳转由前端决定；
2. 写操作继续走表单语义（``application/x-www-form-urlencoded`` 或 JSON），
   CSRF 令牌用 ``X-CSRF-Token`` 头或 ``csrf_token`` 字段传递，与页面路由
   共用同一套校验函数。
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from ludic.web import Request
from starlette.responses import JSONResponse

from app.models.admin_control import SuperAdminAccount
from app.models.admin_user import AdminUser
from app.models.conversation import Conversation
from app.models.run import Run
from services.admin_web.common import json_response_no_store, with_auth
from services.admin_web.registry import (
    can_access_page,
    can_show_component,
    visible_pages,
)


def _principal_payload(principal: Any) -> dict[str, Any]:
    """把管理员主体整理成前端可直接消费的形状。"""
    return {
        "kind": principal.kind,
        "principalId": principal.principal_id,
        "username": principal.username,
        "tenantId": principal.tenant_id,
        "isOwner": principal.is_owner,
        "isPlatformSuperAdmin": principal.is_platform_super_admin,
    }


def _page_payload(page: Any) -> dict[str, Any]:
    """把页面注册项整理成前端可直接消费的形状。"""
    return {
        "code": page.code,
        "routeKey": page.route_key,
        "title": page.title,
        "navGroup": page.nav_group,
        "navOrder": page.nav_order,
    }


def register(app: Any) -> None:
    """把管理面 JSON API 挂到 app 上。"""

    @app.get("/admin/api/session")
    async def admin_api_session(request: Request):
        """当前管理员身份、界面偏好、可见页面与 CSRF 令牌。

        前端启动时拉一次：拿到它就知道「我是谁、能看哪些页、当前主题」。
        组件级权限按需再查（``?page=dashboard``）。
        """
        values = await with_auth(request)
        if values is None:
            return json_response_no_store(
                {"code": "unauthorized", "message": "请先登录管理后台"},
                status_code=401,
            )
        db, principal, pref, csrf = values
        try:
            pages = await visible_pages(db, principal)
            payload: dict[str, Any] = {
                "principal": _principal_payload(principal),
                "preferences": {
                    "theme": pref.theme,
                    "colorMode": pref.color_mode,
                    "density": pref.density,
                },
                "pages": [_page_payload(page) for page in pages],
                "csrfToken": csrf,
            }
            page_code = request.query_params.get("page")
            if page_code:
                payload["components"] = {
                    page_code: {
                        component: await can_show_component(
                            db, principal, page_code, component
                        )
                        for component in _component_codes(db, page_code)
                    }
                }
            return json_response_no_store(payload)
        finally:
            await db.close()

    @app.get("/admin/api/dashboard")
    async def admin_api_dashboard(request: Request):
        """概览页的指标与组件可见性。"""
        values = await with_auth(request)
        if values is None:
            return json_response_no_store(
                {"code": "unauthorized", "message": "请先登录管理后台"},
                status_code=401,
            )
        db, principal, _, _ = values
        try:
            if not await can_access_page(db, principal, "dashboard"):
                return json_response_no_store(
                    {"code": "not_found", "message": "页面不可用"},
                    status_code=404,
                )
            conversation_query = sa.select(sa.func.count()).select_from(Conversation)
            run_query = sa.select(sa.func.count()).select_from(Run).join(
                Conversation, Conversation.id == Run.conversation_id
            )
            admin_query = sa.select(sa.func.count()).select_from(AdminUser).where(
                AdminUser.status == "active"
            )
            if not principal.is_platform_super_admin:
                conversation_query = conversation_query.where(
                    Conversation.tenant_id == principal.tenant_id
                )
                run_query = run_query.where(
                    Conversation.tenant_id == principal.tenant_id
                )
                admin_query = admin_query.where(
                    AdminUser.tenant_id == principal.tenant_id
                )
            counts = {
                "conversations": int(await db.scalar(conversation_query) or 0),
                "runs": int(await db.scalar(run_query) or 0),
                "admins": int(await db.scalar(admin_query) or 0),
                "platformAdmins": (
                    int(
                        await db.scalar(
                            sa.select(sa.func.count()).select_from(SuperAdminAccount)
                        )
                        or 0
                    )
                    if principal.is_platform_super_admin
                    else 0
                ),
            }
            return json_response_no_store(
                {
                    "counts": counts,
                    "components": {
                        "metrics": await can_show_component(
                            db, principal, "dashboard", "metrics"
                        ),
                        "quickActions": await can_show_component(
                            db, principal, "dashboard", "quick_actions"
                        ),
                    },
                }
            )
        finally:
            await db.close()


async def _component_codes(db: Any, page_code: str) -> list[str]:
    """列出某页面下注册的全部组件编码。"""
    from app.models.admin_control import AdminComponentDefinition

    rows = await db.scalars(
        sa.select(AdminComponentDefinition.component_code).where(
            AdminComponentDefinition.page_code == page_code
        )
    )
    return list(rows)