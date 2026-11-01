"""请求体大小限制（纯 ASGI 中间件）。

**为什么要有这道限制**：请求体没有上限时，一条 ``POST /api/agents
{"system_prompt": "A"×5MB}`` 会被完整受理——进内存、落库、送 LLM。不限流的
情况下，这就是一条放大内存与 token 成本的通道：成本不在这一次请求上，而在它
后面那一次模型调用上。

**为什么是纯 ASGI 而不是 BaseHTTPMiddleware**：``BaseHTTPMiddleware`` 会把整个
请求体读进内存再往下传，对「限制请求体」这件事来说正好是反的；纯 ASGI 版本
可以在自己这一层就把大小卡住。
"""

from __future__ import annotations

import logging

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

# 会带请求体的方法。GET / HEAD / DELETE / OPTIONS 一般没有体，直接放行。
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})


class BodySizeLimitMiddleware:
    """把请求体限制在 ``max_bytes`` 以内，超了直接 413。

    实现是**先缓冲再转发**：本服务只有 JSON 接口，没有流式上传，把体读进内存
    再交给下游，既能精确卡住大小，也能给出一个干净的 413——而不是让下游拿到
    一段被截断的 body、报出一个莫名其妙的 422。

    内存占用因此被 ``max_bytes`` 兜住：超限时立刻停读并拒绝，不再累积。
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        """绑定应用与上限。

        Args:
            app: 下一个 ASGI 应用。
            max_bytes: 允许的最大请求体字节数。小于等于 0 表示不限制。
        """
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """ASGI 入口。"""
        if scope["type"] != "http" or scope.get("method") not in _BODY_METHODS:
            await self.app(scope, receive, send)
            return
        if self.max_bytes <= 0:
            await self.app(scope, receive, send)
            return

        body = b""
        too_large = False
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                break
            body += message.get("body", b"")
            if len(body) > self.max_bytes:
                too_large = True
                break
            if not message.get("more_body", False):
                break

        if too_large:
            logger.warning(
                "请求体超过上限（%s 字节），已拒绝：%s %s",
                self.max_bytes,
                scope.get("method"),
                scope.get("path"),
            )
            response = JSONResponse(
                status_code=413,
                content={
                    "code": "payload_too_large",
                    "message": f"请求体过大（上限 {self.max_bytes} 字节）",
                },
            )
            await response(scope, receive, send)
            return

        # 把缓冲下来的 body 重新喂给下游
        replayed = False

        async def _replay() -> Message:
            """只回一次 body，之后告诉下游客户端已断开。"""
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        await self.app(scope, _replay, send)


__all__ = ["BodySizeLimitMiddleware"]
