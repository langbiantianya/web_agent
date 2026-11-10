"""管理端 MCP 操作的校验与能力映射。"""

from __future__ import annotations

from typing import Any

from app.core.mcp.transport import (
    McpTransportError, check_command_allowed, check_url_allowed,
)
from app.repo.mcp_repo import mcp_server_repo, mcp_tool_repo


async def validate_mcp_connection(
    transport: str,
    *,
    command: str | None,
    url: str | None,
) -> None:
    """在保存前执行 MCP 命令和 URL 的边界校验。"""
    if transport == "stdio":
        if not command:
            raise ValueError("command_required")
        if url:
            raise ValueError("url_not_allowed_for_stdio")
        check_command_allowed(command)
    elif transport == "http":
        if not url:
            raise ValueError("url_required")
        if command:
            raise ValueError("command_not_allowed_for_http")
        await check_url_allowed(url, require_dns=False)
    else:
        raise ValueError("invalid_transport")


async def page_bindings(
    db: Any,
    servers: list[Any],
    tenant_id: str,
) -> tuple[dict[int, list[Any]], dict[str, list[str]]]:
    """加载每台 MCP 服务器的工具与 Agent 引用。"""
    tools_by_server = {
        server.id: await mcp_tool_repo.list_by_server(db, server.id)
        for server in servers
    }
    used_by_by_server = {
        server.code: await mcp_server_repo.agents_using(db, server, tenant_id)
        for server in servers
    }
    return tools_by_server, used_by_by_server


__all__ = ["McpTransportError", "page_bindings", "validate_mcp_connection"]
