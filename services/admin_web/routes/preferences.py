"""管理员外观偏好（主题、明暗、密度）的路由。"""

from __future__ import annotations

from typing import Any

from ludic.web import Request
from starlette.responses import RedirectResponse

from services.admin_web.audit import audit_principal_action
from services.admin_web.common import (
    not_found, origin_ok, require_csrf, unauthorized, with_auth,
)
from services.admin_web.registry import can_access_page, visible_pages
from services.admin_web.views import preferences_page


ALLOWED_THEMES = {"fluent", "cloudscape", "material"}


ALLOWED_COLOR_MODES = {"light", "dark", "system"}


ALLOWED_DENSITIES = {"comfortable", "compact"}


def register(app: Any) -> None:
    """把外观偏好页的路由挂到 app 上。"""

    @app.get("/admin/preferences")
    async def preferences_get(request: Request):
        """主题与外观偏好页。"""
        values = await with_auth(request)
        if values is None:
            return unauthorized()
        db, principal, pref, csrf = values
        try:
            if not await can_access_page(db, principal, "preferences"):
                return not_found()
            return preferences_page(
                principal=principal,
                csrf=csrf,
                pages=await visible_pages(db, principal),
                theme=pref.theme,
                color_mode=pref.color_mode,
                density=pref.density,
            )
        finally:
            await db.close()

    @app.post("/admin/preferences")
    async def preferences_post(request: Request):
        """保存主题、色温和密度。"""
        values = await with_auth(request)
        if values is None:
            return unauthorized()
        db, principal, pref, _ = values
        try:
            if not origin_ok(request) or not await can_access_page(
                db, principal, "preferences"
            ):
                return not_found()
            form = await request.form()
            if not await require_csrf(db, request, principal, form):
                return not_found()
            theme = str(form.get("theme") or "fluent")
            color_mode = str(form.get("color_mode") or "system")
            density = str(form.get("density") or "comfortable")
            if (
                theme not in ALLOWED_THEMES
                or color_mode not in ALLOWED_COLOR_MODES
                or density not in ALLOWED_DENSITIES
            ):
                return not_found()
            pref.theme = theme
            pref.color_mode = color_mode
            pref.density = density
            pref.version = (pref.version or 1) + 1
            await audit_principal_action(
                db,
                principal,
                action="admin_ui.preferences.update",
                target_type="admin_ui_preference",
                target_id=f"{pref.principal_kind}:{pref.principal_id}",
                tenant_id=principal.tenant_id,
                detail={
                    "theme": theme,
                    "color_mode": color_mode,
                    "density": density,
                },
            )
            await db.commit()
            return RedirectResponse("/admin/preferences?notice=saved", status_code=303)
        finally:
            await db.close()
