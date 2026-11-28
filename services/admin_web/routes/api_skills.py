"""技能资源的 JSON API（V4.10）。

技能比 Agent 多一层**修订历史**：每次更新都先把旧正文与资源快照写进
``skill_revision``，这样正文写坏了可以回滚。这条逻辑与页面路由**共用同一
套**（见 ``skill_repo.add_revision``），不在这里重写。

**仍然只有一份授权判定**：越权、CSRF、页面与组件权限都在
``api_support.authorize`` 里，Python 侧唯一。
"""

from __future__ import annotations

import hashlib
from typing import Any

from ludic.web import Request
from pydantic import ValidationError

from app.core.skill_pack import compute_content_hash
from app.repo.agent_repo import agent_repo
from app.repo.skill_repo import skill_repo
from app.schemas.skill import SkillCreate, SkillUpdate
from services.admin_web.api_support import (
    ApiError,
    api_route,
    authorize,
    json_response,
    read_json_body,
    tenant_of,
)
from services.admin_web.audit import audit_principal_action
from services.admin_web.listing import parse_list_params, search_term

PAGE_CODE = "skills"
COMPONENT_CODE = "skill_editor"


def skill_payload(row: Any, used_by: list[str] | None = None) -> dict[str, Any]:
    """把技能行整理成前端形状。"""
    return {
        "code": row.code,
        "name": row.name,
        "description": row.description or "",
        "allowedTools": [
            item.strip()
            for item in (row.allowed_tools or "").split(",")
            if item.strip()
        ],
        "body": row.body or "",
        "status": row.status or "published",
        "enabled": bool(row.enabled),
        "origin": row.origin or "manual",
        "version": row.version,
        "contentHash": row.content_hash or "",
        "usedBy": list(used_by or []),
        "createdAt": row.created_at.isoformat() if row.created_at else None,
        "updatedAt": row.updated_at.isoformat() if row.updated_at else None,
    }


def _tools_text(value: Any) -> str:
    """把工具列表规整成库里存的逗号分隔文本。"""
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",")]
    elif isinstance(value, list):
        items = [str(item).strip() for item in value]
    else:
        items = []
    return ", ".join(item for item in items if item)


def _field_errors(exc: Exception) -> dict[str, str]:
    """把校验错误压成「字段 → 提示」。"""
    if isinstance(exc, ValidationError):
        errors: dict[str, str] = {}
        for item in exc.errors():
            errors.setdefault(
                str((item.get("loc") or ("form",))[-1]), item.get("msg") or "字段不合法"
            )
        return errors
    return {"body": str(exc)}


