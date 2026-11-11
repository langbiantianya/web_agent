"""技能定义管理的路由。"""

from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import quote

from ludic.web import Request
from pydantic import ValidationError
from starlette.responses import RedirectResponse

from app.core.skill_pack import compute_content_hash
from app.repo.agent_repo import agent_repo
from app.repo.skill_repo import skill_repo
from app.schemas.skill import SkillCreate, SkillUpdate
from services.admin_web.audit import audit_principal_action
from services.admin_web.listing import parse_list_params, search_term
from services.admin_web.form_state import (
    field_errors, invalid_form_redirect, take_invalid,
)
from services.admin_web.common import (
    as_bool, business_redirect, can_edit_resource, csv_list, flash_from_query,
    not_found, operation_authorized, tenant_id_of, unauthorized, with_auth,
)
from services.admin_web.registry import can_access_page, visible_pages
from services.admin_web.views import skills_page


def register(app: Any) -> None:
    """把技能定义管理的路由挂到 app 上。"""

    @app.get("/admin/skills")
    async def skills_get(request: Request):
        """渲染当前租户的技能定义列表。"""
        values = await with_auth(request)
        if values is None:
            return unauthorized()
        db, principal, pref, csrf = values
        try:
            if not await can_access_page(db, principal, "skills"):
                return not_found()
            flash, flash_kind = flash_from_query(request)
            tenant_id = tenant_id_of(principal)
            # 深链接到编辑面板（UX-09）：``?resource=<code>`` 决定打开
            # 哪个对象的编辑面板。
            resource = (request.query_params.get("resource") or "").strip()
            invalid_nonce = request.query_params.get("invalid")
            if resource:
                edit_values, edit_errors = take_invalid(invalid_nonce)
                create_values, create_errors = {}, {}
            else:
                create_values, create_errors = take_invalid(invalid_nonce)
                edit_values, edit_errors = {}, {}
            # 列表页的搜索 / 排序 / 分页（UX-06）。
            list_params = parse_list_params(
                request,
                allowed_sorts=("name", "code"),
            )
            search = search_term(list_params.q)
            total = await skill_repo.count(
                db, tenant_id=tenant_id, search=search or None
            )
            rows = await skill_repo.list(
                db,
                limit=list_params.size,
                offset=list_params.offset,
                tenant_id=tenant_id,
                search=search or None,
                sort=list_params.sort,
            )
            editing_row = None
            if resource:
                editing_row = next(
                    (row for row in rows if row.code == resource), None
                )
                if editing_row is None:
                    editing_row = await skill_repo.get_by_code(
                        db, resource, tenant_id
                    )
            return skills_page(
                principal=principal,
                csrf=csrf,
                pages=await visible_pages(db, principal),
                rows=rows,
                used_by=await agent_repo.skill_binding_map(db, tenant_id),
                theme=pref.theme,
                color_mode=pref.color_mode,
                density=pref.density,
                can_write=await can_edit_resource(
                    db,
                    principal,
                    page_code="skills",
                    component_code="skill_editor",
                ),
                flash=flash,
                flash_kind=flash_kind,
                create_errors=create_errors,
                create_values=create_values,
                list_params=list_params,
                list_total=total,
                editing_row=editing_row,
                edit_errors=(
                    {resource: edit_errors} if resource and edit_errors else {}
                ),
                edit_values=(
                    {resource: edit_values} if resource and edit_values else {}
                ),
            )
        finally:
            await db.close()

    @app.post("/admin/skills")
    async def skills_post(request: Request):
        """创建手动维护的技能定义。"""
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
                page_code="skills",
                component_code="skill_editor",
                form=form,
            ):
                return not_found()
            submitted = {
                "code": str(form.get("code") or ""),
                "description": str(form.get("description") or ""),
                "allowed_tools": ", ".join(
                    csv_list(form.get("allowed_tools"))
                ),
                "body": str(form.get("body") or ""),
                "status": str(form.get("status") or "published"),
                "enabled": as_bool(form.get("enabled"), True),
            }
            try:
                payload = SkillCreate(**submitted)
            except ValidationError as exc:
                # 校验失败：保留已填内容并逐字段标错（UX-03）。
                return RedirectResponse(
                    invalid_form_redirect(
                        "/admin/skills",
                        submitted,
                        field_errors(exc),
                    ),
                    status_code=303,
                )
            tenant_id = tenant_id_of(principal)
            if await skill_repo.get_by_code(db, payload.code, tenant_id):
                return business_redirect("/admin/skills", error="duplicate")
            row = await skill_repo.create(
                db,
                tenant_id=tenant_id,
                code=payload.code,
                name=payload.code,
                description=payload.description,
                allowed_tools=payload.allowed_tools,
                body=payload.body,
                status=payload.status,
                enabled=1 if payload.enabled and payload.status == "published" else 0,
                origin="manual",
                content_hash=compute_content_hash(payload.body, []),
            )
            await audit_principal_action(
                db,
                principal,
                action="skill.create",
                target_type="skill",
                target_id=row.code,
                tenant_id=tenant_id,
            )
            await db.commit()
            return business_redirect("/admin/skills", notice="created")
        finally:
            await db.close()

    @app.post("/admin/skills/{code}/update")
    async def skills_update(code: str, request: Request):
        """更新技能内容，并把变更写入可回滚的修订表。"""
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
                page_code="skills",
                component_code="skill_editor",
                form=form,
            ):
                return not_found()
            tenant_id = tenant_id_of(principal)
            existing = await skill_repo.get_by_code(db, code, tenant_id)
            if existing is None:
                return business_redirect("/admin/skills", error="missing")
            try:
                payload = SkillUpdate(
                    description=str(form.get("description") or ""),
                    allowed_tools=", ".join(csv_list(form.get("allowed_tools"))),
                    body=str(form.get("body") or ""),
                    status=str(form.get("status") or existing.status),
                    enabled=as_bool(form.get("enabled"), bool(existing.enabled)),
                )
            except ValidationError as exc:
                # 校验失败：回到编辑面板，保留已填内容并逐字段标错
                # （UX-03 + UX-09）。
                submitted = {
                    "description": str(form.get("description") or ""),
                    "allowed_tools": ", ".join(
                        csv_list(form.get("allowed_tools"))
                    ),
                    "body": str(form.get("body") or ""),
                    "status": str(form.get("status") or existing.status),
                }
                return RedirectResponse(
                    invalid_form_redirect(
                        f"/admin/skills?resource={quote(code, safe='')}",
                        submitted,
                        field_errors(exc),
                    ),
                    status_code=303,
                )
            used_by = await agent_repo.skill_binding_map(db, tenant_id)
            disables_active_skill = (
                bool(existing.enabled)
                and (not payload.enabled or payload.status == "draft")
            )
            if code in used_by and disables_active_skill:
                return business_redirect("/admin/skills", error="in_use")
            resources = await skill_repo.list_resources(db, existing.id)
            await skill_repo.add_revision(
                db,
                existing.id,
                existing.version,
                existing.body or "",
                [
                    {
                        "path": item.path,
                        "sha256": hashlib.sha256(item.content).hexdigest(),
                        "is_text": bool(item.is_text),
                        "size_bytes": item.size_bytes,
                    }
                    for item in resources
                ],
                principal.username,
                "admin web update",
                resource_blobs=[
                    {
                        "path": item.path,
                        "mime_type": item.mime_type,
                        "is_text": bool(item.is_text),
                        "content": item.content,
                    }
                    for item in resources
                ],
            )
            fields = payload.model_dump(exclude_unset=True)
            fields["enabled"] = (
                1
                if fields["enabled"] and fields["status"] == "published"
                else 0
            )
            fields["content_hash"] = compute_content_hash(
                payload.body or "",
                [(item.path, item.content) for item in resources],
            )
            await skill_repo.update(
                db,
                code,
                fields,
                tenant_id=tenant_id,
            )
            await audit_principal_action(
                db,
                principal,
                action="skill.update",
                target_type="skill",
                target_id=code,
                tenant_id=tenant_id,
            )
            await db.commit()
            return business_redirect("/admin/skills", notice="updated")
        finally:
            await db.close()

    @app.post("/admin/skills/{code}/archive")
    async def skills_archive(code: str, request: Request):
        """归档技能，保留资源和修订以便恢复。"""
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
                page_code="skills",
                component_code="skill_editor",
                form=form,
            ):
                return not_found()
            tenant_id = tenant_id_of(principal)
            used_by = await agent_repo.skill_binding_map(db, tenant_id)
            if code in used_by:
                return business_redirect("/admin/skills", error="in_use")
            if not await skill_repo.soft_delete(db, code, tenant_id):
                return business_redirect("/admin/skills", error="missing")
            await audit_principal_action(
                db,
                principal,
                action="skill.archive",
                target_type="skill",
                target_id=code,
                tenant_id=tenant_id,
            )
            await db.commit()
            return business_redirect("/admin/skills", notice="deleted")
        finally:
            await db.close()
