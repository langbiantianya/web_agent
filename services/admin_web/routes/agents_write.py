"""Agent 配置的写操作（更新 / 启停 / 删除）。

从 ``agents.py`` 拆出来：单文件行数上限是 350 行，而 Agent 的写操作
连同校验失败的字段级回显已经占了近半。拆分只切代码位置，路由的路径、
方法与注册顺序都没变。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from ludic.web import Request
from pydantic import ValidationError
from starlette.responses import RedirectResponse

from app.core.crypto import encrypt
from app.repo.agent_repo import agent_repo
from app.repo.workflow_repo import workflow_repo
from app.schemas.agent import AgentUpdate
from services.admin_web.agent_forms import (
    _agent_form_values,
    _business_field_errors,
)
from services.admin_web.audit import audit_principal_action
from services.admin_web.common import (
    as_bool, business_redirect, not_found, operation_authorized, tenant_id_of,
    unauthorized, with_auth,
)
from services.admin_web.deletion import record_deletion
from services.admin_web.form_state import field_errors, invalid_form_redirect


def register(app: Any) -> None:
    """把 Agent 的写操作路由挂到 app 上。"""

    @app.post("/admin/agents/{code}/update")
    async def agents_update(code: str, request: Request):
        """更新当前租户的 Agent 配置。"""
        values = await with_auth(request)
        if values is None:
            return unauthorized()
        db, principal, _, _ = values
        try:
            form = await request.form()
            if not await operation_authorized(
                request,
                db,
                principal,
                page_code="agents",
                component_code="agent_editor",
                form=form,
            ):
                return not_found()
            existing = await agent_repo.get_by_code(
                db, code, tenant_id=tenant_id_of(principal)
            )
            if existing is None:
                return business_redirect("/admin/agents", error="missing")
            submitted = {
                "name": str(form.get("name") or ""),
                "description": str(form.get("description") or ""),
                "system_prompt": str(form.get("system_prompt") or ""),
                "model": str(form.get("model") or ""),
                "llm_endpoint_code": str(
                    form.get("llm_endpoint_code") or ""
                ),
            }
            try:
                raw = await _agent_form_values(
                    db,
                    tenant_id_of(principal),
                    form,
                    enabled_default=bool(existing.enabled),
                )
                api_key = raw.pop("api_key", "")
                if api_key:
                    raw["api_key"] = api_key
                payload = AgentUpdate(**raw)
            except ValidationError as exc:
                # 校验失败：保留已填内容并逐字段标错（UX-03）。
                return RedirectResponse(
                    invalid_form_redirect(
                        f"/admin/agents?resource={quote(code, safe='')}",
                        submitted,
                        field_errors(exc),
                    ),
                    status_code=303,
                )
            except ValueError as exc:
                return RedirectResponse(
                    invalid_form_redirect(
                        f"/admin/agents?resource={quote(code, safe='')}",
                        submitted,
                        _business_field_errors(exc),
                    ),
                    status_code=303,
                )
            fields = payload.model_dump(exclude_unset=True)
            if "api_key" in fields:
                fields["api_key_cipher"] = encrypt(fields.pop("api_key"))
            if "enabled" in fields:
                fields["enabled"] = 1 if fields["enabled"] else 0
            await agent_repo.update(
                db,
                code,
                fields,
                tenant_id=tenant_id_of(principal),
            )
            await audit_principal_action(
                db,
                principal,
                action="agent.update",
                target_type="agent",
                target_id=code,
                tenant_id=tenant_id_of(principal),
            )
            await db.commit()
            return business_redirect("/admin/agents", notice="updated")
        finally:
            await db.close()

    @app.post("/admin/agents/{code}/toggle")
    async def agents_toggle(code: str, request: Request):
        """切换当前租户 Agent 的启用状态。"""
        values = await with_auth(request)
        if values is None:
            return unauthorized()
        db, principal, _, _ = values
        try:
            form = await request.form()
            if not await operation_authorized(
                request,
                db,
                principal,
                page_code="agents",
                component_code="agent_editor",
                form=form,
            ):
                return not_found()
            existing = await agent_repo.get_by_code(
                db, code, tenant_id=tenant_id_of(principal)
            )
            if existing is None:
                return business_redirect("/admin/agents", error="missing")
            next_enabled = as_bool(form.get("enabled"))
            if (
                existing.enabled
                and not next_enabled
                and await workflow_repo.agents_using(db, code)
            ):
                return business_redirect(
                    "/admin/agents", error="agent_in_use"
                )
            row = await agent_repo.update(
                db,
                code,
                {"enabled": 1 if next_enabled else 0},
                tenant_id=tenant_id_of(principal),
            )
            if row is None:
                return business_redirect("/admin/agents", error="missing")
            await audit_principal_action(
                db,
                principal,
                action="agent.toggle",
                target_type="agent",
                target_id=code,
                tenant_id=tenant_id_of(principal),
                detail={"enabled": bool(row.enabled)},
            )
            await db.commit()
            return business_redirect("/admin/agents", notice="toggled")
        finally:
            await db.close()

    @app.post("/admin/agents/{code}/delete")
    async def agents_delete(code: str, request: Request):
        """删除当前租户的 Agent。"""
        values = await with_auth(request)
        if values is None:
            return unauthorized()
        db, principal, _, _ = values
        try:
            form = await request.form()
            if not await operation_authorized(
                request,
                db,
                principal,
                page_code="agents",
                component_code="agent_editor",
                form=form,
            ):
                return not_found()
            existing = await agent_repo.get_by_code(
                db, code, tenant_id=tenant_id_of(principal)
            )
            if existing is None:
                return business_redirect("/admin/agents", error="missing")
            if await workflow_repo.agents_using(db, code):
                return business_redirect(
                    "/admin/agents", error="agent_in_use"
                )
            # 删除前留快照：误删能在撤销窗口内原样恢复（UX-01）。
            entry = await record_deletion(
                db,
                resource_type="agent",
                row=existing,
                actor_id=principal.principal_id,
                actor_username=principal.username,
            )
            if not await agent_repo.delete(
                db, code, tenant_id=tenant_id_of(principal)
            ):
                return business_redirect("/admin/agents", error="missing")
            await audit_principal_action(
                db,
                principal,
                action="agent.delete",
                target_type="agent",
                target_id=code,
                tenant_id=tenant_id_of(principal),
            )
            await db.commit()
            return RedirectResponse(
                f"/admin/agents?notice=deleted&undo={entry.id}",
                status_code=303,
            )
        finally:
            await db.close()
