"""Agent 相关 schema。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMBase


class AgentBase(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=512)
    system_prompt: str = Field(min_length=1)
    model: str = Field(min_length=1, max_length=128)
    base_url: str = Field(min_length=1, max_length=255)
    api_key: str = Field(default="", description="明文 api_key，落库前加密")
    temperature: float = Field(default=0.70, ge=0.0, le=2.0)
    max_tokens: int = Field(default=2048, ge=1, le=1_000_000)
    # 工具名列表（V3.2）。**不是** OpenAI 的 function schema——填的是注册表里
    # 的工具名，schema 与执行器都由服务端从同一张表取。
    tools: list[str] | None = None
    extra_params: dict | None = None
    enabled: bool = True


class AgentCreate(AgentBase):
    code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")


class AgentUpdate(BaseModel):
    """全部可选；传入 api_key 才会覆盖密文。version 自动 +1。"""

    name: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    system_prompt: str | None = None
    model: str | None = Field(default=None, max_length=128)
    base_url: str | None = Field(default=None, max_length=255)
    api_key: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1, le=1_000_000)
    tools: list[str] | None = None
    extra_params: dict | None = None
    enabled: bool | None = None


class AgentOut(ORMBase):
    id: int
    code: str
    name: str
    description: str
    system_prompt: str
    model: str
    base_url: str
    temperature: float
    max_tokens: int
    tools: list[str] | None = None
    extra_params: dict | None = None
    enabled: bool
    version: int
    has_api_key: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None
