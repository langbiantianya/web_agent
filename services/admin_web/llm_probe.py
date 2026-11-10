"""OpenAI 兼容接口的探测：拉模型列表 + 一次最小对话。

管理端要在端点配置页上回答两个问题：「这个地址+密钥能用吗」「它有哪些
模型、我选的这个模型真的能对话吗」。这里只做两件事：

- :func:`fetch_models` —— ``GET {base_url}/models``，返回 ``data[].id``；
- :func:`probe_chat` —— ``POST {base_url}/chat/completions``，用很小的
  ``max_tokens`` 发一句话，拿到任何一条 ``choices`` 就算通过。

**为什么不像 MCP 那样拦私网地址**：LLM 端点本来就允许指向自建 vLLM /
Ollama / 内网网关，拦掉内网等于把主要用法拦掉。这里只做两件边界校验——
只接受 http(s)、拒绝 URL 内嵌凭据；密钥只出现在请求头里，既不会写进异常
文案，也不会回显给页面。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

# 拉列表通常很快；对话测试要留出模型首字延迟，所以给更长的上限。
_MODELS_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_CHAT_TIMEOUT = httpx.Timeout(30.0, connect=5.0)

# 上游报错正文只截一小段：太多是网关的 HTML 错误页，对管理员没有信息量，
# 还可能把请求细节带进页面。
_MAX_DETAIL = 200

# 对话测试的输入：一句无意义的短句，避免模型「认真答题」花掉 token。
_PROBE_PROMPT = "ping"


class LlmProbeError(Exception):
    """探测失败；``str(exc)`` 是可以直接展示给管理员的短原因。

    ``status_code`` 是上游 HTTP 状态码（网络层失败时为 ``None``）。调用方据此
    区分「配置不对」（401/404）与「配置对但账号被限流」（402/429）——后者的
    地址与密钥其实已经验证过了。
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class ProbeResult:
    """一次最小对话的结果。"""

    latency_ms: int
    model: str
    reply: str


def _endpoint_url(base_url: str, path: str) -> str:
    """拼出 ``base_url`` 下的 OpenAI 兼容路径，并校验地址边界。"""
    base = (base_url or "").strip().rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise LlmProbeError("base_url must be an http(s) URL")
    if parsed.username or parsed.password:
        # 凭据必须走独立的 api_key，不能混进地址——否则会进日志与审计。
        raise LlmProbeError("base_url must not embed credentials")
    return f"{base}{path}"


def _short(text: str) -> str:
    """把上游正文压成单行短串。"""
    return " ".join((text or "").split())[:_MAX_DETAIL]


def _auth_headers(api_key: str) -> dict[str, str]:
    """按 OpenAI 协议放 Bearer 头；空 Key 也发请求（有些自建服务不校验）。"""
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


# 上游状态码 → 一句**稳定**的中性说明。
#
# 这里刻意**不回显上游正文**：上游的错误体可能带内部请求 ID、账号信息，
# 甚至把密钥片段回显出来（有些网关会在报错里重复一次密钥的前后几位）。
# 管理端需要的是「哪里错了」，不是上游的原始报文。
_STATUS_HINTS: dict[int, str] = {
    400: "The endpoint rejected the request. Check the model name.",
    401: "The API key was rejected. Check the key and its account.",
    403: "The endpoint refused the request. The key may lack a scope.",
    404: "The path was not found. Check the base URL.",
    408: "The endpoint timed out.",
    402: "The account has no remaining credit for this endpoint.",
    429: "The endpoint is rate limiting this key. Try again later.",
    500: "The endpoint returned a server error.",
    502: "The upstream gateway returned an invalid response.",
    503: "The endpoint is temporarily unavailable.",
    504: "The upstream gateway timed out.",
}


def _http_error(action: str, response: httpx.Response) -> LlmProbeError:
    """把非 2xx 响应翻成一句可展示的错误。

    只保留**状态码 + 归类说明**，不带上游正文（见 :data:`_STATUS_HINTS`）。
    """
    status = response.status_code
    hint = _STATUS_HINTS.get(status, "The endpoint returned an unexpected response.")
    return LlmProbeError(
        f"{action} failed: HTTP {status}. {hint}",
        status_code=status,
    )


async def fetch_models(
    base_url: str, api_key: str, *, timeout: httpx.Timeout | None = None
) -> list[str]:
    """``GET {base_url}/models``，返回去重排序后的模型 id 列表。"""
    url = _endpoint_url(base_url, "/models")
    try:
        async with httpx.AsyncClient(
            timeout=timeout or _MODELS_TIMEOUT, follow_redirects=False
        ) as client:
            response = await client.get(url, headers=_auth_headers(api_key))
    except httpx.HTTPError as exc:
        raise LlmProbeError(f"GET /models failed: {type(exc).__name__}") from exc
    if response.status_code >= 400:
        raise _http_error("GET /models", response)
    try:
        payload = response.json()
    except ValueError as exc:
        raise LlmProbeError("GET /models returned a non-JSON body") from exc
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise LlmProbeError("GET /models returned no data array")
    models = sorted(
        {
            str(item.get("id")).strip()
            for item in rows
            if isinstance(item, dict) and item.get("id")
        }
    )
    if not models:
        raise LlmProbeError("GET /models returned an empty model list")
    return models


async def probe_chat(
    base_url: str,
    api_key: str,
    model: str,
    *,
    timeout: httpx.Timeout | None = None,
) -> ProbeResult:
    """用一次最小对话验证「地址 + 密钥 + 模型」确实可用。

    ``max_tokens`` 取 16 而不是 1：少数推理型模型在 1 个 token 下会直接报参数
    错误，那会把「能用的配置」判成失败。16 个 token 的成本可以忽略。
    """
    selected = (model or "").strip()
    if not selected:
        raise LlmProbeError("model is required")
    url = _endpoint_url(base_url, "/chat/completions")
    payload = {
        "model": selected,
        "messages": [{"role": "user", "content": _PROBE_PROMPT}],
        "max_tokens": 16,
        "temperature": 0,
        "stream": False,
    }
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            timeout=timeout or _CHAT_TIMEOUT, follow_redirects=False
        ) as client:
            response = await client.post(
                url, json=payload, headers=_auth_headers(api_key)
            )
    except httpx.HTTPError as exc:
        raise LlmProbeError(
            f"POST /chat/completions failed: {type(exc).__name__}"
        ) from exc
    latency_ms = int((time.perf_counter() - started) * 1000)
    if response.status_code >= 400:
        raise _http_error("POST /chat/completions", response)
    try:
        body = response.json()
    except ValueError as exc:
        raise LlmProbeError(
            "POST /chat/completions returned a non-JSON body"
        ) from exc
    choices = body.get("choices") if isinstance(body, dict) else None
    if not isinstance(choices, list) or not choices:
        raise LlmProbeError("POST /chat/completions returned no choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = ""
    if isinstance(message, dict):
        content = _short(str(message.get("content") or ""))
    return ProbeResult(latency_ms=latency_ms, model=selected, reply=content)
