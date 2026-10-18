"""假的 OpenAI 兼容端点，用于在无真实 API Key 的情况下跑通全链路。

用 uvicorn 起一个真实的 HTTP 服务（而不是 MockTransport），这样 AsyncOpenAI 的
真实网络栈、流式解析、心跳、超时等行为都会被覆盖到。
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from dataclasses import dataclass, field

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse


def _free_port() -> int:
    """向 OS 借一个空闲端口：固定端口会撞上残留进程或并行用例。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class FakeLLM:
    """可编程的假 LLM。

    ``texts`` 按调用序依次返回；``chunk_delay`` 控制吐字速度，方便在流中途
    触发「客户端断开 / 重连续传」这类时序测试。
    """

    texts: list[str] = field(default_factory=lambda: ["你好", "，", "世界"])
    chunk_delay: float = 0.0
    include_reasoning: str = ""
    port: int = field(default_factory=_free_port)
    calls: list[dict] = field(default_factory=list)
    _server: uvicorn.Server | None = None
    _thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        """给 AsyncOpenAI 的 base_url（带 ``/v1`` 后缀，对齐 OpenAI 兼容协议）。"""
        return f"http://127.0.0.1:{self.port}/v1"

    # --------------------------------------------------------------- 生命周期

    def start(self) -> "FakeLLM":
        """在后台线程起 uvicorn，**轮询到真正 started** 后才返回。

        不等 started 就返回，客户端会拿到「连接被拒」的假失败。
        """
        app = FastAPI()

        @app.post("/v1/chat/completions")
        async def chat(req: Request):
            """记录请求体、按调用序取下一段文本，再交给流式生成器。"""
            body = await req.json()
            self.calls.append(body)

            index = len(self.calls) - 1
            text = self.texts[index] if index < len(self.texts) else "默认回复"

            async def gen():
                """按 OpenAI 顺序吐帧：reasoning → content → finish → usage → [DONE]。

                刻意逐帧 ``sleep``，让「流到一半断开/重连」这类时序可被稳定复现。
                """
                cid = f"chatcmpl-{index}"
                created = int(time.time())
                if self.include_reasoning:
                    yield _chunk(cid, created, reasoning=self.include_reasoning)

                for piece in _split(text):
                    if self.chunk_delay:
                        await asyncio.sleep(self.chunk_delay)
                    yield _chunk(cid, created, content=piece)

                yield _chunk(cid, created, content="", finish="stop")
                # include_usage 的尾包：choices 为空 + usage
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "id": cid,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": body.get("model", "fake"),
                            "choices": [],
                            "usage": {
                                "prompt_tokens": 11,
                                "completion_tokens": len(text),
                                "total_tokens": 11 + len(text),
                            },
                        },
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )
                yield "data: [DONE]\n\n"

            return StreamingResponse(gen(), media_type="text/event-stream")

        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()

        deadline = time.time() + 10
        while time.time() < deadline:
            if getattr(self._server, "started", False):
                return self
            time.sleep(0.02)
        raise RuntimeError("fake LLM 启动超时")

    def stop(self) -> None:
        """置 ``should_exit`` 再 join 线程：不收尾会把端口和线程泄漏给下一个用例。"""
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)


def _split(text: str) -> list[str]:
    """按 2 个字符切块，制造足够多的 token 事件用于断线测试。"""
    return [text[i : i + 2] for i in range(0, len(text), 2)] or [""]


def _chunk(
    cid: str, created: int, content: str | None = None, reasoning: str | None = None,
    finish: str | None = None,
) -> str:
    """拼一个 ``chat.completion.chunk`` 的 SSE 帧（reasoning_content 为 DeepSeek 等方言）。"""
    delta: dict = {}
    if content:
        delta["content"] = content
    if reasoning:
        delta["reasoning_content"] = reasoning
    payload = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": "fake-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
