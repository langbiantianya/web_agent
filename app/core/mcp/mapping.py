"""MCP 工具结果 → 给模型看的文本（设计文档 §21.9）。

MCP 的 ``CallToolResult`` 与本项目的 ``(ok, text)`` 之间需要一个明确的转换。
口径与 §17.5 一致：**所有错误一律回灌，不失败节点**——模型看见「参数错了」有机会
自己纠正，比直接让整个节点失败有用得多。真正兜不住的是「模型一直调同一个必失败
的工具」，那由轮次上限收口。

## 非文本内容为什么要占位

OpenAI 的 ``role=tool`` 消息只接受字符串内容，图片/音频塞不进去。所以
``ImageContent`` / ``AudioContent`` / ``ResourceLink`` 在文本位置写一句占位
（如「返回了 1 张图片（image/png，20480 字节），本通道暂不支持直接回灌」），让模型
知道「有东西但你看不到」，而不是让它以为工具返回了空结果。

``EmbeddedResource`` 里 ``mimeType`` 是 ``text/*`` 时可以把 ``text`` 成员提取出来当
普通文本用——那本来就是文本，没道理丢掉。
"""

from __future__ import annotations

import json
from typing import Any

# 结果文本的字符上限。复用调用方的口径：太长会挤爆下一轮的 prompt。
DEFAULT_MAX_CHARS = 4000


def _truncate(text: str, max_chars: int) -> str:
    """超长截断并说明截断了。"""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"…（内容过长，已截断到 {max_chars} 字符）"


def _content_text(item: Any) -> str | None:
    """从一条内容里取出纯文本；不是文本则返回 ``None``。"""
    kind = getattr(item, "type", None)
    if kind == "text":
        return getattr(item, "text", "") or ""
    if kind == "resource":
        # EmbeddedResource：里面的 resource 可能是 text 资源。
        resource = getattr(item, "resource", None)
        mime = getattr(resource, "mimeType", None) or getattr(
            resource, "mime_type", ""
        )
        if isinstance(mime, str) and mime.startswith("text/"):
            return getattr(resource, "text", None)
    return None


def _content_placeholder(item: Any) -> str:
    """给非文本内容写一句占位说明。"""
    kind = getattr(item, "type", None)
    mime = getattr(item, "mimeType", None) or getattr(item, "mime_type", "")
    if kind == "image":
        data = getattr(item, "data", "") or ""
        # base64 长度 → 字节数的近似（够用来告诉模型「大概多大」）。
        size = len(data) * 3 // 4
        return f"返回了 1 张图片（{mime or '未知类型'}，约 {size} 字节），本通道只传文本，无法直接回灌。"
    if kind == "audio":
        return f"返回了 1 段音频（{mime or '未知类型'}），本通道只传文本，无法直接回灌。"
    if kind == "resource_link":
        uri = getattr(item, "uri", "")
        return f"返回了一个资源链接（{uri}），本通道只传文本，无法直接回灌。"
    if kind == "resource":
        resource = getattr(item, "resource", None)
        mime = getattr(resource, "mimeType", None) or getattr(
            resource, "mime_type", ""
        )
        uri = getattr(resource, "uri", "")
        return f"返回了一个内嵌资源（{mime or '未知类型'}，{uri}），本通道只传文本，无法直接回灌。"
    return f"返回了 1 段本通道不支持的内容（{kind or '未知类型'}），无法直接回灌。"


def map_call_result(result: Any, max_chars: int = DEFAULT_MAX_CHARS) -> tuple[bool, str]:
    """把 MCP 的 ``CallToolResult`` 映射成 ``(ok, text)``。

    | MCP 侧 | 我们的侧 |
    | --- | --- |
    | ``content: [TextContent]`` | ``ok=True``，文本为其文本 |
    | 多个 ``TextContent`` | ``ok=True``，用 ``\\n`` 连接（顺序保留） |
    | ``structuredContent`` 且无 text | ``ok=True``，文本为其 JSON 序列化 |
    | ``isError: true`` | **``ok=False``**，文本为其内容 |
    | 非文本内容 | ``ok=True``，文本为占位说明 |

    Args:
        result: MCP 的 ``CallToolResult``。
        max_chars: 返回文本的字符上限。

    Returns:
        ``(ok, text)``。``ok`` 为 False 时 ``text`` 是给模型看的错误说明。
    """
    is_error = bool(getattr(result, "is_error", False))
    content = list(getattr(result, "content", None) or [])

    parts: list[str] = []
    for item in content:
        text = _content_text(item)
        if text is None:
            parts.append(_content_placeholder(item))
        elif text:
            parts.append(text)

    if not parts:
        # 没有 content 时兜底用 structuredContent 的 JSON 序列化——规范要求有
        # structured 时**同时**给一份 text，但第三方服务器未必照做。
        structured = getattr(result, "structured_content", None)
        if structured is not None:
            parts.append(json.dumps(structured, ensure_ascii=False))

    text = "\n".join(parts)
    if not text:
        text = "（工具没有返回任何内容）"

    if is_error:
        # 工具**执行**错误：模型有机会自己纠正，与 §17.5 同一口径。
        return False, _truncate(text, max_chars)
    return True, _truncate(text, max_chars)
