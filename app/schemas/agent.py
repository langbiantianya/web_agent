"""Agent 相关 schema。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import ORMBase, check_json_shape

# system_prompt 的长度上限。提示词是要整段送进模型的：无上限的话，一条 5 MB 的
# system_prompt 会被完整受理——进内存、落库、送 LLM，token 账单按字符走。
MAX_SYSTEM_PROMPT = 32_000


class ToolPolicyIn(BaseModel):
    """工具级权限、并发和超时。"""

    enabled: bool = True
    max_concurrency: int = Field(default=4, ge=1, le=16)
    timeout_sec: float = Field(default=60.0, ge=0.1, le=600.0)


class AgentBase(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=512)
    system_prompt: str = Field(min_length=1, max_length=MAX_SYSTEM_PROMPT)
    model: str = Field(min_length=1, max_length=128)
    # 新部署的地址与 Key 在 llm_endpoint 表统一管理；这两个字段保留给旧客户端
    # 与升级期兼容。两者为空时，运行期只使用数据库接口配置。
    base_url: str = Field(default="", max_length=255)
    api_key: str = Field(
        default="",
        max_length=512,
        description="兼容字段：明文仅在写入时接收并加密，新配置请用 /api/llm-endpoints",
    )
    temperature: float = Field(default=0.70, ge=0.0, le=2.0)
    max_tokens: int = Field(default=2048, ge=1, le=1_000_000)
    # 工具名列表（V3.2）。**不是** OpenAI 的 function schema——填的是注册表里
    # 的工具名，schema 与执行器都由服务端从同一张表取。
    tools: list[str] | None = None
    # 工具级执行策略；未出现的工具沿用系统默认。
    tool_policy: dict[str, ToolPolicyIn] | None = None
    # 绑定的技能 code 列表（V4，§22.5）。技能只贡献**指令正文**，不授予任何
    # 工具权限——能不能调工具仍然只看 tools。
    skills: list[str] | None = None
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
    tool_policy: dict[str, ToolPolicyIn] | None = None
    skills: list[str] | None = None
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
    tool_policy: dict[str, dict] | None = None
    skills: list[str] | None = None
    extra_params: dict | None = None
    enabled: bool
    version: int
    has_api_key: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None
