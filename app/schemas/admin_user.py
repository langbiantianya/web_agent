"""数据库管理员用户 API 模型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class AdminUserCreate(BaseModel):
    """新建管理员用户。"""

    username: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.\-]+$")
    display_name: str = Field(default="", max_length=128)
    tenant_id: str = Field(default="default", min_length=1, max_length=64)
    role: str = Field(default="viewer", pattern=r"^(viewer|operator|admin)$")
    enabled: bool = True


class AdminUserUpdate(BaseModel):
    """局部更新管理员用户。"""

    display_name: str | None = Field(default=None, max_length=128)
    role: str | None = Field(default=None, pattern=r"^(viewer|operator|admin)$")
    enabled: bool | None = None


class AdminUserOut(BaseModel):
    """管理员用户响应；不回显 Key 或其摘要。"""

    id: int
    username: str
    display_name: str
    tenant_id: str
    role: str
    api_key_hint: str
    enabled: bool
    version: int
    last_used_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AdminUserCreatedOut(AdminUserOut):
    """创建或轮换时一次性返回明文 Key。"""

    api_key: str


class AdminKeyRotateOut(BaseModel):
    """轮换后的新 Key。"""

    username: str
    api_key: str
    version: int
