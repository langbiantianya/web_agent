"""工具目录（V3.2 内置工具 + V4 MCP 工具，设计文档 §17.7 / §21.11）。

暴露一个只读接口，让后台能列出「这台部署上有哪些工具可用」，而不是让用户在
输入框里凭记忆敲工具名——敲错了要等保存时才被拒。

V4 起条目多两个字段：

- ``source``：``builtin`` 还是 ``mcp``——前端据此分组显示，避免一个长列表里
  ``mcp__weather__get_weather`` 和 ``mcp__weather__get_forecast`` 混在一起
  看不出来自哪台服务器；
- ``server``：MCP 服务器的 code（内置工具没有这一项）。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.config.db import SessionLocal
from app.core.tools import TOOL_REGISTRY

router = APIRouter(prefix="/api/tools", tags=["tools"])


@router.get("")
async def list_tools() -> list[dict]:
    """列出全部可用工具（内置 + MCP）。

    Returns:
        每个工具一条：``name`` / ``description`` / ``parameters`` / ``source`` /
        ``server``。

    ``parameters`` 是 OpenAI function-calling 的 JSON Schema，前端可以直接拿它
    渲染「这个工具要什么参数」的提示，但**不能**拿它去执行——执行只认名字，
    对应的是服务端注册表里的实现。

    **离线服务器的工具也会列出来**（§21.6）：快照回答的是「有哪些工具」，
    「现在能不能用」由在线状态决定——那是运行时的事，不该让勾选框里的选项
    随连接状态跳来跳去。
    """
    items: list[dict] = [
        {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters,
            "source": "builtin",
            "server": None,
        }
        for spec in TOOL_REGISTRY.values()
    ]

    from app.repo.mcp_repo import mcp_server_repo, mcp_tool_repo

    async with SessionLocal() as db:
        rows = await mcp_tool_repo.list_all(db)
        servers = await mcp_server_repo.map_by_id(db)
    for row in rows:
        server = servers.get(row.server_id)
        items.append(
            {
                "name": row.qualified_name,
                "description": row.description or f"MCP 工具 {row.tool_name}",
                "parameters": row.input_schema or {"type": "object", "properties": {}},
                "source": "mcp",
                "server": server.code if server is not None else None,
            }
        )
    return items
