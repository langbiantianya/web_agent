"""工具调用审计管理员查询路由。"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import AdminDep, DbSession
from app.core.errors import NotFoundError
from app.repo.tool_audit_repo import tool_audit_repo
from app.schemas.tool_audit import (
    ToolAuditDetailOut,
    ToolAuditListOut,
    ToolAuditSummaryOut,
)

router = APIRouter(prefix="/api/tool-audits", tags=["tool-audits"])


def _to_summary(row) -> ToolAuditSummaryOut:
    """把 ORM 审计行转成不含正文的列表项。"""
    return ToolAuditSummaryOut(
        id=row.id,
        tenant_id=row.tenant_id,
        run_id=row.run_id,
        node_id=row.node_id,
        tool_call_id=row.tool_call_id,
        tool_name=row.tool_name,
        source=row.source,
        server=row.server,
        arguments_redacted=bool(row.arguments_redacted),
        arguments_chars=row.arguments_chars,
        result_chars=row.result_chars,
        result_bytes=row.result_bytes,
        result_truncated=bool(row.result_truncated),
        ok=bool(row.ok),
        failure_type=row.failure_type,
        duration_ms=row.duration_ms,
        created_at=row.created_at,
        expires_at=row.expires_at,
    )


def _to_detail(row) -> ToolAuditDetailOut:
    """把 ORM 审计行转成单条详情，正文已在写入时脱敏。"""
    return ToolAuditDetailOut(
        **_to_summary(row).model_dump(),
        arguments=row.arguments_json,
        result=row.result_text,
    )


@router.get("", response_model=ToolAuditListOut)
async def list_tool_audits(
    db: DbSession,
    _admin: AdminDep,
    run_id: int | None = Query(None, ge=1),
    node_id: str | None = Query(None, max_length=64),
    tool_name: str | None = Query(None, max_length=255),
    source: str | None = Query(None, pattern=r"^(builtin|skill|mcp)$"),
    ok: bool | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> ToolAuditListOut:
    """分页查询脱敏工具审计摘要，按最新记录优先。"""
    rows, total = await tool_audit_repo.list(
        db,
        tenant_id=_admin.tenant_id,
        run_id=run_id,
        node_id=node_id,
        tool_name=tool_name,
        source=source,
        ok=ok,
        limit=limit,
        offset=offset,
    )
    return ToolAuditListOut(
        total=total,
        limit=limit,
        offset=offset,
        items=[_to_summary(row) for row in rows],
    )


@router.get("/{audit_id}", response_model=ToolAuditDetailOut)
async def get_tool_audit(
    audit_id: int,
    db: DbSession,
    _admin: AdminDep,
) -> ToolAuditDetailOut:
    """查询单条工具审计，包括脱敏参数和受上限约束的结果。"""
    row = await tool_audit_repo.get_for_tenant(db, audit_id, _admin.tenant_id)
    if row is None:
        raise NotFoundError(f"工具审计不存在：{audit_id}")
    return _to_detail(row)
