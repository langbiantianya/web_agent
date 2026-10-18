"""SSE ticket 签发与校验（设计文档 §5.1）。

EventSource 无法自定义请求头，只能把凭证放在 query 上，因此使用**短期 ticket**
而不是长期 token：ticket 有效期默认 60 秒，仅用于建立一条 SSE 连接。

实现：Fernet 对 ``{"run_id":..., "uid":..., "exp":...}`` 做认证加密，天然防篡改
+ 防伪造 + 自带过期校验，不引入额外依赖。
"""

from __future__ import annotations

import json
import time

from app.config.settings import settings
from app.core.crypto import _fernet
from app.core.errors import UnauthorizedError


def issue_ticket(
    user_id: str = "anonymous", run_id: int | None = None, ttl: int | None = None
) -> tuple[str, int]:
    """签发一次性 SSE 凭证。

    EventSource 不能自定义请求头，只能把凭证塞 query，而 query 会进浏览器历史、
    nginx 访问日志和 Referer，所以这里发**短期** ticket 而非长期 token。

    Args:
        user_id: 会话用户 id，校验时回传给调用方做归属判断。
        run_id: 可选。带上即把 ticket 与该 run 绑定，只能用于这一条流。
        ttl: 有效期秒数，默认取 ``settings.sse_ticket_ttl``。

    Returns:
        ``(token, ttl)``。把 ttl 一并返回，是为了让前端按真实值安排「先换票再
        重连」，而不是硬编码一个可能已经过期的等待时长。
    """
    ttl = ttl if ttl is not None else settings.sse_ticket_ttl
    payload = {"uid": user_id, "exp": int(time.time()) + ttl}
    if run_id is not None:
        payload["run_id"] = run_id
    token = _fernet().encrypt(json.dumps(payload).encode("utf-8")).decode("ascii")
    return token, ttl


def verify_ticket(ticket: str, run_id: int | None = None) -> dict:
    """校验 ticket；可绑定 run_id，绑定了就只能访问那一个 run。"""
    if not ticket:
        raise UnauthorizedError("缺少 ticket")
    try:
        raw = _fernet().decrypt(ticket.encode("ascii")).decode("utf-8")
        payload = json.loads(raw)
    except Exception as exc:
        raise UnauthorizedError("ticket 无效") from exc

    if payload.get("exp", 0) < int(time.time()):
        raise UnauthorizedError("ticket 已过期，请重新获取")

    if run_id is not None and payload.get("run_id") not in (None, run_id):
        raise UnauthorizedError("ticket 与该 run 不匹配")
    return payload
