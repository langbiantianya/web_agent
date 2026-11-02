"""MCP 工具的命名空间映射（设计文档 §21.5）。

## 为什么要有这一层

内置工具叫 ``calc``、``now``；MCP 工具有自己的名字，两台服务器的同名工具会撞。

OpenAI function-calling 的函数名约束是 ``^[a-zA-Z0-9_-]{1,64}$``——**没有点号**。
所以 ``weather.get_weather`` 非法，``weather__get_weather`` 合法。

采用 Claude Code 的约定：

```text
mcp__<server_code>__<tool_name>
```

- ``server_code``：服务器在本项目 ``mcp_server`` 表里的 ``code``，**不是** MCP 协议里
  的 ``serverInfo.name``——后者是服务端自报的，不同服务器可能重名，不能当主键；
- ``tool_name``：MCP 服务器自己的工具名，原样保留。

## 超长的名字怎么办

OpenAI 的上限是 64 字符。超长的工具名**不导入、不截断**——截断会把两个不同的工具
撞成同一个名，那比不导入更糟。
"""

from __future__ import annotations

import re

# OpenAI function-calling 的函数名约束。
OPENAI_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# MCP 限定名的前缀。
MCP_PREFIX = "mcp__"
# server_code 与 tool_name 之间的分隔符。
MCP_SEPARATOR = "__"

# 内部工具名的分段上限。server_code 本身受 mcp_server.code 的列宽（64）约束，
# tool_name 受 OpenAI 的 64 约束；两者相加再加上分隔符，很容易超过 64。
MAX_QUALIFIED_LEN = 64


class McpNamingError(ValueError):
    """工具名不合法——形状不对或超长。"""


def is_mcp_name(name: str) -> bool:
    """这个名字是不是一个 MCP 限定名。

    Args:
        name: 工具名。

    Returns:
        以 ``mcp__`` 开头就是 True。
    """
    return isinstance(name, str) and name.startswith(MCP_PREFIX)


def build_qualified_name(server_code: str, tool_name: str) -> str:
    """拼出 MCP 限定名。

    Args:
        server_code: 本项目里的服务器 code。
        tool_name: MCP 服务器自己的工具名。

    Returns:
        ``mcp__<server_code>__<tool_name>``。

    Raises:
        McpNamingError: 拼出来的名字不满足 OpenAI 的函数名约束——超长的工具名
            **不导入、不截断**（截断会把两个不同的工具撞成同一个名）。
    """
    qualified = f"{MCP_PREFIX}{server_code}{MCP_SEPARATOR}{tool_name}"
    if not OPENAI_NAME_PATTERN.match(qualified):
        raise McpNamingError(
            f"工具名不满足 OpenAI 函数名约束（^[A-Za-z0-9_-]{{1,64}}$）："
            f"{qualified!r}（长度 {len(qualified)}）"
        )
    return qualified


def parse_qualified_name(name: str) -> tuple[str, str]:
    """拆开一个 MCP 限定名。

    Args:
        name: ``mcp__<server_code>__<tool_name>``。

    Returns:
        ``(server_code, tool_name)``。

    Raises:
        McpNamingError: 不是合法的 MCP 限定名。
    """
    if not is_mcp_name(name):
        raise McpNamingError(f"不是 MCP 限定名：{name!r}")
    rest = name[len(MCP_PREFIX) :]
    server_code, sep, tool_name = rest.partition(MCP_SEPARATOR)
    if not sep or not server_code or not tool_name:
        raise McpNamingError(f"MCP 限定名形状不对：{name!r}")
    return server_code, tool_name
