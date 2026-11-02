"""MCP 工具目录快照（设计文档 §21.6，V4）。

一台服务器**已发现**的工具。这张表让「模型能调的」与「服务端能执行的」在
**快照层面**仍然对得上——这是 §21.2 那条契约在 MCP 场景下的落地方式。

三个用途，缺一不可（§21.6）：

1. **保存期校验**：``agent.tools`` 里的 ``mcp__`` 名字要在这张表里查得到；
2. **展开 schema 的数据源**（离线服务器的工具不展开，§21.9）；
3. **后台勾选框的数据源**（``GET /api/tools``）。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import DATETIME3, JSON_COL, PK, Base


class McpTool(Base):
    """一台服务器**已发现**的工具快照。"""

    __tablename__ = "mcp_tool"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    server_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    # ``mcp__<server_code>__<tool_name>``——OpenAI 函数名约束是
    # ``^[a-zA-Z0-9_-]{1,64}$``，没有点号（§21.5）。
    qualified_name: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    # MCP 服务器自己的工具名，原样保留。
    tool_name: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    # 服务器声明的 inputSchema——我们要发给 OpenAI 的 arguments 形状。
    input_schema: Mapped[dict | None] = mapped_column(JSON_COL, default=None)
    output_schema: Mapped[dict | None] = mapped_column(JSON_COL, default=None)
    # readOnlyHint / destructiveHint / …——**不可信数据**，只用于展示（§21.10 第 3 条）。
    annotations: Mapped[dict | None] = mapped_column(JSON_COL, default=None)
    discovered_at: Mapped[object] = mapped_column(
        DATETIME3, server_default=sa.func.now(), nullable=False
    )

    __table_args__ = (
        sa.UniqueConstraint("qualified_name", name="uk_mcp_tool_qualified"),
        sa.Index("idx_mcp_tool_server", "server_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        """调试用摘要。"""
        return f"<McpTool {self.qualified_name}>"
