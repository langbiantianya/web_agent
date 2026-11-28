"""工作流资源的 JSON API（V4.10）。

**画布不在这一版**：拖拽编排器（67 KB）作为 island 保留，仍由 Python 渲染
（见 [V4.10](../../app/design/v4.10-sveltekit-admin-ui.md) 的阶段 D）。这里
只管**列表与元信息**：新建、改名、启停、删除。

因此本模块刻意**不暴露图定义的写入口**——图仍然只能从画布保存，避免出现
两条写路径。图定义只在读取时原样带回，供画布 island 使用。
"""

from __future__ import annotations

from typing import Any

from ludic.web import Request
from pydantic import ValidationError

from app.repo.agent_repo import agent_repo
from app.repo.workflow_repo import workflow_repo
from services.admin_web.api_support import (
    ApiError,
    api_route,
    authorize,
    json_response,
    read_json_body,
    tenant_of,
)
from services.admin_web.audit import audit_principal_action
from services.admin_web.deletion import record_deletion
from services.admin_web.listing import parse_list_params, search_term
from services.admin_web.workflow_save import prepare_workflow_create

PAGE_CODE = "workflows"
COMPONENT_CODE = "workflow_editor"


class _JsonForm:
    """把 JSON 对象包装成表单那样的取值接口。

    ``prepare_workflow_create`` 收的是「表单」——一个能按名字取值的对象。
    造这么一个小适配器，就能让**页面表单与管理面 API 共用同一份创建
    逻辑**，而不是把「初始 Agent 校验 + 单节点骨架生成」再写一遍。
    """

    #: JSON 字段名到表单字段名的对照。
    #:
    #: 管理面 API 统一用 camelCase，页面表单是 snake_case。少了这层翻译，
    #: ``agentCode`` 会被当成未知字段丢掉，初始 Agent 校验于是永远失败。
    _ALIASES = {"agent_code": "agentCode"}

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def get(self, name: str, default: Any = "") -> Any:
        """按名字取值；表单字段名找不到就按别名找 camelCase 的 JSON 键。"""
        if name in self._data:
            return self._data[name]
        alias = self._ALIASES.get(name)
        if alias is not None and alias in self._data:
            return self._data[alias]
        return default


def workflow_payload(row: Any, *, include_definition: bool = False) -> dict[str, Any]:
    """把工作流行整理成前端形状。"""
    payload: dict[str, Any] = {
        "code": row.code,
        "name": row.name,
        "description": row.description or "",
        "enabled": bool(row.enabled),
        "version": row.version,
        "nodeCount": len((row.definition or {}).get("nodes") or []),
        "createdAt": row.created_at.isoformat() if row.created_at else None,
        "updatedAt": row.updated_at.isoformat() if row.updated_at else None,
    }
    if include_definition:
        payload["definition"] = row.definition or {}
    return payload


