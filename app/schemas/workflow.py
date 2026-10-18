"""Workflow 相关 schema。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.core.workflow_def import WorkflowDefinition
from app.schemas.common import ORMBase


class WorkflowCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=512)
    definition: WorkflowDefinition
    enabled: bool = True


class WorkflowUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    definition: WorkflowDefinition | None = None
    enabled: bool | None = None


class WorkflowOut(ORMBase):
    id: int
    code: str
    name: str
    description: str
    definition: dict
    enabled: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None
