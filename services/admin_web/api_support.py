"""管理面 JSON API 的公共支撑（V4.10）。

SvelteKit 前端通过这里定义的四个动作访问管理端：**认证、CSRF、权限**和
**错误形状**。它们与页面路由共用同一套判定函数——差异只有两点：

1. CSRF 令牌从 ``X-CSRF-Token`` 头取，而不是表单字段；
2. 失败时返回 JSON，而不是 HTML 错误页或 303 跳转。

**为什么不在前端做权限判断**：``can_access_page`` / ``can_show_component``
是 Casbin 的唯一入口。前端拿到的「可见页面/组件」只用于决定**渲染什么**，
能不能**写**永远由这里判定。两处判定迟早会漂移，而漂移方向通常是放行。
"""

from __future__ import annotations

import json
from functools import wraps
from typing import Any

from ludic.web import Request
from starlette.responses import JSONResponse

from app.core.admin_auth import request_csrf_cookie
from services.admin_web.accounts import verify_session_csrf
from services.admin_web.common import (
    can_edit_resource,
    origin_ok,
    tenant_id_of,
    with_auth,
)
from services.admin_web.security import csrf_matches


class ApiError(Exception):
    """管理面 API 的业务失败。

    带 HTTP 状态码与稳定的机器码：前端据此决定是提示、重试还是跳登录页，
    不去解析错误文案——文案会改，机器码不会。
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        field_errors: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.field_errors = field_errors or {}

    def response(self) -> JSONResponse:
        """把错误渲染成 JSON 响应。"""
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.field_errors:
            payload["fieldErrors"] = self.field_errors
        return json_response(payload, status_code=self.status_code)


def api_route(func):
    """把 :class:ApiError 翻译成 JSON 响应。

    每个 JSON 端点都套一层：这样错误处理只有**一处**，不会因为某个端点
    忘了 catch 就漏出 500 或 HTML 错误页。装饰器用 `functools.wraps`
    保留原签名——Starlette 靠签名识别路径参数。
    """

    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except ApiError as exc:
            return exc.response()

    return wrapper


def json_response(payload: dict[str, Any], *, status_code: int = 200) -> JSONResponse:
    """返回禁止中间层缓存的 JSON 响应。"""
    response = JSONResponse(payload, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    return response


async def read_json_body(request: Request) -> dict[str, Any]:
    """读取请求体，要求是一个 JSON 对象。"""
    raw = await request.body()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiError(
            "invalid_json",
            f"请求体不是合法 JSON：第 {exc.lineno} 行第 {exc.colno} 列",
            status_code=400,
        ) from exc
    if not isinstance(parsed, dict):
        raise ApiError("invalid_json", "请求体必须是 JSON 对象", status_code=400)
    return parsed


async def authorize(
    request: Request,
    *,
    page_code: str,
    component_code: str,
    write: bool,
) -> tuple[Any, Any, str]:
    """完成认证、权限与 CSRF 判定。

    返回 ``(db, principal, csrf_token)``。任何一步失败都抛 :class:`ApiError`，
    由调用点统一渲染。

    **写操作必须带 CSRF 令牌**：令牌从 ``X-CSRF-Token`` 头取，浏览器全程
    不参与——服务端持有会话 Cookie 与 CSRF 令牌，写请求由服务端代发。
    """
    values = await with_auth(request)
    if values is None:
        raise ApiError("unauthorized", "请先登录管理后台", status_code=401)
    db, principal, _pref, csrf = values
    try:
        if not origin_ok(request):
            raise ApiError("invalid_origin", "请求来源无效", status_code=403)
        if write:
            # ``page_code`` 为空表示「权限要在知道资源类型之后才能判定」
            # （撤销删除就是这样：先看快照是什么资源，再决定按哪张表的
            # 写权限放行）。CSRF 仍然要校验——它与权限无关。
            if page_code and not await can_edit_resource(
                db, principal, page_code=page_code, component_code=component_code
            ):
                raise ApiError("not_found", "页面不可用", status_code=404)
            if not await verify_api_csrf(db, request, principal):
                raise ApiError("invalid_csrf", "CSRF 校验失败", status_code=403)
        return db, principal, csrf
    except Exception:
        await db.close()
        raise


async def verify_api_csrf(db, request: Request, principal) -> bool:
    """校验 ``X-CSRF-Token`` 头里的令牌。

    判定逻辑与页面路由的 :func:`require_csrf` 完全一致：Cookie、表单/头、
    数据库 Session 三方一致才算通过。
    """
    token = request.headers.get("x-csrf-token") or ""
    if not csrf_matches(
        request_csrf_cookie(request, super_admin=principal.is_platform_super_admin),
        token,
    ):
        return False
    return await verify_session_csrf(db, principal, token)


def tenant_of(principal) -> str | None:
    """取主体所属租户。"""
    return tenant_id_of(principal)
