"""测试用的真实 MCP 服务器（stdio 传输）。

**为什么要有它**：MCP 的价值全在「第三方进程按协议提供工具」这件事上。用 mock
把 SDK 的返回结构造出来，等于只测了「我们能解析自己造的数据」——协议对不对、
分页怎么翻、`isError` 怎么表达，全都没被覆盖。

所以这里用官方 SDK 的 :class:`MCPServer` 起一个**真的**服务器，让测试用
**真的** stdio 子进程连上去。

这个文件既能被测试当子进程拉起（``python tests/fixtures/mcp_server_stdio.py``），
也能被 ``python -m tests.fixtures.mcp_server_stdio`` 启动。

工具清单刻意覆盖几种形状：

- ``echo`` —— 普通文本工具，正常返回；
- ``boom`` —— 抛异常，测试 ``isError`` 的映射；
- ``binary`` —— 返回图片，测试非文本内容的占位说明。
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

# 允许直接 `python tests/fixtures/mcp_server_stdio.py` 运行。
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mcp.server import MCPServer  # noqa: E402
from mcp.types import ImageContent  # noqa: E402

server = MCPServer("fixture-tools", version="1.0.0")


@server.tool(description="回显传入的文本。")
def echo(text: str) -> str:
    """回显。

    Args:
        text: 要回显的文本。

    Returns:
        原样回显。
    """
    return f"echo: {text}"


@server.tool(description="总是失败的工具，用来测 isError。")
def boom() -> str:
    """故意失败。

    Raises:
        RuntimeError: 总是抛。
    """
    raise RuntimeError("故意失败的工具")


@server.tool(description="返回一张 1x1 的 PNG。")
def binary() -> ImageContent:
    """返回一张图片。

    Returns:
        一张 1x1 透明 PNG。
    """
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
    )
    return ImageContent(type="image", data=base64.b64encode(png).decode(), mimeType="image/png")


def main() -> None:
    """以 stdio 传输启动服务器。

    stdout 只能有协议消息——所以这里**不能**有别的 print。
    """
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
