"""LLM 端点的写操作（更新 / 探测 / 启停 / 删除）。

从 ``llm.py`` 拆出来：单文件行数上限是 350 行。拆分只切代码位置，
路由的路径、方法与注册顺序都没变。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from ludic.web import Request
from pydantic import ValidationError
from starlette.responses import RedirectResponse

from app.core.crypto import decrypt, encrypt
from app.repo.agent_repo import agent_repo
from app.repo.llm_endpoint_repo import llm_endpoint_repo
from services.admin_web.audit import audit_principal_action
from services.admin_web.common import (
    as_bool, business_redirect, flash_from_query, json_response_no_store,
    not_found, operation_authorized, unauthorized, with_auth,
)
from services.admin_web.deletion import record_deletion
from services.admin_web.form_errors import _llm_field_errors
from services.admin_web.form_state import invalid_form_redirect
# ``fetch_models`` / ``probe_chat`` 是**测试的替换点**：用例在这里打桩，
# 避免真的去连外部服务。处理器必须和这些名字待在同一个模块里，
# 打桩才有效。
from services.admin_web.llm_probe import LlmProbeError, fetch_models, probe_chat


def register(app: Any) -> None:
    """挂载写操作路由。"""

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
            submitted = {
                "name": str(form.get("name") or code),
                "base_url": str(form.get("base_url") or ""),
                "model": str(form.get("model") or ""),
                "priority": str(form.get("priority") or "100"),
            }
            try:
                values = {
                    "name": submitted["name"],
                    "base_url": submitted["base_url"],
                    "model": submitted["model"],
                    "priority": int(submitted["priority"]),
                }
                if api_key:
                    values["api_key"] = api_key
                payload = LlmEndpointUpdate(
                    **values,
                )
            except (TypeError, ValueError, ValidationError) as exc:
                # 校验失败：保留已填内容并逐字段标错（UX-03）。
                return RedirectResponse(
                    invalid_form_redirect(
                        f"/admin/llm?resource={quote(code, safe='')}",
                        submitted,
                        _llm_field_errors(exc),
                    ),
                    status_code=303,
                )
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
            return json_response_no_store(
                {"ok": False, "error": "unauthorized"}, 401
            )
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
                return json_response_no_store(
                    {"ok": False, "error": "forbidden"}, 403
                )
            mode = "test" if str(form.get("mode") or "") == "test" else "models"
            code = str(form.get("code") or "").strip()
            base_url = str(form.get("base_url") or "").strip()
            model = str(form.get("model") or "").strip()
            api_key = str(form.get("api_key") or "")
            if code:
                row = await llm_endpoint_repo.get_by_code(db, code)
                if row is None:
                    return json_response_no_store(
                        {"ok": False, "error": "endpoint not found"}, 404
                    )
                base_url = base_url or row.base_url
                model = model or row.model
                # 编辑表单里的 API Key 是只写的：留空表示沿用库里已存密钥。
                api_key = api_key or decrypt(row.api_key_cipher)
            if not base_url:
                return json_response_no_store(
                    {"ok": False, "error": "base_url is required"}, 400
                )
            target_id = (code or base_url)[:128]
            models: list[str] = []
            try:
                if mode == "test":
                    # 「测试连接」问的是「这个地址+密钥+模型能不能对话」，
                    # 所以**先**探对话端点。顺序反过来会误判：不少
                    # OpenAI 兼容网关没有开放 ``GET /models``（403/404），
                    # 但 ``POST /chat/completions`` 一切正常；先拉列表会把
                    # 这种可用配置判成「连接失败」。
                    try:
                        result = await probe_chat(base_url, api_key, model)
                    except LlmProbeError as exc:
                        # 402/429 是「额度/限流」，不是「配置写错」。要分清
                        # 这两者，得知道地址与密钥**本身**是好的——用模型
                        # 列表再验一次：拿到了就说明连接没问题，只是对话
                        # 这一环被额度挡住了。
                        if exc.status_code in {402, 429}:
                            try:
                                models = await fetch_models(
                                    base_url, api_key
                                )
                            except LlmProbeError:
                                models = []
                        raise
                    # 模型列表是锦上添花：拿不到不影响「连接可用」的结论。
                    try:
                        models = await fetch_models(base_url, api_key)
                    except LlmProbeError:
                        models = []
                else:
                    models = await fetch_models(base_url, api_key)
                    result = None
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
                payload = {"ok": False, "error": str(exc), "models": models}
                # 402/429：模型列表已经拿到（地址与密钥可用），只是账号额度/
                # 限流把对话这一环拦下了。单独标出来，免得把「额度不够」
                # 误报成「配置写错」。
                if mode == "test" and exc.status_code in {402, 429}:
                    payload["limited"] = True
                return json_response_no_store(payload, 400)
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
            return json_response_no_store(payload)
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
            existing = await llm_endpoint_repo.get_by_code(db, code)
            if existing is None:
                return business_redirect("/admin/llm", error="missing")
            next_enabled = as_bool(form.get("enabled"))
            if (
                existing.enabled
                and not next_enabled
                and await agent_repo.count_for_llm_endpoint(db, code)
            ):
                return business_redirect("/admin/llm", error="in_use")
            row = await llm_endpoint_repo.update(
                db,
                code,
                {"enabled": 1 if next_enabled else 0},
            )
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
            existing = await llm_endpoint_repo.get_by_code(db, code)
            if existing is None:
                return business_redirect("/admin/llm", error="missing")
            if await agent_repo.count_for_llm_endpoint(db, code):
                return business_redirect("/admin/llm", error="in_use")
            # 删除前留快照：误删能在撤销窗口内原样恢复（UX-01）。
            entry = await record_deletion(
                db,
                resource_type="llm_endpoint",
                row=existing,
                actor_id=principal.principal_id,
                actor_username=principal.username,
            )
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
            return RedirectResponse(
                f"/admin/llm?notice=deleted&undo={entry.id}",
                status_code=303,
            )
        finally:
            await db.close()
