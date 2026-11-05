"""MCP 服务器相关 schema（设计文档 §21.11）。

## 凭据字段是「只写」的

``env`` 与 ``headers`` 里装的就是凭据。请求模型收它们，**响应模型一律不回显
明文**——回显时替换成 ``***``。这与 ``agent.api_key_cipher`` 是同一套口径
（§17.2 那条「密钥边界」）。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.models.mcp_server import TRANSPORT_HTTP, TRANSPORT_STDIO

# code 的格式：与 Agent / Skill 的 code 同一套规矩（小写字母、数字、连字符）。
CODE_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+)*$"


class McpServerBase(BaseModel):
    """MCP 服务器配置的公共字段。"""

    name: str = Field(default="", max_length=128)
    transport: str = Field(pattern="^(stdio|http)$")
    timeout_sec: int = Field(default=30, ge=1, le=600)
    allow_tools: list[str] | None = None
    enabled: bool = True
    auth_type: str = Field(
        default="static", pattern="^(none|static|oauth_client_credentials)$"
    )
    oauth_token_url: str | None = Field(default=None, max_length=512)
    oauth_revocation_url: str | None = Field(default=None, max_length=512)
    oauth_client_id: str | None = Field(default=None, max_length=256)
    oauth_scopes: list[str] | None = None
    oauth_resource: str | None = Field(default=None, max_length=512)


class McpServerCreate(McpServerBase):
    """注册一台 MCP 服务器。

    ``command`` / ``args`` / ``env`` 是 stdio 的，``url`` / ``headers`` 是 http 的
    ——字段与传输方式对不上时由路由层拒（400）。
    """

    code: str = Field(min_length=1, max_length=64, pattern=CODE_PATTERN)
    command: str | None = Field(default=None, max_length=512)
    args: list[str] | None = None
    env: dict[str, str] | None = None
    url: str | None = Field(default=None, max_length=512)
    headers: dict[str, str] | None = None
    oauth_client_secret: str | None = Field(default=None, max_length=4096)


class McpServerUpdate(BaseModel):
    """更新 MCP 服务器。全部可选；没传的字段不动。"""

    name: str | None = Field(default=None, max_length=128)
    timeout_sec: int | None = Field(default=None, ge=1, le=600)
    allow_tools: list[str] | None = None
    enabled: bool | None = None
    command: str | None = Field(default=None, max_length=512)
    args: list[str] | None = None
    env: dict[str, str] | None = None
    url: str | None = Field(default=None, max_length=512)
    headers: dict[str, str] | None = None
    auth_type: str | None = Field(
        default=None, pattern="^(none|static|oauth_client_credentials)$"
    )
    oauth_token_url: str | None = Field(default=None, max_length=512)
    oauth_revocation_url: str | None = Field(default=None, max_length=512)
    oauth_client_id: str | None = Field(default=None, max_length=256)
    oauth_client_secret: str | None = Field(default=None, max_length=4096)
    oauth_scopes: list[str] | None = None
    oauth_resource: str | None = Field(default=None, max_length=512)


class McpServerOut(BaseModel):
    """MCP 服务器的对外形状。

    ``env`` / ``headers`` 的**值一律是 ``***``**——只告诉你「配了哪些键」，
    不告诉你值是什么。
    """

    id: int
    code: str
    tenant_id: str = "default"
    name: str
    transport: str
    command: str | None = None
    args: list[str] | None = None
    env_keys: list[str] = Field(default_factory=list)
    url: str | None = None
    header_keys: list[str] = Field(default_factory=list)
    auth_type: str = "static"
    oauth_token_url: str | None = None
    oauth_revocation_url: str | None = None
    oauth_client_id: str | None = None
    oauth_scopes: list[str] | None = None
    oauth_resource: str | None = None
    oauth_configured: bool = False
    oauth_token_expires_at: datetime | None = None
    allow_tools: list[str] | None = None
    timeout_sec: int = 30
    enabled: bool = True
    version: int = 1
    # 运行期状态。
    online: bool = False
    # online / unavailable_reason 的观察范围。当前为进程实例级，不是集群共识。
    online_scope: str = "instance"
    unavailable_reason: str | None = None
    tool_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class McpToolOut(BaseModel):
    """已发现的 MCP 工具。"""

    qualified_name: str
    tool_name: str
    server: str
    description: str | None = None
    input_schema: dict | None = None
    annotations: dict | None = None
    discovered_at: datetime | None = None


class McpRefreshOut(BaseModel):
    """一次目录刷新的结果。"""

    server: str
    imported: list[str] = Field(default_factory=list)
    skipped: list[dict] = Field(default_factory=list)


class McpResourceOut(BaseModel):
    """MCP 资源或资源模板描述。"""

    uri: str
    name: str = ""
    description: str | None = None
    mime_type: str | None = None
    size: int | None = None


class McpResourceContentOut(BaseModel):
    """一次资源读取返回的内容；二进制以 base64 表示。"""

    uri: str
    mime_type: str | None = None
    text: str | None = None
    blob_base64: str | None = None


class McpPromptOut(BaseModel):
    """MCP prompt 描述。"""

    name: str
    description: str | None = None
    arguments: list[dict] = Field(default_factory=list)


class McpPromptGetOut(BaseModel):
    """获取 prompt 后的消息列表。"""

    description: str | None = None
    messages: list[dict] = Field(default_factory=list)


class McpPromptGetIn(BaseModel):
    """获取 prompt 的可选字符串参数。"""

    arguments: dict[str, str] | None = None


class McpOAuthRevokeOut(BaseModel):
    """OAuth 撤销结果。"""

    server: str
    local_tokens_cleared: bool = True
    remote_revoked: bool = False


__all__ = [
    "McpPromptGetIn",
    "McpPromptGetOut",
    "McpPromptOut",
    "McpOAuthRevokeOut",
    "McpRefreshOut",
    "McpResourceContentOut",
    "McpResourceOut",
    "McpServerCreate",
    "McpServerOut",
    "McpServerUpdate",
    "McpToolOut",
    "TRANSPORT_HTTP",
    "TRANSPORT_STDIO",
]
