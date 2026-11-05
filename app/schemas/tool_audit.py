"""工具调用审计的管理员查询模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.common import PageMeta


class ToolAuditSummaryOut(BaseModel):
    """列表项只返回元数据，避免一次查询把所有大结果拉进内存。"""

    id: int
    tenant_id: str
    run_id: int
    node_id: str
    tool_call_id: str
    tool_name: str
    source: str
    server: str | None = None
    arguments_redacted: bool = False
    arguments_chars: int = 0
    result_chars: int = 0
    result_bytes: int = 0
    result_truncated: bool = False
    ok: bool
    failure_type: str | None = None
    duration_ms: int = 0
    created_at: datetime | None = None
    expires_at: datetime | None = None


class ToolAuditListOut(PageMeta):
    """分页审计列表。"""

    items: list[ToolAuditSummaryOut] = Field(default_factory=list)


class ToolAuditDetailOut(ToolAuditSummaryOut):
    """单条详情，包含已脱敏参数和受上限约束的结果。"""

    arguments: Any = None
    result: str | None = None
