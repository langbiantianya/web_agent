"""Skill 相关 schema（设计文档 §22.11）。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.core.skill_pack import MAX_BODY_CHARS

# 技能 code 的格式：与 SKILL.md frontmatter 的 name 规则一致（规范原文）——
# 小写字母 / 数字 / 连字符，不以连字符开头结尾，不连续连字符。
CODE_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+)*$"


class SkillSummaryOut(BaseModel):
    """技能列表项。"""

    code: str
    name: str
    description: str
    origin: str
    version: int
    content_hash: str
    enabled: bool
    allowed_tools: str = ""
    runtime_profile: str = "fast"
    runtime_image: str = ""
    runtime_available: bool = True
    runtime_detail: str = ""
    # 哪些 Agent 绑着它——删技能前得先看一眼这个。
    used_by: list[str] = Field(default_factory=list)


class SkillOut(BaseModel):
    """技能详情。

    刻意**不用** ORM 直出（``from_attributes``）：ORM 上的列名是 ``metadata``
    而属性名是 ``metadata_``（``metadata`` 是 SQLAlchemy 的保留名），走别名
    处理很容易在序列化时踩坑；而且库表列大多可空，逐个兜默认值本来就是这层
    该干的活。转换逻辑放在路由层的 ``_to_out``。
    """

    id: int
    code: str
    name: str
    description: str
    license: str
    compatibility: str
    metadata: dict | None = None
    allowed_tools: str
    body: str
    origin: str
    content_hash: str
    enabled: bool
    version: int
    runtime_profile: str = "fast"
    runtime_image: str = ""
    runtime_available: bool = True
    runtime_detail: str = ""
    import_warnings: list[str] = Field(default_factory=list)
    used_by: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SkillCreate(BaseModel):
    """创建技能。"""

    code: str = Field(min_length=1, max_length=64, pattern=CODE_PATTERN)
    # name 是 frontmatter 的 name，而规范要求 **name 等于目录名**——在本项目里
    # 目录名就是 code。所以传了就必须与 code 一致，不一致直接拒；不传就用 code。
    name: str | None = Field(default=None, max_length=64)
    description: str = Field(min_length=1, max_length=1024)
    license: str = Field(default="", max_length=128)
    compatibility: str = Field(default="", max_length=500)
    metadata: dict | None = None
    allowed_tools: str = Field(default="", max_length=512)
    body: str = Field(default="", max_length=MAX_BODY_CHARS)
    enabled: bool = True


class SkillUpdate(BaseModel):
    """更新技能。全部可选；没传的字段不动。"""

    name: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=1024)
    license: str | None = Field(default=None, max_length=128)
    compatibility: str | None = Field(default=None, max_length=500)
    metadata: dict | None = None
    allowed_tools: str | None = Field(default=None, max_length=512)
    body: str | None = Field(default=None, max_length=MAX_BODY_CHARS)
    enabled: bool | None = None


class SkillResourceOut(BaseModel):
    """资源清单项。"""

    path: str
    size_bytes: int
    mime_type: str
    is_text: bool
    sha256: str


class SkillRevisionOut(BaseModel):
    """变更历史项。"""

    version: int
    author: str
    note: str
    resource_count: int = 0
    created_at: datetime | None = None


class SkillImportEntry(BaseModel):
    """目录结构形式的一个文件。"""

    path: str = Field(min_length=1, max_length=255)
    content_base64: str


class SkillImportIn(BaseModel):
    """目录结构形式的导入（不走 zip 解压，但走**同一套**路径校验）。"""

    entries: list[SkillImportEntry] = Field(min_length=1)
    # 导入的技能默认停用——来源不可信（§22.10）。要启用得管理员显式开。
    enabled: bool = False


class SkillRollbackIn(BaseModel):
    """回滚到指定版本。"""

    version: int = Field(ge=1)


class SkillPromptPreviewOut(BaseModel):
    """生效提示词预览（与实际发给模型的逐字一致）。"""

    agent_code: str
    system_prompt: str
    # 每个技能一条：code / name / version / body / 是否真的注入了。
    skills: list[dict] = Field(default_factory=list)
    # 最终发给模型的 messages（拼好的样子）。
    messages: list[dict] = Field(default_factory=list)
    # 被跳过的技能与原因。
    skipped: list[dict] = Field(default_factory=list)
