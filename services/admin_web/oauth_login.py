"""管理端 OAuth 登录的限频与失败记账（V4.10 加固）。

这些逻辑与客户端应用（``app/api/routes/auth.py``）**同源**——判定都在
``app.core.oauth_login_guard`` 里，这里只做两件事：

1. 把「管理端请求」翻译成限频需要的入参（IP 哈希）；
2. 把「一次登录失败」翻译成管理端要的响应（跳回登录页并带稳定错误码）。

之所以要单独抽一个模块而不是就地写在路由里：``routes/auth.py`` 已经贴着
单文件行数上限（350 行），而这些逻辑是**跨两条路由**（start 与 callback）
共用的。
"""

from __future__ import annotations

import logging

from starlette.responses import RedirectResponse

from app.core.errors import TooManyRequestsError
from app.core.oauth_login_guard import (
    check_callback_failure_limit,
    check_start_rate_limit,
    client_ip,
    ip_hash,
    record_failure,
)
from services.admin_web.common import login_redirect

logger = logging.getLogger(__name__)


def request_ip_hash(request) -> str:
    """取这次请求的限频分组键（IP 哈希，不是明文 IP）。"""
    return ip_hash(client_ip(request))


async def guard_start(db, provider, ip: str) -> RedirectResponse | None:
    """start 闸。超限返回跳转响应，没超限返回 ``None``。

    放在解析 provider 端点**之前**——限频是为了挡住「反复打 IdP 的 discovery /
    token 端点」，先解析等于先挨打。
    """
    try:
        await check_start_rate_limit(db, provider.id, ip)
    except TooManyRequestsError:
        return login_redirect("rate_limited")
    return None


async def fail_login(
    db,
    request,
    provider,
    attempt,
    error_code: str,
) -> RedirectResponse:
    """记一次登录失败，必要时按失败次数限频，返回要跳回登录页的响应。

    **记账是 best-effort**：记账失败只会让限频少算一次，绝不能把一次
    「登录失败」变成「服务器错误」——浏览器只该看到稳定错误码。

    **记不上的失败不计数**：``state_invalid`` 这类失败压根没有对应的事务行
    （伪造的 state），它的防线是 start 闸（伪造 state 也得先拉起一笔真事务）。
    """
    ip = request_ip_hash(request)
    if attempt is not None:
        try:
            await record_failure(db, attempt, error_code, ip=ip)
        except Exception:  # noqa: BLE001
            logger.exception("记录管理端 OAuth 登录失败明细失败")
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                logger.exception("回滚登录失败记账的会话失败")
    try:
        await check_callback_failure_limit(db, provider.id, ip)
    except TooManyRequestsError:
        return login_redirect("rate_limited")
    return login_redirect(error_code)