"""端到端验收用的**真实** MCP 服务器（stdio 传输）。

为什么单独放一个文件，而不是直接用 ``tests/fixtures/mcp_server_stdio.py``：
测试目录不进镜像（见 ``Dockerfile`` 的说明），而这条验收流程要能在**部署环境
的客户端容器里**跑起来——它必须跟着镜像走。

工具刻意做成**确定性**的：输出里有固定的标记前缀（``E2E_ECHO:`` / ``E2E_SUM:``），
验收脚本据此断言「模型问的工具、服务端答的内容」确实对得上，而不是笼统地
断言「有工具调用发生」。

工具清单：

- ``echo`` —— 回显文本，标记 ``E2E_ECHO:``；
- ``add`` —— 整数求和，标记 ``E2E_SUM:``；
- ``now_iso`` —— 当前 UTC 时间；
- ``always_fail`` —— 总是抛异常，用来验证 ``isError`` 的映射（工具失败**不该**
  让整个节点失败，错误文本要回灌给模型）。

另有一个资源 ``e2e://guide`` 与一个 prompt ``summarize``，用来覆盖「资源/提示词
也能被读到」这一段协议面。

stdout 只能有协议帧——所以这个文件里**不能有 print**。
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

# 允许 ``python scripts/e2e_mcp_server.py`` 直接运行（容器里的工作目录是 /app）。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp.server import MCPServer  # noqa: E402

server = MCPServer("e2e-selfcheck-tools", version="1.0.0")


@server.resource(
    "e2e://guide",
    name="guide",
    description="端到端验收用的说明资源",
    mime_type="text/markdown",
)
def guide() -> str:
    """返回一段可读取的资源文本。

    Returns:
        Markdown 文本，内容固定，便于断言。
    """
    return "# E2E Guide\n端到端资源读取正常。"


@server.prompt(description="端到端验收用的 prompt")
def summarize(topic: str) -> list[dict]:
    """返回一条用户消息模板。

    Args:
        topic: 要总结的主题。

    Returns:
        OpenAI 风格的消息列表。
    """
    return [{"role": "user", "content": f"请总结：{topic}"}]


@server.tool(description="回显传入的文本（验收用固定标记）。")
def echo(text: str) -> str:
    """回显。

    Args:
        text: 任意文本。

    Returns:
        带 ``E2E_ECHO:`` 标记的回显结果。
    """
    return f"E2E_ECHO:{text}"


@server.tool(description="两数相加（验收用固定标记）。")
def add(a: int, b: int) -> str:
    """整数求和。

    Args:
        a: 第一个加数。
        b: 第二个加数。

    Returns:
        带 ``E2E_SUM:`` 标记的结果。
    """
    return f"E2E_SUM:{a + b}"


@server.tool(description="返回当前 UTC 时间。")
def now_iso() -> str:
    """当前时间。

    Returns:
        ISO-8601 格式的 UTC 时间。
    """
    return datetime.now(UTC).isoformat()


@server.tool(description="总是失败的工具，用来验证 isError 映射。")
def always_fail() -> str:
    """故意失败。

    Raises:
        RuntimeError: 总是抛。
    """
    raise RuntimeError("E2E 故意失败的工具")


def main() -> None:
    """以 stdio 传输启动服务器。"""
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
