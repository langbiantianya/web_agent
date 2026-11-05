"""测试用的真实 Streamable HTTP MCP 服务器。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mcp.server import MCPServer  # noqa: E402

server = MCPServer("fixture-http", version="1.0.0")


@server.tool(description="通过 Streamable HTTP 回显文本。")
def echo(text: str) -> str:
    """回显传入文本。"""
    return f"http: {text}"


@server.tool(description="返回一个协议级工具错误。")
def boom() -> str:
    """故意失败。"""
    raise RuntimeError("http fixture error")


if __name__ == "__main__":
    server.run(
        transport="streamable-http",
        host="127.0.0.1",
        port=int(os.environ.get("MCP_FIXTURE_PORT", "0")),
        json_response=True,
        stateless_http=True,
    )
