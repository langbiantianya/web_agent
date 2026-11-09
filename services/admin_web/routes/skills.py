"""技能定义管理的路由。"""

from __future__ import annotations

import hashlib
from typing import Any

from ludic.web import Request
from pydantic import ValidationError

from app.core.skill_pack import compute_content_hash
from app.repo.skill_repo import skill_repo
from app.schemas.skill import SkillCreate, SkillUpdate
from services.admin_web.audit import audit_principal_action
from services.admin_web.common import (
    as_bool, business_redirect, can_edit_resource, flash_from_query, not_found,
    operation_authorized, tenant_id_of, unauthorized, with_auth,
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
            flash, _ = flash_from_query(request)
            return skills_page(
                principal=principal,
                csrf=csrf,
                pages=await visible_pages(db, principal),
                rows=await skill_repo.list(
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
                    page_code="skills",
                    component_code="skill_editor",
                ),
                flash=flash,
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
            try:
                payload = SkillCreate(
                    code=str(form.get("code") or ""),
                    description=str(form.get("description") or ""),
                    body=str(form.get("body") or ""),
                    status=str(form.get("status") or "published"),
                    enabled=as_bool(form.get("enabled"), True),
                )
            except ValidationError:
                return business_redirect("/admin/skills", error="invalid")
            tenant_id = tenant_id_of(principal)
            if await skill_repo.get_by_code(db, payload.code, tenant_id):
                return business_redirect("/admin/skills", error="duplicate")
            row = await skill_repo.create(
                db,
                tenant_id=tenant_id,
                code=payload.code,
                name=payload.code,
                description=payload.description,
                body=payload.body,
                status=payload.status,
                enabled=1 if payload.enabled else 0,
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
                    body=str(form.get("body") or ""),
                    status=str(form.get("status") or existing.status),
                    enabled=as_bool(form.get("enabled"), bool(existing.enabled)),
                )
            except ValidationError:
                return business_redirect("/admin/skills", error="invalid")
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
            fields["enabled"] = 1 if fields["enabled"] else 0
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
