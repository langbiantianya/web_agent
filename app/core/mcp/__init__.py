"""MCP 客户端（设计文档 §21）。

本项目是 MCP **客户端 / host**：连接外部 MCP 服务器，把它们的工具接进现有的
工具闭环（§17.3）。

分四块：

- :mod:`app.core.mcp.naming` —— ``mcp__<server>__<tool>`` 命名空间映射；
- :mod:`app.core.mcp.transport` —— 两种传输（stdio / Streamable HTTP）+ SSRF 防护；
- :mod:`app.core.mcp.manager` —— 连接生命周期（惰性连接、空闲回收、并发闸门）；
- :mod:`app.core.mcp.mapping` —— ``CallToolResult`` → ``(ok, text)``；
- :mod:`app.core.mcp.catalog` —— 工具发现与快照落库。
"""

from __future__ import annotations

__all__ = ["catalog", "manager", "mapping", "naming", "transport"]
