"""请求/响应模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ORMBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class PageMeta(BaseModel):
    total: int = 0
    limit: int = 50
    offset: int = 0


class HealthOut(BaseModel):
    status: str = "ok"
    app: str
    db: str
    # Redis 依赖状态：ok / error / disabled（未启用）。
    redis: str = "disabled"
    active_runs: int = 0
    version: str = ""


class ErrorOut(BaseModel):
    code: str
    message: str


Any_ = Any
