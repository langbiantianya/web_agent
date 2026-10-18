"""单 Agent 执行：OpenAI 兼容协议流式调用（设计文档 §6.3）。

要点：
- 统一走 ``/v1/chat/completions``，``base_url`` 可覆盖，OpenAI / DeepSeek / Qwen /
  vLLM 全部可接；
- 每个 delta 转成 ``token`` / ``reasoning`` / ``tool_call`` 事件交给 RunHub；
- **全程只用异步调用**：AsyncOpenAI + asyncio，禁止任何同步阻塞；
- 每个 chunk 都检查取消标志，用户点「停止」后当前流立即收尾。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from openai import APIError, AsyncOpenAI, NOT_GIVEN

from app.config.settings import settings
from app.core.crypto import decrypt
from app.core.errors import RunCancelled
from app.core.run_hub import RunEventDTO, RunHub
from app.models.agent import Agent
from app.models.enums import EventType

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AgentResult:
    text: str = ""
    reasoning: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)


class LLMClientFactory:
    """按 (base_url, api_key) 复用 AsyncOpenAI 客户端，避免重复建 httpx 连接池。"""

    def __init__(self) -> None:
        """维护 ``(base_url, api_key) → AsyncOpenAI`` 的客户端缓存表。

        缓存键里放的是**解密后的明文 key**：``api_key_cipher`` 每次加密密文都
        不同（Fernet 随机 IV），若拿密文做键，同一个 Agent 每换一次配置就会新建
        一个客户端，httpx 连接池永远复用不上。
        """
        self._clients: dict[tuple[str, str], AsyncOpenAI] = {}

    def get(self, agent: Agent) -> AsyncOpenAI:
        """按 ``(base_url, api_key)`` 取复用客户端，缺失时现建一个。

        Args:
            agent: 已持久化的 Agent 配置。``api_key_cipher`` 存的是密文，这里会
                先解密；明文为空时用 ``"EMPTY"`` 占位，让本地 vLLM / Ollama 这类
                不校验 key 的端点也能正常调用。

        Returns:
            可直接 ``await client.chat.completions.create(...)`` 的异步客户端。

        注意构造时 ``max_retries=0``：重试节奏由编排器的节点级指数退避统一负责，
        否则 SDK 会在内部悄悄重试，把 SSE 吐字一起卡住。
        """
        base_url = agent.base_url.rstrip("/")
        api_key = decrypt(agent.api_key_cipher) or "EMPTY"
        key = (base_url, api_key)
        client = self._clients.get(key)
        if client is None:
            client = AsyncOpenAI(
                base_url=base_url,
                api_key=api_key,
                timeout=settings.openai_timeout,
                max_retries=0,  # 重试策略由节点的指数退避统一负责
            )
            self._clients[key] = client
        return client

    async def aclose(self) -> None:
        """关闭并清空所有缓存客户端（进程退出时调用）。

        关连接是为了让底层 httpx 连接池释放文件描述符；清空缓存是为了让下次调用
        按新配置（例如换过 base_url）重新建连接，而不是拿到一个已失效的旧实例。
        """
        for client in self._clients.values():
            await client.close()
        self._clients.clear()


_client_factory = LLMClientFactory()


class AgentRunner:
    """执行单个节点。每产生一个 delta 就 emit 一次事件。"""

    def __init__(
        self,
        agent: Agent,
        node_id: str,
        run_id: int,
        hub: RunHub,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> None:
        """绑定一次「节点 × run」的执行上下文。

        关键状态：
        - ``hub``：事件出口，token / reasoning / tool_call 都经它分配 seq；
        - ``is_cancelled``：取消探针，默认恒为 False，便于单测脱离 RunHub 直接
          构造 Runner。编排器会传入 ``hub.is_cancelled`` 的闭包，每个 chunk 轮询
          一次，用户点「停止」后当前流在下一个 chunk 收尾。
        """
        self.agent = agent
        self.node_id = node_id
        self.run_id = run_id
        self.hub = hub
        self.is_cancelled = is_cancelled or (lambda: False)

    async def run(self, messages: list[dict]) -> AgentResult:
        """流式跑完一次对话，把每个 delta 实时发出去。

        流式调用有两个协议坑：
        1. 部分自建 / vLLM 端点不支持 ``stream_options.include_usage``。这里先
           带上请求，报错后退回无 usage 的形式重试一次，而不是直接判失败；
        2. ``tool_calls`` 是**分片**下发的——id 和 function.name 通常只出现在首片，
           arguments 是逐片拼接的 JSON 字符串，必须按 index 攒到流结束才完整。

        每个 chunk 开头查一次取消标志，所以「停止」的响应延迟约等于一个 chunk
        的间隔；``finally`` 里保证关流，异常路径不会泄漏 httpx 连接。

        Args:
            messages: 已装配好的 system + 历史上下文 + 本轮输入。

        Returns:
            ``AgentResult``：完整文本、思维链、工具调用与 usage 汇总。

        Raises:
            RunCancelled: 用户取消，或编排器在重试间隙置了取消标志。
            APIError: 端点返回错误且重试次数耗尽（由编排器决定是否重试）。
        """
        client = _client_factory.get(self.agent)
        result = AgentResult()
        stream = None
        pending_tool_calls: dict[int, dict] = {}
        want_usage = True

        try:
            while True:
                kwargs = self._build_kwargs(messages, want_usage)
                try:
                    stream = await client.chat.completions.create(**kwargs)
                    break
                except APIError as exc:
                    # 部分自建/vLLM 端点不支持 stream_options.include_usage，
                    # 去掉后重试一次即可，不影响主流程。
                    if want_usage and "stream_options" in str(exc):
                        logger.warning(
                            "端点 %s 不支持 stream_options.include_usage，退回无 usage 流",
                            self.agent.base_url,
                        )
                        want_usage = False
                        continue
                    raise

            async for chunk in stream:
                if self.is_cancelled():
                    raise RunCancelled(f"run {self.run_id} 已被取消")

                if getattr(chunk, "usage", None):
                    u = chunk.usage
                    result.usage = {
                        "prompt_tokens": getattr(u, "prompt_tokens", 0) or 0,
                        "completion_tokens": getattr(u, "completion_tokens", 0) or 0,
                        "total_tokens": getattr(u, "total_tokens", 0) or 0,
                    }
                if not getattr(chunk, "choices", None):
                    continue

                delta = chunk.choices[0].delta

                content = getattr(delta, "content", None)
                if content:
                    result.text += content
                    await self._emit(EventType.TOKEN, content)

                # DeepSeek / Qwen 等的思维链字段
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    result.reasoning += reasoning
                    await self._emit(EventType.REASONING, reasoning)

                for tc in getattr(delta, "tool_calls", None) or ():
                    self._accumulate_tool_call(pending_tool_calls, tc)

            if pending_tool_calls:
                result.tool_calls = [pending_tool_calls[i] for i in sorted(pending_tool_calls)]
                await self._emit(
                    EventType.TOOL_CALL,
                    {"node": self.node_id, "toolCalls": result.tool_calls},
                )
            return result

        finally:
            if stream is not None:
                try:
                    await stream.close()
                except Exception:  # pragma: no cover
                    logger.debug("关闭 LLM 流失败", exc_info=True)

    # ------------------------------------------------------------- 内部

    def _build_kwargs(self, messages: list[dict], want_usage: bool) -> dict[str, Any]:
        """拼 ``chat.completions.create`` 的参数。

        ``extra_params`` 里的自定义键（top_p、seed、thinking 等）在最后覆盖基础
        值，优先级最高——改 Agent 配置就能调参，不用改代码。``tool_choice`` 是
        唯一例外：没配 tools 时带上它会被多数端点拒掉，所以此时跳过。

        Args:
            messages: 已装配好的 system + 会话历史 + 本轮输入。
            want_usage: 为 False 时不带 ``stream_options``，用于兼容不支持
                ``include_usage`` 的端点（配合 run() 里的重试逻辑）。
        """
        extra = self.agent.extra_params or {}
        kwargs: dict[str, Any] = {
            "model": self.agent.model,
            "messages": messages,
            "temperature": float(self.agent.temperature or 0.7),
            "max_tokens": self.agent.max_tokens or 2048,
            "stream": True,
        }
        tools = self.agent.tools
        if tools:
            kwargs["tools"] = tools
            if extra.get("tool_choice") is not None:
                kwargs["tool_choice"] = extra["tool_choice"]
        if want_usage:
            kwargs["stream_options"] = {"include_usage": True}
        # extra_params 里的自定义参数（如 top_p、seed、thinking）优先级最高
        for k, v in extra.items():
            if k == "tool_choice" and not tools:
                continue
            kwargs[k] = v
        return kwargs

    async def _emit(self, event_type: EventType, payload: Any) -> RunEventDTO:
        """发一个节点事件，自动补上 ``node`` 字段。

        字符串 payload 包成 ``{"content": ...}``（token / reasoning），其余按
        dict 合并进去（tool_call）——前端只认「统一的 node + 扁平字段」这一种
        结构，不必为每种事件写分支。

        seq 的分配与顺序保证由 ``RunHub.emit`` 负责：它在同一把锁里完成
        「分配 seq → 追加落库队列 → 广播订阅者」，所以这里**不能**绕过 hub
        自己发事件，否则 seq 与广播顺序就会脱节，前端会看到乱序。
        """
        body = {"node": self.node_id}
        if isinstance(payload, str):
            body["content"] = payload
        else:
            body.update(payload)
        return await self.hub.emit(self.run_id, event_type, body)

    @staticmethod
    def _accumulate_tool_call(store: dict[int, dict], tc: Any) -> None:
        """把一个 tool_call 分片合并进 ``store``。

        OpenAI 协议把一次工具调用拆成多片下发：``id`` 和 ``function.name`` 通常
        只出现在首片，``arguments`` 是逐片拼接的 JSON 字符串。所以 name 用覆盖、
        arguments 用累加，且每片都可能缺字段，必须能容忍 None。

        按 ``tc.index`` 建槽而非按到达顺序追加——并行工具调用的分片是交错到达的，
        按顺序拼会得到张冠李戴的参数。

        Args:
            store: 按 index 累积的结果字典，流结束时据此还原完整 tool_calls。
            tc: 当前 chunk 里的一个 tool_call 分片。
        """
        idx = getattr(tc, "index", 0) or 0
        slot = store.setdefault(
            idx, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
        )
        if getattr(tc, "id", None):
            slot["id"] = tc.id
        fn = getattr(tc, "function", None)
        if fn is not None:
            if getattr(fn, "name", None):
                slot["function"]["name"] = fn.name
            if getattr(fn, "arguments", None):
                slot["function"]["arguments"] += fn.arguments
