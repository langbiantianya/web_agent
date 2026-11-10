"""管理端「撤销删除」的路由。

删除是硬删除，误删后没有退路。这里的撤销通道把删除前的行快照存下
（见 :mod:`services.admin_web.deletion`），在有限窗口内允许原样恢复。

撤销必须满足三件事：

1. **同源 + CSRF**：与其它写操作走同一套鉴权助手；
2. **本人**：只有发起删除的那个管理员能撤销；
3. **未过期**：快照过期后一律拒绝。

权限按**资源类型**判定：撤销一个 Agent 要有 Agent 的写权限，
撤销一个 LLM 端点要有 LLM 端点的写权限——不能用一个页面的写权限
去恢复另一个页面的资源。
"""

from __future__ import annotations

from typing import Any

from ludic.web import Request
from starlette.responses import RedirectResponse

from app.models.admin_deleted_resource import AdminDeletedResource
from services.admin_web.audit import audit_principal_action
from services.admin_web.common import (
    business_redirect, not_found, operation_authorized, unauthorized, with_auth,
)
from services.admin_web.deletion import UndoNotAvailable, undo_deletion

# 资源类型 → (列表页路径, 页面编码, 组件编码, 重定向目标)。
_UNDO_TARGETS = {
    "agent": ("/admin/agents", "agents", "agent_editor", "/admin/agents"),
    "llm_endpoint": ("/admin/llm", "llm", "llm_editor", "/admin/llm"),
    "mcp_server": ("/admin/mcp", "mcp", "mcp_editor", "/admin/mcp"),
    "workflow": (
        "/admin/workflows",
        "workflows",
        "workflow_editor",
        "/admin/workflows",
    ),
    "super_admin_account": (
        "/admin/platform-admins",
        "platform_admins",
        "admin_list",
        "/admin/platform-admins",
    ),
}


def register(app: Any) -> None:
    """把撤销路由挂到 app 上。"""

    @app.post("/admin/undo/{entry_id}")
    async def undo(entry_id: int, request: Request):
        """撤销一次删除，把资源按快照原样恢复。"""
        values = await with_auth(request)
        if values is None:
            return unauthorized()
        db, principal, _, _ = values
        try:
            form = await request.form()
            # 先看这条快照属于哪类资源，再按**那一类**的写权限判定：
            # 撤销一个 LLM 端点不该要求 Agent 编辑权，撤销一个平台管理员
            # 也不该要求 Agent 编辑权。之前的实现把页面写死成
            # ``agents``，于是「有 Agent 权限但没有 LLM 权限」的管理员
            # 撤销不了自己刚删的 LLM 端点。
            peeked = await db.get(AdminDeletedResource, entry_id)
            if peeked is None:
                return business_redirect("/admin/agents", error="undo_failed")
            target = _UNDO_TARGETS.get(peeked.resource_type)
            if target is None:
                return business_redirect("/admin/agents", error="undo_failed")
            _, page_code, component_code, redirect_path = target
            if not await operation_authorized(
                request,
                db,
                principal,
                page_code=page_code,
                component_code=component_code,
                form=form,
            ):
                return not_found()
            try:
                resource_type, code = await undo_deletion(
                    db,
                    entry_id=entry_id,
                    actor_id=principal.principal_id,
                    actor_username=principal.username,
                )
            except UndoNotAvailable:
                return business_redirect(redirect_path, error="undo_failed")
            await audit_principal_action(
                db,
                principal,
                action="resource.undo",
                target_type=resource_type,
                target_id=code,
                tenant_id=principal.tenant_id,
            )
            await db.commit()
            return RedirectResponse(
                f"{redirect_path}?notice=restored",
                status_code=303,
            )
        finally:
            await db.close()
