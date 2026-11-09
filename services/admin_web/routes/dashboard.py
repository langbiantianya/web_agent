"""后台总览页的路由。"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from ludic.web import Request

from app.models.admin_control import SuperAdminAccount
from app.models.admin_user import AdminUser
from app.models.conversation import Conversation
from app.models.run import Run
from services.admin_web.common import not_found, unauthorized, with_auth
from services.admin_web.registry import (
    can_access_page, can_show_component, visible_pages,
)
from services.admin_web.views import dashboard


def register(app: Any) -> None:
    """把后台总览页的路由挂到 app 上。"""

    @app.get("/admin/")
    async def dashboard_get(request: Request):
        """后台总览。"""
        values = await with_auth(request)
        if values is None:
            return unauthorized()
        db, principal, pref, csrf = values
        try:
            if not await can_access_page(db, principal, "dashboard"):
                return not_found()
            pages = await visible_pages(db, principal)
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
                "platform_admins": (
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
            overview_visible = await can_show_component(
                db, principal, "dashboard", "metrics"
            )
            quick_visible = await can_show_component(
                db, principal, "dashboard", "quick_actions"
            )
            return dashboard(
                principal=principal,
                csrf=csrf,
                pages=pages,
                theme=pref.theme,
                color_mode=pref.color_mode,
                density=pref.density,
                counts=counts,
                overview_visible=overview_visible,
                quick_actions_visible=quick_visible,
            )
        finally:
            await db.close()
