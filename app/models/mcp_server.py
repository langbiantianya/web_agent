"""MCP 服务器注册表（设计文档 §21.6，V4）。

一台已注册的 MCP 服务器。``env`` 与 ``headers`` 里装的是凭据，**加密存储、
只写不读**——任何 API 响应里都不回显明文。

本项目是 MCP **客户端**，不做服务器。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import DATETIME3, JSON_COL, PK, Base, long_text

# 传输方式。旧版 HTTP+SSE 传输不实现（2025-03-26 废弃）。
TRANSPORT_STDIO = "stdio"
TRANSPORT_HTTP = "http"

AUTH_NONE = "none"
AUTH_STATIC = "static"
AUTH_OAUTH_CLIENT_CREDENTIALS = "oauth_client_credentials"


class McpServer(Base):
    """一台已注册的 MCP 服务器。"""

    __tablename__ = "mcp_server"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    # 业务主键。**不是** MCP 协议里的 serverInfo.name——后者是服务端自报的，
    # 不同服务器可能重名，不能拿来当主键（§21.5）。
    code: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    tenant_id: Mapped[str] = mapped_column(
        sa.String(64), nullable=False, default="default"
    )
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
    # OAuth 2.0 机器身份。client secret 与 token 全部 Fernet 加密；client_id 和
    # token_endpoint 不是秘密，可回显。
    auth_type: Mapped[str] = mapped_column(
        sa.String(32), nullable=False, default=AUTH_STATIC
    )
    oauth_token_url: Mapped[str | None] = mapped_column(
        sa.String(512), nullable=True
    )
    oauth_revocation_url: Mapped[str | None] = mapped_column(
        sa.String(512), nullable=True
    )
    oauth_client_id: Mapped[str | None] = mapped_column(
        sa.String(256), nullable=True
    )
    oauth_client_secret_cipher: Mapped[str | None] = mapped_column(
        sa.String(4096), nullable=True
    )
    oauth_scopes: Mapped[list | None] = mapped_column(JSON_COL, default=None)
    oauth_resource: Mapped[str | None] = mapped_column(
        sa.String(512), nullable=True
    )
    # 访问令牌与刷新令牌的密文都很长（JWT 动辄上千字符）。两个 VARCHAR(8192)
    # 在 MySQL 的 utf8mb4 下各占 32 KB 行内空间，合起来就顶爆 65535 的行上限，
    # 所以走 long_text：MySQL 落 TEXT（行外存储），其它方言仍是 VARCHAR。
    oauth_access_token_cipher: Mapped[str | None] = mapped_column(
        long_text(8192), nullable=True
    )
    oauth_refresh_token_cipher: Mapped[str | None] = mapped_column(
        long_text(8192), nullable=True
    )
    oauth_expires_at: Mapped[object] = mapped_column(DATETIME3, nullable=True)
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
        sa.Index("idx_mcp_server_tenant_enabled", "tenant_id", "enabled"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        """调试用摘要。刻意不打印 url 里的凭据与 env/headers 的内容。"""
        return f"<McpServer {self.code} {self.transport}>"
