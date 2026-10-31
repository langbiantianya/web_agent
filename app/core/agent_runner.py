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
from app.core.errors import RunCancelled, SensitiveContentError, ToolLoopExceeded
from app.core.run_hub import RunEventDTO, RunHub
from app.core.sensitive import screen_messages
from app.core.tools import build_openai_tools, execute_tool
from app.models.agent import Agent
from app.models.enums import EventType

logger = logging.getLogger(__name__)


def _truncate(text: str) -> str:
    """截断过长的工具结果。

    工具结果要回灌进下一轮的 prompt，不截断的话一个返回 1MB 的工具能把 prompt
    撑爆，而 token 账单按字符走。

    Args:
        text: 工具返回的原始文本。

    Returns:
        截断后的文本（超长时带一个说明标记）。
    """
    limit = settings.tool_result_max_chars
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（结果过长，已截断到 {limit} 字符）"


@dataclass(slots=True)
class AgentResult:
    text: str = ""
    reasoning: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    # 用了几轮工具调用（V3.2）。0 表示模型没要过工具。
    tool_rounds: int = 0


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
            SensitiveContentError: 消息里命中敏感内容（LLM 边界兜底）。
        """
        # LLM 边界的最后一道过滤。API 层已经拦过一遍，这里再拦一次，是为了让
        # 「进入 LLM 的内容一定被过滤过」这条不变量对**所有路径**成立——历史
        # 消息、绕开 API 的内部调用、别的服务复用本模块，都逃不掉这一关。
        self._guard_sensitive(messages)

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

    # ------------------------------------------------------------- 工具闭环

    async def run_with_tools(self, messages: list[dict]) -> AgentResult:
        """跑一次节点，必要时让模型多轮调用工具（V3.2）。

        闭环的形状是：**模型要工具 → 执行工具 → 结果以 ``role=tool`` 回灌 →
        模型继续**，直到某一轮不再要工具。

        ## 为什么结果要回灌，而不是直接结束

        模型要工具时，它的意图是「拿到数据再回答」。直接结束等于让它话说一半就
        没了——把工具结果回灌回去，模型才有条件给出真正的答案。

        ## 文本是各轮拼接，不是只取最后一轮

        流里 emit 的 token 就是各轮的拼接，落库的 assistant 消息也是同一个拼接，
        于是「流文本 == 库文本」这条性质依然成立（设计文档 §14.3 依赖它）。
        代价是中间轮模型的过程性发言（"我先算一下"）也会进最终产出——那本来就是
        它说出口的话，留在上下文里不算错。

        ## 工具报错不直接失败节点

        工具执行失败时把错误文本回灌给模型，让它有机会自己纠正（换个参数、或者
        干脆不用这个工具）。真正兜不住的是「模型一直调同一个必失败的工具」，
        那由轮次上限收口。

        ## 轮次预算怎么算

        ``tool_max_rounds`` 是**模型调用次数**的上限，不是工具执行次数。模型在
        最后一轮还想要工具，就判定它陷住了——这时**不执行**那次工具：执行了也
        没有下一轮去读结果，白跑一次。

        于是预算的实际效果是：模型最多能用上 ``max_rounds - 1`` 个工具结果，
        并在第 ``max_rounds`` 次调用里给出最终答案。

        Args:
            messages: 已装配好的 system + 历史上下文 + 本轮输入。

        Returns:
            ``AgentResult``：各轮拼接的文本、全部工具调用、累计 usage。

        Raises:
            ToolLoopExceeded: 工具调用轮数超过上限。
            RunCancelled: 用户取消。
            APIError: 端点返回错误且重试次数耗尽（由编排器决定是否重试）。
        """
        convo = list(messages)
        total_text = ""
        total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        all_tool_calls: list[dict] = []
        rounds = 0
        max_rounds = max(1, settings.tool_max_rounds)

        for round_no in range(1, max_rounds + 1):
            result = await self.run(convo)
            total_text += result.text
            for key in total_usage:
                total_usage[key] += result.usage.get(key, 0)

            if not result.tool_calls:
                result.text = total_text
                result.usage = total_usage
                result.tool_calls = all_tool_calls
                result.tool_rounds = rounds
                return result

            rounds = round_no
            if round_no == max_rounds:
                # 预算用尽，模型还要工具：判定它陷住了。
                # 这里**不执行**那次工具——执行了也没人消费（没有下一轮去读结果），
                # 白跑一次。直接失败，把「陷住了」告诉用户。
                break

            all_tool_calls.extend(result.tool_calls)
            # 模型这一轮说的话要原样带回去——OpenAI 协议要求 assistant 消息带上
            # 与后续 tool 消息配对的 tool_calls（id 必须一致）
            convo.append({
                "role": "assistant",
                "content": result.text or None,
                "tool_calls": result.tool_calls,
            })

            for tc in result.tool_calls:
                fn = tc.get("function") or {}
                name = fn.get("name") or ""
                ok, output = await execute_tool(name, fn.get("arguments") or "{}")
                output = _truncate(output)
                await self._emit(
                    EventType.TOOL_RESULT,
                    {"tool": name, "ok": ok, "summary": output[:200]},
                )
                convo.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id") or "",
                    "name": name,
                    "content": output,
                })

        raise ToolLoopExceeded(max_rounds)

    # ------------------------------------------------------------- 内部

    def _guard_sensitive(self, messages: list[dict]) -> None:
        """发送前的敏感词兜底检查。

        API 层（``POST /api/conversations/{id}/messages``）已经拦过一遍了，
        这里再拦一次不是重复劳动，而是补上那条**兜底缝**：历史消息是早于过滤
        功能入库的，绕开 API 的内部调用、以及复用本模块的别的服务，都不会
        走 API 层那道检查。有了这一关，「进入 LLM 的内容一定被过滤过」才是
        一条对所有路径成立的**不变量**，而不是一句约定。

        只查 ``role == "user"`` 的消息：system 提示词是我们自己写的，tool
        消息是模型自己的产出——把它们也判一遍，等于让模型自己把自己绊倒。

        Raises:
            SensitiveContentError: 命中敏感内容。
        """
        hit = screen_messages(messages)
        if hit is None:
            return
        index, verdict = hit
        # 只记分数和位置，不记命中词——把敏感词写进日志等于给日志库造一份副本。
        logger.warning(
            "LLM 边界拦下敏感内容：消息下标 %s，分数 %.3f，位置 %d",
            index,
            verdict.score,
            verdict.position,
        )
        raise SensitiveContentError(
            "内容包含不被允许的请求，请调整措辞后重试",
            data={"score": verdict.score, "excerpt": verdict.excerpt},
        )

    def _build_kwargs(self, messages: list[dict], want_usage: bool) -> dict[str, Any]:
        """拼 ``chat.completions.create`` 的参数。

        ``extra_params`` 里的自定义键（top_p、seed、thinking 等）在最后覆盖基础
        值，优先级最高——改 Agent 配置就能调参，不用改代码。``tool_choice`` 是
        唯一例外：没配 tools 时带上它会被多数端点拒掉，所以此时跳过。

        ``tools`` 存的是**工具名**（见 :mod:`app.core.tools`），这里从注册表展开成
        OpenAI 的 function-calling schema。展开而不是透传的原因见该模块的说明：
        「模型能调的」与「服务端能执行的」必须是同一张表。

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
        tools = build_openai_tools(self.agent.tools)
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
