"""LLM 端点资源的 JSON API（V4.10）。

给 SvelteKit 前端用。规则与页面路由一致：同一套仓储与 schema、同一套
Casbin 判定，只是请求体是 JSON、CSRF 令牌走 ``X-CSRF-Token`` 头。

**明文 Key 不回显**：读取形状里只有 ``hasApiKey``，没有 Key 本身。
"""

from __future__ import annotations

from typing import Any

from ludic.web import Request
from pydantic import ValidationError

from app.core.crypto import encrypt
from app.repo.agent_repo import agent_repo
from app.repo.llm_endpoint_repo import llm_endpoint_repo
from app.schemas.llm_endpoint import LlmEndpointCreate, LlmEndpointUpdate
from services.admin_web.api_support import (
    ApiError,
    api_route,
    authorize,
    json_response,
    read_json_body,
)
from services.admin_web.audit import audit_principal_action
from services.admin_web.deletion import record_deletion
from services.admin_web.listing import parse_list_params, search_term

PAGE_CODE = "llm"
COMPONENT_CODE = "llm_editor"


def endpoint_payload(row: Any, *, bound_agents: int = 0) -> dict[str, Any]:
    """把 LLM 端点行整理成前端形状（不含任何 Key）。"""
    return {
        "code": row.code,
        "name": row.name,
        "baseUrl": row.base_url,
        "model": row.model or "",
        "priority": row.priority,
        "enabled": bool(row.enabled),
        "hasApiKey": bool(row.api_key_cipher),
        "boundAgents": bound_agents,
        "createdAt": row.created_at.isoformat() if row.created_at else None,
        "updatedAt": row.updated_at.isoformat() if row.updated_at else None,
    }


#: API 字段名与 schema 字段名的对照。
#:
#: 前端表单用 camelCase，pydantic 的报错用 snake_case。不做这层翻译的话，
#: 错误会标到前端找不到的字段上——用户看到「没有报错」，实际是报错了。
_FIELD_NAMES = {
    "base_url": "baseUrl",
    "api_key": "apiKey",
}


def _field_errors(exc: Exception) -> dict[str, str]:
    """把校验错误压成「字段 → 提示」，字段名用 API 的形状。"""
    if isinstance(exc, ValidationError):
        errors: dict[str, str] = {}
        for item in exc.errors():
            name = str((item.get("loc") or ("form",))[-1])
            errors.setdefault(
                _FIELD_NAMES.get(name, name), item.get("msg") or "字段不合法"
            )
        return errors
    return {"baseUrl": str(exc)}


