"""Agent 相关 schema。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import ORMBase, check_json_shape

# system_prompt 的长度上限。提示词是要整段送进模型的：无上限的话，一条 5 MB 的
# system_prompt 会被完整受理——进内存、落库、送 LLM，token 账单按字符走。
MAX_SYSTEM_PROMPT = 32_000


class AgentBase(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=512)
    system_prompt: str = Field(min_length=1, max_length=MAX_SYSTEM_PROMPT)
    model: str = Field(min_length=1, max_length=128)
    base_url: str = Field(min_length=1, max_length=255)
    api_key: str = Field(default="", max_length=512, description="明文 api_key，落库前加密")
    temperature: float = Field(default=0.70, ge=0.0, le=2.0)
    max_tokens: int = Field(default=2048, ge=1, le=1_000_000)
    # 工具名列表（V3.2）。**不是** OpenAI 的 function schema——填的是注册表里
    # 的工具名，schema 与执行器都由服务端从同一张表取。
    tools: list[str] | None = None
    extra_params: dict | None = None
    enabled: bool = True

    @field_validator("extra_params")
    @classmethod
    def _check_extra_params(cls, v: dict | None) -> dict | None:
        """JSON 字段的深度与规模守卫。

        没有这道守卫时，一个 3000 层嵌套的 ``extra_params`` 会让 pydantic 序列化
        时抛 "Circular reference detected (depth exceeded)"——那是**未捕获**的
        异常，于是接口返回 500，而不是「参数不合法」该有的 400。
        """
        return check_json_shape(v)


class AgentCreate(AgentBase):
    code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")


class AgentUpdate(BaseModel):
    """全部可选；传入 api_key 才会覆盖密文。version 自动 +1。"""

    name: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    system_prompt: str | None = Field(default=None, max_length=MAX_SYSTEM_PROMPT)
    model: str | None = Field(default=None, max_length=128)
    base_url: str | None = Field(default=None, max_length=255)
    api_key: str | None = Field(default=None, max_length=512)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1, le=1_000_000)
    tools: list[str] | None = None
    extra_params: dict | None = None
    enabled: bool | None = None

    @field_validator("extra_params")
    @classmethod
    def _check_extra_params(cls, v: dict | None) -> dict | None:
        """JSON 字段的深度与规模守卫（理由见 :meth:`AgentBase._check_extra_params`）。"""
        return check_json_shape(v)


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
