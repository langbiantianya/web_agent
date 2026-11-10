"""工作流的写操作路由：校验、创建、更新、启停与删除。"""

from __future__ import annotations

import json
from typing import Any

from ludic.web import Request
from starlette.responses import JSONResponse, RedirectResponse

from app.repo.workflow_repo import workflow_repo
from services.admin_web.audit import audit_principal_action
from services.admin_web.deletion import record_deletion
from services.admin_web.common import (
    business_redirect, can_edit_resource, json_response_no_store, not_found,
    operation_authorized, origin_ok, require_csrf, safe_next, tenant_id_of,
    unauthorized, with_auth,
)
from services.admin_web.workflow_save import (
    prepare_workflow_create,
    prepare_workflow_update,
)
from services.admin_web.workflow_validation import _definition_errors


def register(app: Any) -> None:
    """把工作流的写操作路由挂到 app 上。"""

    @app.post("/admin/workflows/validate")
    async def workflows_validate(request: Request):
        """校验一份 Workflow definition，不写 Workflow 数据。

        画布用它做实时反馈。规则实现在 ``app.core.workflow_def``，与保存时
        走的是同一份代码——前端不自己判断「这个图合不合法」。

        只读指的是 **Workflow 数据**：登录态与主题偏好的读取沿用既有机制，
        管理员尚无偏好行时后台仍会补建一行（与其它后台页面一致）。
        """
        values = await with_auth(request)
        if values is None:
            # 本端点服务于画布的 fetch()，必须回 JSON：表单端点那种 303 跳登录页
            # 会被 fetch 跟随后拿到登录页 HTML，前端连 JSON 都解析不出来，
            # 更分不清「登录过期」与「校验失败」。
            return JSONResponse(
                {"ok": False, "errors": [{"message": "forbidden"}]},
                status_code=403,
            )
        db, principal, _pref, _csrf = values
        try:
            try:
                body = await request.json()
            except (json.JSONDecodeError, ValueError):
                return JSONResponse(
                    {"ok": False, "errors": [{"message": "invalid_json"}]},
                    status_code=400,
                )
            if not isinstance(body, dict):
                return JSONResponse(
                    {"ok": False, "errors": [{"message": "invalid_json"}]},
                    status_code=400,
                )
            definition = body.get("definition")
            if not isinstance(definition, dict):
                return JSONResponse(
                    {"ok": False, "errors": [{"message": "definition_required"}]},
                    status_code=400,
                )
            # 形状门是**纵深防御**：collect_graph_issues 自身已能处理畸形负载，
            # 并把畸形部分报成 issue（见 app/core/workflow_def.py 的
            # _sanitize_payload）。这里仍然先挡一道，让形状不对的负载拿到
            # 400 而不是 200 + issues——前端据此区分「负载没送对」与「图不合法」。
            nodes = definition.get("nodes")
            edges = definition.get("edges", [])
            shape_ok = (
                isinstance(nodes, list)
                and isinstance(edges, list)
                and all(isinstance(item, dict) for item in nodes)
                and all(isinstance(item, dict) for item in edges)
            )
            if not shape_ok:
                return JSONResponse(
                    {"ok": False, "errors": [{"message": "definition_shape_invalid"}]},
                    status_code=400,
                )
            authorized = (
                origin_ok(request)
                and await can_edit_resource(
                    db,
                    principal,
                    page_code="workflows",
                    component_code="workflow_editor",
                )
                and await require_csrf(
                    db, request, principal, {"csrf_token": body.get("csrf_token")}
                )
            )
            if not authorized:
                return JSONResponse(
                    {"ok": False, "errors": [{"message": "forbidden"}]},
                    status_code=403,
                )
            locale = "en" if body.get("locale") == "en" else "zh-CN"
            errors = _definition_errors(definition, locale=locale)
            if not errors:
                return JSONResponse({"ok": True})
            return JSONResponse({"ok": False, "errors": errors})
        finally:
            await db.close()

    @app.post("/admin/workflows")
    async def workflows_post(request: Request):
        """通过共享 Workflow 仓储创建编排。

        新表单只收一个「初始 Agent code」，服务端据此生成单节点骨架 definition，
        剩下的图交给编排页画。老的 ``definition`` JSON 表单字段仍然接受，
        避免既有调用方（含既有测试）突然失效。
        """
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
                page_code="workflows",
                component_code="workflow_editor",
                form=form,
            ):
                return not_found()
            payload, error = await prepare_workflow_create(
                db, form, tenant_id=tenant_id_of(principal)
            )
            if payload is None:
                return business_redirect("/admin/workflows", error=error)
            if await workflow_repo.get_by_code(db, payload.code):
                return business_redirect("/admin/workflows", error="duplicate")
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
            # 建完之后直接进编排页，并带上 notice 让页面给出可见反馈。
            return business_redirect(f"/admin/workflows/{row.code}", notice="created")
        finally:
            await db.close()

    @app.post("/admin/workflows/{code}/update")
    async def workflows_update(code: str, request: Request):
        """更新 Workflow 名称、启用状态或图定义。

        表单可以带一个可选的 ``next`` 指回保存后的落点（编排页会带上自己），
        只接受 ``/admin/`` 内部路径，避免开放重定向。
        """
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
                page_code="workflows",
                component_code="workflow_editor",
                form=form,
            ):
                return not_found()
            if await workflow_repo.get_by_code(db, code) is None:
                return business_redirect("/admin/workflows", error="missing")
            locale = "en" if str(form.get("locale") or "") == "en" else "zh-CN"
            # 画布上的提交是 fetch 发出的（Accept: application/json）：
            # 校验不过时**留在画布上**并把逐条错误回给前端，而不是 303 跳回
            # 列表页——跳回去等于把管理员还没保存的图直接丢掉（UX-03）。
            wants_json = "application/json" in (
                request.headers.get("accept") or ""
            )
            payload, errors = await prepare_workflow_update(
                db,
                form,
                tenant_id=tenant_id_of(principal),
                locale=locale,
            )
            if payload is None:
                if wants_json:
                    return json_response_no_store(
                        {"ok": False, "errors": errors}, 400
                    )
                return business_redirect(
                    f"/admin/workflows/{code}", error="invalid"
                )
            fields = payload.model_dump(exclude_unset=True)
            fields["definition"] = payload.definition.model_dump(
                by_alias=True, exclude_none=True
            )
            fields["enabled"] = 1 if payload.enabled else 0
            await workflow_repo.update(db, code, fields)
            await audit_principal_action(
                db,
                principal,
                action="workflow.update",
                target_type="workflow",
                target_id=code,
                tenant_id=principal.tenant_id,
            )
            await db.commit()
            if wants_json:
                return json_response_no_store({"ok": True, "code": code})
            next_url = safe_next(str(form.get("next") or "/admin/workflows"))
            # 编排页与列表页都要能看到「已保存」的反馈。
            separator = "&" if "?" in next_url else "?"
            return RedirectResponse(f"{next_url}{separator}notice=updated", status_code=303)
        finally:
            await db.close()

    @app.post("/admin/workflows/{code}/toggle")
    async def workflows_toggle(code: str, request: Request):
        """切换 Workflow 启用状态。"""
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
                page_code="workflows",
                component_code="workflow_editor",
                form=form,
            ):
                return not_found()
            row = await workflow_repo.update(
                db,
                code,
                {"enabled": 1 if as_bool(form.get("enabled")) else 0},
            )
            if row is None:
                return business_redirect("/admin/workflows", error="missing")
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
            return business_redirect("/admin/workflows", notice="toggled")
        finally:
            await db.close()

    @app.post("/admin/workflows/{code}/delete")
    async def workflows_delete(code: str, request: Request):
        """永久删除 Workflow。"""
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
                page_code="workflows",
                component_code="workflow_editor",
                form=form,
            ):
                return not_found()
            existing = await workflow_repo.get_by_code(db, code)
            if existing is None:
                return business_redirect("/admin/workflows", error="missing")
            # 删除前留快照：误删能在撤销窗口内原样恢复（UX-01）。
            entry = await record_deletion(
                db,
                resource_type="workflow",
                row=existing,
                actor_id=principal.principal_id,
                actor_username=principal.username,
            )
            if not await workflow_repo.delete(db, code):
                return business_redirect("/admin/workflows", error="missing")
            await audit_principal_action(
                db,
                principal,
                action="workflow.delete",
                target_type="workflow",
                target_id=code,
                tenant_id=principal.tenant_id,
            )
            await db.commit()
            return RedirectResponse(
                f"/admin/workflows?notice=deleted&undo={entry.id}",
                status_code=303,
            )
        finally:
            await db.close()
