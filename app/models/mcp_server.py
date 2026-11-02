"""MCP 服务器注册表（设计文档 §21.6，V4）。

一台已注册的 MCP 服务器。``env`` 与 ``headers`` 里装的是凭据，**加密存储、
只写不读**——任何 API 响应里都不回显明文。

本项目是 MCP **客户端**，不做服务器。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import DATETIME3, JSON_COL, PK, Base

# 传输方式。旧版 HTTP+SSE 传输不实现（2025-03-26 废弃）。
TRANSPORT_STDIO = "stdio"
TRANSPORT_HTTP = "http"


class McpServer(Base):
    """一台已注册的 MCP 服务器。"""

    __tablename__ = "mcp_server"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    # 业务主键。**不是** MCP 协议里的 serverInfo.name——后者是服务端自报的，
    # 不同服务器可能重名，不能拿来当主键（§21.5）。
    code: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(128), nullable=False, default="")
    transport: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    # --- stdio：启动命令与参数 ---
    command: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    args: Mapped[list | None] = mapped_column(JSON_COL, default=None)
    # 子进程环境变量。**值加密**（与 agent.api_key_cipher 同一套 Fernet）。
    env: Mapped[dict | None] = mapped_column(JSON_COL, default=None)
    # --- http：端点与请求头 ---
    url: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    # 请求头。**值加密**。
    headers: Mapped[dict | None] = mapped_column(JSON_COL, default=None)
    # 工具白名单：只导入这些工具；为空 = 全导。
    allow_tools: Mapped[list | None] = mapped_column(JSON_COL, default=None)
    timeout_sec: Mapped[int] = mapped_column(sa.Integer, default=30)
    enabled: Mapped[bool] = mapped_column(sa.SmallInteger, default=1)
    version: Mapped[int] = mapped_column(sa.Integer, default=1)
    created_at: Mapped[object] = mapped_column(
        DATETIME3, server_default=sa.func.now(), nullable=False
    )
    updated_at: Mapped[object] = mapped_column(
        DATETIME3,
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
        nullable=False,
    )

    __table_args__ = (
        sa.UniqueConstraint("code", name="uq_mcp_server_code"),
        sa.Index("idx_mcp_server_enabled", "enabled"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        """调试用摘要。刻意不打印 url 里的凭据与 env/headers 的内容。"""
        return f"<McpServer {self.code} {self.transport}>"
