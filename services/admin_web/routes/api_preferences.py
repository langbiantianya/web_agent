"""管理员外观偏好的 JSON API（V4.10）。

主题、色温与密度只有三个白名单值。判定与页面路由
``routes/preferences.py`` 一致：不认识的取值一律拒绝，而不是悄悄存进去
——否则下一次渲染会回退到默认值，管理员会以为自己改了却没生效。
"""

from __future__ import annotations

from typing import Any

from ludic.web import Request

from services.admin_web.api_support import (
    ApiError,
    api_route,
    authorize,
    json_response,
    read_json_body,
)
from services.admin_web.audit import audit_principal_action
from services.admin_web.common import preference_of
from services.admin_web.preference_values import (
    ALLOWED_COLOR_MODES,
    ALLOWED_DENSITIES,
    ALLOWED_THEMES,
)

PAGE_CODE = "preferences"
COMPONENT_CODE = "theme_form"

THEMES = ("fluent", "cloudscape", "material")
COLOR_MODES = ("light", "dark", "system")
DENSITIES = ("comfortable", "compact")


def _preferences_payload(pref: Any) -> dict[str, Any]:
    """把偏好记录投影成前端形状。"""
    return {
        "theme": pref.theme,
        "colorMode": pref.color_mode,
        "density": pref.density,
        "options": {
            "themes": list(THEMES),
            "colorModes": list(COLOR_MODES),
            "densities": list(DENSITIES),
        },
    }


def register(app: Any) -> None:
    """把外观偏好的 JSON API 挂到 app 上。"""

    @app.get("/admin/api/preferences")
    @api_route
    async def api_preferences_get(request: Request):
        """读取当前管理员的外观偏好。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=False
        )
        try:
            pref = await preference_of(db, principal)
            return json_response(_preferences_payload(pref))
        finally:
            await db.close()

    @app.put("/admin/api/preferences")
    @api_route
    async def api_preferences_update(request: Request):
        """保存主题、色温与密度。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=True
        )
        try:
            body = await read_json_body(request)
            theme = str(body.get("theme") or "fluent")
            color_mode = str(body.get("colorMode") or "system")
            density = str(body.get("density") or "comfortable")
            if (
                theme not in ALLOWED_THEMES
                or color_mode not in ALLOWED_COLOR_MODES
                or density not in ALLOWED_DENSITIES
            ):
                raise ApiError(
                    "invalid_input",
                    "外观取值不在允许范围内",
                    status_code=400,
                    field_errors={
                        "theme": "取值不合法",
                        "colorMode": "取值不合法",
                        "density": "取值不合法",
                    },
                )
            pref = await preference_of(db, principal)
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
            return json_response(_preferences_payload(pref))
        finally:
            await db.close()

