"""故意向 stdout 写普通日志的 MCP 服务器。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mcp.server import MCPServer  # noqa: E402

# stdout 是 JSON-RPC 控制通道；这行模拟第三方服务器错误的日志写法。
print("ordinary log before protocol", flush=True)

server = MCPServer("polluted-stdio", version="1.0.0")


@server.tool(description="不会真正可用，因为 stdout 已被污染。")
def echo(text: str) -> str:
    """回显。"""
    return text


if __name__ == "__main__":
    server.run(transport="stdio")
