"""管理员配置 OAuth / OIDC 供应商的 schema。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class OAuthProviderCreate(BaseModel):
    """创建受管 OIDC 供应商。"""

    code: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    display_name: str = Field(min_length=1, max_length=128)
    issuer: str = Field(min_length=8, max_length=512)
    discovery_url: str | None = Field(default=None, max_length=512)
    authorization_endpoint: str | None = Field(default=None, max_length=512)
    token_endpoint: str | None = Field(default=None, max_length=512)
    jwks_uri: str | None = Field(default=None, max_length=512)
    client_id: str = Field(min_length=1, max_length=256)
    client_secret: str | None = Field(default=None, max_length=2048)
    token_auth_method: str = Field(
        default="client_secret_basic",
        pattern=r"^(client_secret_basic|client_secret_post)$",
    )
    scopes: list[str] = Field(
        default_factory=lambda: ["openid", "profile", "email", "phone"],
        max_length=32,
    )
    username_claim: str = Field(default="preferred_username", max_length=64)
    auto_create: bool = True
    enabled: bool = True


class OAuthProviderUpdate(BaseModel):
    """局部更新供应商；未传 client_secret 时保持原密文。"""

    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    issuer: str | None = Field(default=None, min_length=8, max_length=512)
    discovery_url: str | None = Field(default=None, max_length=512)
    authorization_endpoint: str | None = Field(default=None, max_length=512)
    token_endpoint: str | None = Field(default=None, max_length=512)
    jwks_uri: str | None = Field(default=None, max_length=512)
    client_id: str | None = Field(default=None, min_length=1, max_length=256)
    client_secret: str | None = Field(default=None, max_length=2048)
    token_auth_method: str | None = Field(
        default=None,
        pattern=r"^(client_secret_basic|client_secret_post)$",
    )
    scopes: list[str] | None = Field(default=None, max_length=32)
    username_claim: str | None = Field(default=None, max_length=64)
    auto_create: bool | None = None
    enabled: bool | None = None


class OAuthProviderAdminOut(BaseModel):
    """管理员看到的供应商配置，secret 仅以布尔状态表示。"""

    code: str
    display_name: str
    issuer: str
    discovery_url: str | None = None
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None
    jwks_uri: str | None = None
    client_id: str
    has_client_secret: bool
    token_auth_method: str
    scopes: list[str]
    username_claim: str
    auto_create: bool
    enabled: bool
