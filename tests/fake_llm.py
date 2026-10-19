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

    ``tool_calls_plan`` 让某一次调用**不要文本、改为请求工具**（V3.2）：第 N 项
    对应第 N 次调用，值为一个工具调用列表，每项形如
    ``{"id": "call_1", "name": "calc", "arguments": '{"expression": "1+1"}'}``。
    没配到的调用照常返回 ``texts``。

    ``fixed_text`` 让**每次调用都返回同一段文本**（压测用）：``texts`` 是按调用
    序取的，并发下每次调用拿到哪一段是不确定的——压测需要的是**均匀**的负载。
    设了它之后 ``texts`` 与 ``tool_calls_plan`` 都不再生效。
    """

    texts: list[str] = field(default_factory=lambda: ["你好", "，", "世界"])
    chunk_delay: float = 0.0
    include_reasoning: str = ""
    tool_calls_plan: list[list[dict]] = field(default_factory=list)
    fixed_text: str | None = None
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
            if self.fixed_text is not None:
                # 压测模式：每次调用都吐同一段，负载才均匀
                text = self.fixed_text
                planned = None
            else:
                text = self.texts[index] if index < len(self.texts) else "默认回复"
                planned = (
                    self.tool_calls_plan[index]
                    if index < len(self.tool_calls_plan)
                    else None
                )

            async def gen():
                """按 OpenAI 顺序吐帧：reasoning → content → finish → usage → [DONE]。

                刻意逐帧 ``sleep``，让「流到一半断开/重连」这类时序可被稳定复现。
                """
                cid = f"chatcmpl-{index}"
                created = int(time.time())
                if self.include_reasoning:
                    yield _chunk(cid, created, reasoning=self.include_reasoning)

                if planned:
                    # 这一轮模型不要文本，改成请求工具
                    for piece in _tool_call_frames(cid, created, planned):
                        if self.chunk_delay:
                            await asyncio.sleep(self.chunk_delay)
                        yield piece
                else:
                    for piece in _split(text):
                        if self.chunk_delay:
                            await asyncio.sleep(self.chunk_delay)
                        yield _chunk(cid, created, content=piece)

                finish = "tool_calls" if planned else "stop"
                yield _chunk(cid, created, content="", finish=finish)
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


def _tool_call_frames(cid: str, created: int, calls: list[dict]) -> list[str]:
    """把工具调用摊成 OpenAI 的**分片**格式（V3.2）。

    真实端点不会把一次 tool_call 整块发下来：``id`` 与 ``function.name`` 通常
    只在首片，``arguments`` 是逐片拼接的 JSON 字符串。假服务必须照着这个形状
    发，否则「分片累积」那段代码在测试里永远走不到——一个只发整块的假服务，
    会让分片处理写错了也没人发现。

    Args:
        cid: 本次会话的 id。
        created: Unix 时间戳。
        calls: 工具调用列表，每项含 ``id`` / ``name`` / ``arguments``。

    Returns:
        SSE 帧字符串列表。
    """
    frames: list[str] = []
    for index, call in enumerate(calls):
        args = call.get("arguments") or "{}"
        # arguments 劈成两片：验证「逐片拼接」而不是「后片覆盖前片」
        cut = max(1, len(args) // 2)
        for piece, is_first in ((args[:cut], True), (args[cut:], False)):
            delta: dict = {
                "index": index,
                "type": "function",
                "function": {"arguments": piece},
            }
            if is_first:
                delta["id"] = call.get("id") or f"call_{index}"
                delta["function"]["name"] = call.get("name") or ""
            frames.append(_raw_chunk(cid, created, {"tool_calls": [delta]}))
    return frames


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


def _raw_chunk(cid: str, created: int, delta: dict) -> str:
    """拼一个指定 ``delta`` 内容的 chunk（用于 tool_calls 这种非文本增量）。

    Args:
        cid: 会话 id。
        created: Unix 时间戳。
        delta: 直接放进 ``choices[0].delta`` 的字典。

    Returns:
        SSE 帧字符串。
    """
    payload = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": "fake-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
