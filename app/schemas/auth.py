"""终端用户认证与 SSE ticket schema。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class AuthProviderOut(BaseModel):
    """公开登录供应商信息，不包含任何 client secret。"""

    code: str
    display_name: str
    kind: str


class OAuthStartIn(BaseModel):
    """发起 OIDC 登录。"""

    return_to: str = Field(default="/", max_length=512)


class OAuthStartOut(BaseModel):
    """浏览器需要跳转的授权地址。"""

    authorization_url: str


class UserOut(BaseModel):
    """当前用户允许回显的本地资料。"""

    username: str
    phone: str | None = None
    phone_verified: bool | None = None
    email: str | None = None
    email_verified: bool | None = None
    status: str


class LogoutOut(BaseModel):
    """登出结果。"""

    ok: bool = True
    revoked_sessions: int = 1


class SSETicketIn(BaseModel):
    """SSE ticket 必须绑定到当前用户拥有的 run。"""

    run_id: int = Field(ge=1, description="绑定到具体 run")


class SSETicketOut(BaseModel):
    ticket: str
    expires_in: int
    stream_base: str = "/api/runs"
