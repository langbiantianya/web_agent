"""LLM 接口配置管理的路由。"""

from __future__ import annotations

from typing import Any

from ludic.web import Request
from pydantic import ValidationError
from starlette.responses import JSONResponse

from app.core.crypto import decrypt, encrypt
from app.repo.llm_endpoint_repo import llm_endpoint_repo
from app.schemas.llm_endpoint import LlmEndpointCreate, LlmEndpointUpdate
from services.admin_web.audit import audit_principal_action
from services.admin_web.common import (
    as_bool, business_redirect, can_edit_resource, flash_from_query, not_found,
    operation_authorized, unauthorized, with_auth,
)
from services.admin_web.llm_probe import LlmProbeError, fetch_models, probe_chat
from services.admin_web.registry import can_access_page, visible_pages
from services.admin_web.views import llm_page


def _probe_response(payload: dict[str, Any], status_code: int = 200) -> JSONResponse:
    """返回探测结果 JSON，并禁止任何中间层缓存它。"""
    response = JSONResponse(payload, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    return response



def register(app: Any) -> None:
    """把 LLM 接口配置管理的路由挂到 app 上。"""

    @app.get("/admin/llm")
    async def llm_get(request: Request):
        """渲染 LLM 接口优先级配置页。"""
        values = await with_auth(request)
        if values is None:
            return unauthorized()
        db, principal, pref, csrf = values
        try:
            if not await can_access_page(db, principal, "llm"):
                return not_found()
            flash, _ = flash_from_query(request)
            return llm_page(
                principal=principal,
                csrf=csrf,
                pages=await visible_pages(db, principal),
                rows=await llm_endpoint_repo.list(db, limit=500),
                theme=pref.theme,
                color_mode=pref.color_mode,
                density=pref.density,
                can_write=await can_edit_resource(
                    db,
                    principal,
                    page_code="llm",
                    component_code="llm_editor",
                ),
                flash=flash,
            )
        finally:
            await db.close()

    @app.post("/admin/llm")
    async def llm_post(request: Request):
        """新增一个 OpenAI 兼容接口配置。"""
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
                page_code="llm",
                component_code="llm_editor",
                form=form,
            ):
                return not_found()
            try:
                payload = LlmEndpointCreate(
                    code=str(form.get("code") or ""),
                    name=str(form.get("name") or ""),
                    base_url=str(form.get("base_url") or ""),
                    model=str(form.get("model") or ""),
                    api_key=str(form.get("api_key") or ""),
                    priority=int(str(form.get("priority") or "100")),
                )
            except (TypeError, ValueError, ValidationError):
                return business_redirect("/admin/llm", error="invalid")
            if await llm_endpoint_repo.get_by_code(db, payload.code):
                return business_redirect("/admin/llm", error="duplicate")
            row = await llm_endpoint_repo.create(
                db,
                code=payload.code,
                name=payload.name or payload.code,
                base_url=payload.base_url,
                model=payload.model,
                api_key_cipher=encrypt(payload.api_key),
                priority=payload.priority,
                enabled=1,
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
            return business_redirect("/admin/llm", notice="created")
        finally:
            await db.close()

    @app.post("/admin/llm/{code}/update")
    async def llm_update(code: str, request: Request):
        """更新 LLM 接口；空 API Key 表示保留旧密钥。"""
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
                page_code="llm",
                component_code="llm_editor",
                form=form,
            ):
                return not_found()
            if await llm_endpoint_repo.get_by_code(db, code) is None:
                return business_redirect("/admin/llm", error="missing")
            api_key = str(form.get("api_key") or "")
            try:
                values = {
                    "name": str(form.get("name") or code),
                    "base_url": str(form.get("base_url") or ""),
                    "model": str(form.get("model") or ""),
                    "priority": int(str(form.get("priority") or "100")),
                }
                if api_key:
                    values["api_key"] = api_key
                payload = LlmEndpointUpdate(
                    **values,
                )
            except (TypeError, ValueError, ValidationError):
                return business_redirect("/admin/llm", error="invalid")
            fields = payload.model_dump(exclude_unset=True)
            if "api_key" in fields:
                fields["api_key_cipher"] = encrypt(fields.pop("api_key"))
            await llm_endpoint_repo.update(db, code, fields)
            await audit_principal_action(
                db,
                principal,
                action="llm.update",
                target_type="llm_endpoint",
                target_id=code,
                tenant_id=principal.tenant_id,
            )
            await db.commit()
            return business_redirect("/admin/llm", notice="updated")
        finally:
            await db.close()

    @app.post("/admin/llm/probe")
    async def llm_probe(request: Request):
        """拉取模型列表，或用最小对话验证端点配置是否可用。

        ``mode=models`` 只列模型（填充页面的下拉框）；``mode=test`` 先列模型、
        再对选中模型发一次 ``max_tokens=16`` 的对话。两种模式都会真的用
        ``base_url + api_key`` 出网，所以要求与写端点相同的组件权限，并审计
        成功/失败结果（审计里不放 Key，也不放上游正文）。
        """
        values = await with_auth(request)
        if values is None:
            return _probe_response({"ok": False, "error": "unauthorized"}, 401)
        db, principal, _, _ = values
        try:
            form = await request.form()
            if not await operation_authorized(
                request,
                db,
                principal,
                page_code="llm",
                component_code="llm_editor",
                form=form,
            ):
                return _probe_response({"ok": False, "error": "forbidden"}, 403)
            mode = "test" if str(form.get("mode") or "") == "test" else "models"
            code = str(form.get("code") or "").strip()
            base_url = str(form.get("base_url") or "").strip()
            model = str(form.get("model") or "").strip()
            api_key = str(form.get("api_key") or "")
            if code:
                row = await llm_endpoint_repo.get_by_code(db, code)
                if row is None:
                    return _probe_response(
                        {"ok": False, "error": "endpoint not found"}, 404
                    )
                base_url = base_url or row.base_url
                model = model or row.model
                # 编辑表单里的 API Key 是只写的：留空表示沿用库里已存密钥。
                api_key = api_key or decrypt(row.api_key_cipher)
            if not base_url:
                return _probe_response(
                    {"ok": False, "error": "base_url is required"}, 400
                )
            target_id = (code or base_url)[:128]
            models: list[str] = []
            try:
                models = await fetch_models(base_url, api_key)
                result = (
                    await probe_chat(base_url, api_key, model)
                    if mode == "test"
                    else None
                )
            except LlmProbeError as exc:
                await audit_principal_action(
                    db,
                    principal,
                    action="llm.probe",
                    target_type="llm_endpoint",
                    target_id=target_id,
                    tenant_id=principal.tenant_id,
                    outcome="denied",
                    detail={"mode": mode, "ok": False, "reason": str(exc)[:120]},
                )
                await db.commit()
                payload: dict[str, Any] = {
                    "ok": False,
                    "error": str(exc),
                    "models": models,
                }
                # 402/429：模型列表已经拿到（地址与密钥可用），只是账号额度/
                # 限流把对话这一环拦下了。单独标出来，免得把「额度不够」
                # 误报成「配置写错」。
                if mode == "test" and exc.status_code in {402, 429}:
                    payload["limited"] = True
                return _probe_response(payload, 400)
            await audit_principal_action(
                db,
                principal,
                action="llm.probe",
                target_type="llm_endpoint",
                target_id=target_id,
                tenant_id=principal.tenant_id,
                detail={
                    "mode": mode,
                    "ok": True,
                    "model": result.model if result else "",
                    "latency_ms": result.latency_ms if result else 0,
                },
            )
            await db.commit()
            payload = {"ok": True, "mode": mode, "models": models}
            if result is not None:
                payload.update(
                    model=result.model,
                    latency_ms=result.latency_ms,
                    reply=result.reply,
                )
            return _probe_response(payload)
        finally:
            await db.close()

    @app.post("/admin/llm/{code}/toggle")
    async def llm_toggle(code: str, request: Request):
        """切换 LLM 接口启用状态。"""
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
                page_code="llm",
                component_code="llm_editor",
                form=form,
            ):
                return not_found()
            row = await llm_endpoint_repo.update(
                db,
                code,
                {"enabled": 1 if as_bool(form.get("enabled")) else 0},
            )
            if row is None:
                return business_redirect("/admin/llm", error="missing")
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
            return business_redirect("/admin/llm", notice="toggled")
        finally:
            await db.close()

    @app.post("/admin/llm/{code}/delete")
    async def llm_delete(code: str, request: Request):
        """删除 LLM 接口配置。"""
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
                page_code="llm",
                component_code="llm_editor",
                form=form,
            ):
                return not_found()
            if not await llm_endpoint_repo.delete(db, code):
                return business_redirect("/admin/llm", error="missing")
            await audit_principal_action(
                db,
                principal,
                action="llm.delete",
                target_type="llm_endpoint",
                target_id=code,
                tenant_id=principal.tenant_id,
            )
            await db.commit()
            return business_redirect("/admin/llm", notice="deleted")
        finally:
            await db.close()