def register(app: Any) -> None:
    """把 LLM 端点的 JSON API 挂到 app 上。"""

    @app.get("/admin/api/llm")
    @api_route
    async def api_llm_list(request: Request):
        """列出全部 LLM 端点。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=False
        )
        try:
            params = parse_list_params(
                request, allowed_sorts=("name", "code", "created", "priority")
            )
            search = search_term(params.q)
            rows = await llm_endpoint_repo.list(
                db,
                limit=params.size,
                offset=params.offset,
                search=search or None,
                sort=params.sort,
            )
            total = await llm_endpoint_repo.count(db, search=search or None)
            bindings = await agent_repo.llm_endpoint_binding_map(db)
            return json_response(
                {
                    "items": [
                        endpoint_payload(
                            row, bound_agents=len(bindings.get(row.code, []))
                        )
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

    @app.get("/admin/api/llm/{code}")
    @api_route
    async def api_llm_get(code: str, request: Request):
        """取单个 LLM 端点。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=False
        )
        try:
            row = await llm_endpoint_repo.get_by_code(db, code)
            if row is None:
                raise ApiError("missing", "LLM 端点不存在", status_code=404)
            bindings = await agent_repo.llm_endpoint_binding_map(db)
            return json_response(
                {
                    "item": endpoint_payload(
                        row, bound_agents=len(bindings.get(row.code, []))
                    )
                }
            )
        finally:
            await db.close()

    @app.post("/admin/api/llm")
    @api_route
    async def api_llm_create(request: Request):
        """新增 LLM 端点。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=True
        )
        try:
            body = await read_json_body(request)
            try:
                payload = LlmEndpointCreate(
                    code=str(body.get("code") or "").strip(),
                    name=str(body.get("name") or ""),
                    base_url=str(body.get("baseUrl") or ""),
                    model=str(body.get("model") or ""),
                    api_key=str(body.get("apiKey") or ""),
                    priority=int(body.get("priority") or 100),
                    enabled=bool(body.get("enabled", True)),
                )
            except (TypeError, ValueError, ValidationError) as exc:
                raise ApiError(
                    "invalid_input",
                    "提交的字段不合法",
                    status_code=400,
                    field_errors=_field_errors(exc),
                ) from exc
            if await llm_endpoint_repo.get_by_code(db, payload.code):
                raise ApiError(
                    "duplicate", "该 LLM 端点 code 已存在", status_code=409
                )
            row = await llm_endpoint_repo.create(
                db,
                code=payload.code,
                name=payload.name or payload.code,
                base_url=payload.base_url,
                model=payload.model,
                api_key_cipher=encrypt(payload.api_key),
                priority=payload.priority,
                enabled=1 if payload.enabled else 0,
            )
            await audit_principal_action(
                db,
                principal,
                action="llm.create",
                target_type="llm_endpoint",
                target_id=row.code,
                tenant_id=principal.tenant_id,
            )
            await db.commit()
            return json_response({"item": endpoint_payload(row)}, status_code=201)
        finally:
            await db.close()

    @app.put("/admin/api/llm/{code}")
    @api_route
    async def api_llm_update(code: str, request: Request):
        """更新 LLM 端点；空 API Key 表示保留旧密钥。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=True
        )
        try:
            body = await read_json_body(request)
            if await llm_endpoint_repo.get_by_code(db, code) is None:
                raise ApiError("missing", "LLM 端点不存在", status_code=404)
            fields: dict[str, Any] = {}
            for key, column in (
                ("name", "name"),
                ("baseUrl", "base_url"),
                ("model", "model"),
            ):
                if key in body:
                    fields[column] = str(body.get(key) or "")
            if "priority" in body:
                fields["priority"] = int(body.get("priority") or 0)
            if "enabled" in body:
                fields["enabled"] = 1 if body.get("enabled") else 0
            api_key = str(body.get("apiKey") or "")
            try:
                payload = LlmEndpointUpdate(
                    api_key=api_key if api_key else None, **fields
                )
            except (TypeError, ValueError, ValidationError) as exc:
                raise ApiError(
                    "invalid_input",
                    "提交的字段不合法",
                    status_code=400,
                    field_errors=_field_errors(exc),
                ) from exc
            updates = payload.model_dump(exclude_unset=True)
            updates.pop("api_key", None)
            if api_key:
                updates["api_key_cipher"] = encrypt(api_key)
            row = await llm_endpoint_repo.update(db, code, updates)
            if row is None:
                raise ApiError("missing", "LLM 端点不存在", status_code=404)
            await audit_principal_action(
                db,
                principal,
                action="llm.update",
                target_type="llm_endpoint",
                target_id=code,
                tenant_id=principal.tenant_id,
            )
            await db.commit()
            return json_response({"item": endpoint_payload(row)})
        finally:
            await db.close()

    @app.post("/admin/api/llm/{code}/toggle")
    @api_route
    async def api_llm_toggle(code: str, request: Request):
        """切换 LLM 端点的启用状态。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=True
        )
        try:
            body = await read_json_body(request)
            enabled = bool(body.get("enabled"))
            if await llm_endpoint_repo.get_by_code(db, code) is None:
                raise ApiError("missing", "LLM 端点不存在", status_code=404)
            if not enabled and await agent_repo.count_for_llm_endpoint(db, code):
                raise ApiError(
                    "endpoint_in_use",
                    "该连接仍被 Agent 引用，不能停用",
                    status_code=409,
                )
            row = await llm_endpoint_repo.update(
                db, code, {"enabled": 1 if enabled else 0}
            )
            if row is None:
                raise ApiError("missing", "LLM 端点不存在", status_code=404)
            await audit_principal_action(
                db,
                principal,
                action="llm.toggle",
                target_type="llm_endpoint",
                target_id=code,
                tenant_id=principal.tenant_id,
                detail={"enabled": bool(row.enabled)},
            )
            await db.commit()
            return json_response({"item": endpoint_payload(row)})
        finally:
            await db.close()

    @app.delete("/admin/api/llm/{code}")
    @api_route
    async def api_llm_delete(code: str, request: Request):
        """删除 LLM 端点。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=True
        )
        try:
            if await llm_endpoint_repo.get_by_code(db, code) is None:
                raise ApiError("missing", "LLM 端点不存在", status_code=404)
            if await agent_repo.count_for_llm_endpoint(db, code):
                raise ApiError(
                    "endpoint_in_use",
                    "该连接仍被 Agent 引用，不能删除",
                    status_code=409,
                )
            existing = await llm_endpoint_repo.get_by_code(db, code)
            if existing is None:
                raise ApiError("missing", "LLM 端点不存在", status_code=404)
            # 删除前留快照：误删能在撤销窗口内原样恢复（与页面路由一致）。
            entry = await record_deletion(
                db,
                resource_type="llm_endpoint",
                row=existing,
                actor_id=principal.principal_id,
                actor_username=principal.username,
            )
            if not await llm_endpoint_repo.delete(db, code):
                raise ApiError("missing", "LLM 端点不存在", status_code=404)
            await audit_principal_action(
                db,
                principal,
                action="llm.delete",
                target_type="llm_endpoint",
                target_id=code,
                tenant_id=principal.tenant_id,
            )
            await db.commit()
            return json_response({"ok": True, "undoId": entry.id})
        finally:
            await db.close()