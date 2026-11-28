"""撤销删除的 JSON API（V4.10）。

与页面路由 ``routes/undo.py`` 共用同一套快照恢复逻辑：撤销必须满足
**同源 + CSRF + 本人 + 未过期**，并且按**资源类型**判定写权限——撤销一个
LLM 端点不该要求 Agent 编辑权。

权限判定放在**读到快照之后**：只有知道被删的是哪一类资源，才知道该按
哪张表的写权限放行。
"""

from __future__ import annotations

from typing import Any

from ludic.web import Request

from app.models.admin_deleted_resource import AdminDeletedResource
from services.admin_web.api_support import (
    ApiError,
    api_route,
    authorize,
    json_response,
)
from services.admin_web.audit import audit_principal_action
from services.admin_web.common import can_edit_resource
from services.admin_web.deletion import UndoNotAvailable, undo_deletion

# 资源类型 → (页面编码, 组件编码)。
_UNDO_TARGETS = {
    "agent": ("agents", "agent_editor"),
    "llm_endpoint": ("llm", "llm_editor"),
    "mcp_server": ("mcp", "mcp_editor"),
    "workflow": ("workflows", "workflow_editor"),
    "super_admin_account": ("platform_admins", "admin_list"),
}


def register(app: Any) -> None:
    """把撤销路由的 JSON 版本挂到 app 上。"""

    @app.post("/admin/api/undo/{entry_id}")
    @api_route
    async def api_undo(entry_id: int, request: Request):
        """撤销一次删除，把资源按快照原样恢复。"""
        db, principal, _csrf = await authorize(
            request, page_code="", component_code="", write=True
        )
        try:
            peeked = await db.get(AdminDeletedResource, entry_id)
            if peeked is None:
                raise ApiError("not_found", "撤销记录不存在", status_code=404)
            target = _UNDO_TARGETS.get(peeked.resource_type)
            if target is None:
                raise ApiError("not_found", "撤销记录不存在", status_code=404)
            page_code, component_code = target
            if not await can_edit_resource(
                db, principal, page_code=page_code, component_code=component_code
            ):
                raise ApiError("not_found", "页面不可用", status_code=404)
            try:
                resource_type, code = await undo_deletion(
                    db,
                    entry_id=entry_id,
                    actor_id=principal.principal_id,
                    actor_username=principal.username,
                )
            except UndoNotAvailable as exc:
                raise ApiError(
                    "invalid_input", "撤销失败：记录不存在或已过期", status_code=400
                ) from exc
            await audit_principal_action(
                db,
                principal,
                action="resource.undo",
                target_type=resource_type,
                target_id=code,
                tenant_id=principal.tenant_id,
            )
            await db.commit()
            return json_response({"item": {"resourceType": resource_type, "code": code}})
        finally:
            await db.close()