def register(app: Any) -> None:
    """把工作流的 JSON API 挂到 app 上。"""

    @app.get("/admin/api/workflows")
    @api_route
    async def api_workflows_list(request: Request):
        """列出工作流。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=False
        )
        try:
            params = parse_list_params(
                request, allowed_sorts=("name", "code")
            )
            search = search_term(params.q)
            rows = await workflow_repo.list(
                db,
                limit=params.size,
                offset=params.offset,
                search=search or None,
                sort=params.sort,
            )
            total = await workflow_repo.count(db, search=search or None)
            return json_response(
                {
                    "items": [workflow_payload(row) for row in rows],
                    "total": total,
                    "page": {
                        "q": params.q,
                        "sort": params.sort,
                        "page": params.page,
                        "size": params.size,
                    },
                    "options": {
                        "agents": [
                            {"code": row.code, "name": row.name}
                            for row in await agent_repo.list(
                                db,
                                only_enabled=True,
                                limit=500,
                                tenant_id=tenant_of(principal),
                            )
                        ]
                    },
                }
            )
        finally:
            await db.close()

    @app.get("/admin/api/workflows/{code}")
    @api_route
    async def api_workflows_get(code: str, request: Request):
        """取单个工作流（含图定义，供画布 island 使用）。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=False
        )
        try:
            row = await workflow_repo.get_by_code(db, code)
            if row is None:
                raise ApiError("missing", "工作流不存在", status_code=404)
            return json_response(
                {"item": workflow_payload(row, include_definition=True)}
            )
        finally:
            await db.close()

    @app.post("/admin/api/workflows")
    @api_route
    async def api_workflows_create(request: Request):
        """新建工作流：按初始 Agent 生成单节点骨架，图交给画布。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=True
        )
        try:
            body = await read_json_body(request)
            tenant_id = tenant_of(principal)
            payload, error = await prepare_workflow_create(
                db, _JsonForm(body), tenant_id=tenant_id
            )
            if payload is None:
                raise ApiError(
                    "invalid_input",
                    "初始 Agent 不存在或已停用",
                    status_code=400,
                    field_errors={"agentCode": "请选择一个已启用的智能体"},
                )
            if await workflow_repo.get_by_code(db, payload.code):
                raise ApiError("duplicate", "该工作流 code 已存在", status_code=409)
            row = await workflow_repo.create(
                db,
                code=payload.code,
                name=payload.name,
                description=payload.description,
                definition=payload.definition.model_dump(
                    by_alias=True, exclude_none=True
                ),
                enabled=1 if payload.enabled else 0,
            )
            await audit_principal_action(
                db,
                principal,
                action="workflow.create",
                target_type="workflow",
                target_id=row.code,
                tenant_id=principal.tenant_id,
            )
            await db.commit()
            return json_response({"item": workflow_payload(row)}, status_code=201)
        finally:
            await db.close()

    @app.put("/admin/api/workflows/{code}")
    @api_route
    async def api_workflows_update(code: str, request: Request):
        """只更新名称与描述。

        **图定义不在这里改**：改图只能从画布保存，避免出现两条写路径。
        """
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=True
        )
        try:
            body = await read_json_body(request)
            if await workflow_repo.get_by_code(db, code) is None:
                raise ApiError("missing", "工作流不存在", status_code=404)
            fields: dict[str, Any] = {}
            if "name" in body:
                fields["name"] = str(body.get("name") or "")
            if "description" in body:
                fields["description"] = str(body.get("description") or "")
            if "enabled" in body:
                fields["enabled"] = 1 if body.get("enabled") else 0
            row = await workflow_repo.update(db, code, fields)
            if row is None:
                raise ApiError("missing", "工作流不存在", status_code=404)
            await audit_principal_action(
                db,
                principal,
                action="workflow.update",
                target_type="workflow",
                target_id=code,
                tenant_id=principal.tenant_id,
            )
            await db.commit()
            return json_response({"item": workflow_payload(row)})
        finally:
            await db.close()

    @app.post("/admin/api/workflows/{code}/toggle")
    @api_route
    async def api_workflows_toggle(code: str, request: Request):
        """切换工作流的启用状态。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=True
        )
        try:
            body = await read_json_body(request)
            row = await workflow_repo.update(
                db, code, {"enabled": 1 if body.get("enabled") else 0}
            )
            if row is None:
                raise ApiError("missing", "工作流不存在", status_code=404)
            await audit_principal_action(
                db,
                principal,
                action="workflow.toggle",
                target_type="workflow",
                target_id=code,
                tenant_id=principal.tenant_id,
                detail={"enabled": bool(row.enabled)},
            )
            await db.commit()
            return json_response({"item": workflow_payload(row)})
        finally:
            await db.close()

    @app.delete("/admin/api/workflows/{code}")
    @api_route
    async def api_workflows_delete(code: str, request: Request):
        """删除工作流。"""
        db, principal, _csrf = await authorize(
            request, page_code=PAGE_CODE, component_code=COMPONENT_CODE, write=True
        )
        try:
            existing = await workflow_repo.get_by_code(db, code)
            if existing is None:
                raise ApiError("missing", "工作流不存在", status_code=404)
            entry = await record_deletion(
                db,
                resource_type="workflow",
                row=existing,
                actor_id=principal.principal_id,
                actor_username=principal.username,
            )
            if not await workflow_repo.delete(db, code):
                raise ApiError("missing", "工作流不存在", status_code=404)
            await audit_principal_action(
                db,
                principal,
                action="workflow.delete",
                target_type="workflow",
                target_id=code,
                tenant_id=principal.tenant_id,
            )
            await db.commit()
            return json_response({"ok": True, "undoId": entry.id})
        finally:
            await db.close()