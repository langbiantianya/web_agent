"""LLM 接口配置的 API schema。"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import ORMBase


def _clean_base_url(value: str) -> str:
    """校验并规范化 OpenAI 兼容接口地址。

    只接受 HTTP(S)，并拒绝把用户名或密码塞进 URL。API Key 必须单独加密存储，
    不能借地址字段绕过密钥边界进入日志。
    """
    value = value.strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base_url 必须是 http(s) 地址")
    if parsed.username or parsed.password:
        raise ValueError("base_url 不能包含用户名或密码，请使用独立的 api_key")
    return value


class LlmEndpointCreate(BaseModel):
    """新增接口配置。"""

    code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")
    name: str = Field(default="", max_length=128)
    base_url: str = Field(min_length=1, max_length=512)
    model: str = Field(default="", max_length=128)
    api_key: str = Field(default="", max_length=512, description="明文仅用于写入")
    priority: int = Field(default=100, ge=0, le=1_000_000)
    enabled: bool = True

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str) -> str:
        """规范化地址并拒绝 URL 内嵌凭据。"""
        return _clean_base_url(value)


class LlmEndpointUpdate(BaseModel):
    """局部更新接口配置；空 ``api_key`` 不覆盖已存密钥。"""

    name: str | None = Field(default=None, max_length=128)
    base_url: str | None = Field(default=None, max_length=512)
    model: str | None = Field(default=None, max_length=128)
    api_key: str | None = Field(default=None, max_length=512)
    priority: int | None = Field(default=None, ge=0, le=1_000_000)
    enabled: bool | None = None

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str | None) -> str | None:
        """有值时按新增接口的规则规范化。"""
        return _clean_base_url(value) if value is not None else None


class LlmEndpointOut(ORMBase):
    """接口配置的读取模型，绝不回显明文或密文 Key。"""

    id: int
    code: str
    name: str
    base_url: str
    model: str
    priority: int
    enabled: bool
    version: int
    has_api_key: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None
