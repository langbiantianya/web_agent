"""Agent 配置管理的路由。"""

from __future__ import annotations

from typing import Any

from ludic.web import Request
from pydantic import ValidationError

from app.core.crypto import encrypt
from app.core.skill_names import SkillConfigError, normalize_skill_codes
from app.core.tools import normalize_tool_names
from app.repo.agent_repo import agent_repo
from app.repo.mcp_repo import mcp_tool_repo
from app.repo.skill_repo import skill_repo
from app.schemas.agent import AgentCreate, AgentUpdate
from services.admin_web.audit import audit_principal_action
from services.admin_web.common import (
    as_bool, business_redirect, can_edit_resource, csv_list, flash_from_query,
    not_found, operation_authorized, tenant_id_of, unauthorized, with_auth,
)
from services.admin_web.registry import can_access_page, visible_pages
from services.admin_web.views import agents_page


async def _agent_form_values(db, tenant_id: str, form) -> dict:
    """解析并校验 Agent 表单，同时过滤失效 Skill/MCP 绑定。"""
    skills = [row.code for row in await skill_repo.list(
        db, only_enabled=True, limit=1000, tenant_id=tenant_id
    )]
    mcp_names = await mcp_tool_repo.qualified_names(db, tenant_id=tenant_id)
    tools = normalize_tool_names(csv_list(form.get("tools")), mcp_names)
    try:
        normalized_skills = normalize_skill_codes(csv_list(form.get("skills")), set(skills))
    except SkillConfigError as exc:
        raise ValueError("invalid_skill_binding") from exc
    return {
        "name": str(form.get("name") or ""),
        "description": str(form.get("description") or ""),
        "system_prompt": str(form.get("system_prompt") or ""),
        "model": str(form.get("model") or ""),
        "base_url": str(form.get("base_url") or ""),
        "api_key": str(form.get("api_key") or ""),
        "tools": tools,
        "skills": [] if normalized_skills is None else normalized_skills,
        "enabled": as_bool(form.get("enabled"), True),
    }


def register(app: Any) -> None:
    """把 Agent 配置管理的路由挂到 app 上。"""

    @app.get("/admin/agents")
    async def agents_get(request: Request):
        """渲染当前租户的 Agent 配置列表。"""
        values = await with_auth(request)
        if values is None:
            return unauthorized()
        db, principal, pref, csrf = values
        try:
            if not await can_access_page(db, principal, "agents"):
                return not_found()
            flash, _ = flash_from_query(request)
            return agents_page(
                principal=principal,
                csrf=csrf,
                pages=await visible_pages(db, principal),
                rows=await agent_repo.list(
                    db,
                    limit=500,
                    tenant_id=tenant_id_of(principal),
                ),
                theme=pref.theme,
                color_mode=pref.color_mode,
                density=pref.density,
                can_write=await can_edit_resource(
                    db,
                    principal,
                    page_code="agents",
                    component_code="agent_editor",
                ),
                flash=flash,
            )
        finally:
            await db.close()

    @app.post("/admin/agents")
    async def agents_post(request: Request):
        """通过共享 Agent 仓储创建租户配置。"""
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
            try:
                fields = await _agent_form_values(db, tenant_id_of(principal), form)
                payload = AgentCreate(
                    code=str(form.get("code") or ""),
                    **fields,
                )
            except (ValidationError, ValueError):
                return business_redirect("/admin/agents", error="invalid")
            if await agent_repo.get_by_code(db, payload.code):
                return business_redirect("/admin/agents", error="duplicate")
            row = await agent_repo.create(
                db,
                code=payload.code,
                tenant_id=tenant_id_of(principal),
                name=payload.name,
                description=payload.description,
                system_prompt=payload.system_prompt,
                model=payload.model,
                base_url=payload.base_url,
                api_key_cipher=encrypt(payload.api_key),
                tools=payload.tools,
                skills=payload.skills,
                enabled=1 if payload.enabled else 0,
            )
            await audit_principal_action(
                db,
                principal,
                action="agent.create",
                target_type="agent",
                target_id=row.code,
                tenant_id=tenant_id_of(principal),
            )
            await db.commit()
            return business_redirect("/admin/agents", notice="created")
        finally:
            await db.close()

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
            try:
                raw = await _agent_form_values(db, tenant_id_of(principal), form)
                if not raw["api_key"]:
                    raw.pop("api_key")
                payload = AgentUpdate(**raw)
            except (ValidationError, ValueError):
                return business_redirect("/admin/agents", error="invalid")
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
            row = await agent_repo.update(
                db,
                code,
                {"enabled": 1 if as_bool(form.get("enabled")) else 0},
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
            return business_redirect("/admin/agents", notice="deleted")
        finally:
            await db.close()