def register(app: Any) -> None:
    """把技能的 JSON API 挂到 app 上。"""

    @app.get("/admin/api/skills")
    @api_route
    async def api_skills_list(request: Request):
        """列出当前租户的技能。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=False
        )
        try:
            tenant_id = tenant_of(principal)
            params = parse_list_params(
                request, allowed_sorts=("name", "code", "created")
            )
            search = search_term(params.q)
            rows = await skill_repo.list(
                db,
                limit=params.size,
                offset=params.offset,
                tenant_id=tenant_id,
                search=search or None,
                sort=params.sort,
            )
            total = await skill_repo.count(
                db, tenant_id=tenant_id, search=search or None
            )
            bindings = await agent_repo.skill_binding_map(db, tenant_id)
            return json_response(
                {
                    "items": [
                        skill_payload(row, bindings.get(row.code, []))
                        for row in rows
                    ],
                    "total": total,
                    "page": {
                        "q": params.q,
                        "sort": params.sort,
                        "page": params.page,
                        "size": params.size,
                    },
                }
            )
        finally:
            await db.close()

    @app.get("/admin/api/skills/{code}")
    @api_route
    async def api_skills_get(code: str, request: Request):
        """取单个技能。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=False
        )
        try:
            tenant_id = tenant_of(principal)
            row = await skill_repo.get_by_code(db, code, tenant_id)
            if row is None:
                raise ApiError("missing", "技能不存在", status_code=404)
            bindings = await agent_repo.skill_binding_map(db, tenant_id)
            return json_response(
                {"item": skill_payload(row, bindings.get(row.code, []))}
            )
        finally:
            await db.close()

    @app.post("/admin/api/skills")
    @api_route
    async def api_skills_create(request: Request):
        """创建手动维护的技能。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=True
        )
        try:
            body = await read_json_body(request)
            tenant_id = tenant_of(principal)
            try:
                payload = SkillCreate(
                    code=str(body.get("code") or "").strip(),
                    description=str(body.get("description") or ""),
                    allowed_tools=_tools_text(body.get("allowedTools")),
                    body=str(body.get("body") or ""),
                    status=str(body.get("status") or "published"),
                    enabled=bool(body.get("enabled", True)),
                )
            except (TypeError, ValueError, ValidationError) as exc:
                raise ApiError(
                    "invalid_input",
                    "提交的字段不合法",
                    status_code=400,
                    field_errors=_field_errors(exc),
                ) from exc
            if await skill_repo.get_by_code(db, payload.code, tenant_id):
                raise ApiError("duplicate", "该技能 code 已存在", status_code=409)
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
            return json_response({"item": skill_payload(row)}, status_code=201)
        finally:
            await db.close()

    @app.put("/admin/api/skills/{code}")
    @api_route
    async def api_skills_update(code: str, request: Request):
        """更新技能内容，并把变更写入可回滚的修订表。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=True
        )
        try:
            body = await read_json_body(request)
            tenant_id = tenant_of(principal)
            existing = await skill_repo.get_by_code(db, code, tenant_id)
            if existing is None:
                raise ApiError("missing", "技能不存在", status_code=404)
            try:
                payload = SkillUpdate(
                    description=str(body.get("description") or ""),
                    allowed_tools=_tools_text(body.get("allowedTools")),
                    body=str(body.get("body") or ""),
                    status=str(body.get("status") or existing.status),
                    enabled=bool(body.get("enabled", bool(existing.enabled))),
                )
            except (TypeError, ValueError, ValidationError) as exc:
                raise ApiError(
                    "invalid_input",
                    "提交的字段不合法",
                    status_code=400,
                    field_errors=_field_errors(exc),
                ) from exc
            bindings = await agent_repo.skill_binding_map(db, tenant_id)
            disables_active = (
                bool(existing.enabled)
                and (not payload.enabled or payload.status == "draft")
            )
            if code in bindings and disables_active:
                raise ApiError(
                    "in_use",
                    "该技能正被智能体引用，不能停用或转草稿",
                    status_code=409,
                )
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
                        "is_text": item.is_text,
                        "content": item.content,
                    }
                    for item in resources
                ],
            )
            fields = payload.model_dump(exclude_unset=True)
            fields["enabled"] = (
                1 if fields["enabled"] and fields["status"] == "published" else 0
            )
            fields["content_hash"] = compute_content_hash(
                payload.body or "",
                [(item.path, item.content) for item in resources],
            )
            row = await skill_repo.update(db, code, fields, tenant_id=tenant_id)
            if row is None:
                raise ApiError("missing", "技能不存在", status_code=404)
            await audit_principal_action(
                db,
                principal,
                action="skill.update",
                target_type="skill",
                target_id=code,
                tenant_id=tenant_id,
            )
            await db.commit()
            return json_response({"item": skill_payload(row)})
        finally:
            await db.close()

    @app.post("/admin/api/skills/{code}/archive")
    @api_route
    async def api_skills_archive(code: str, request: Request):
        """归档技能，保留资源与修订以便恢复。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=True
        )
        try:
            tenant_id = tenant_of(principal)
            bindings = await agent_repo.skill_binding_map(db, tenant_id)
            if code in bindings:
                raise ApiError(
                    "in_use",
                    "该技能正被智能体引用，不能归档",
                    status_code=409,
                )
            if not await skill_repo.soft_delete(db, code, tenant_id):
                raise ApiError("missing", "技能不存在", status_code=404)
            await audit_principal_action(
                db,
                principal,
                action="skill.archive",
                target_type="skill",
                target_id=code,
                tenant_id=tenant_id,
            )
            await db.commit()
            return json_response({"ok": True})
        finally:
            await db.close()