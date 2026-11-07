"""后台敏感操作审计写入。"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.admin_control import AdminAuditEvent
from services.admin_web.accounts import AdminPrincipal


def _safe_detail(detail: dict[str, Any] | None) -> dict[str, Any] | None:
    """只保留短标量字段，阻止正文与凭据进入审计 JSON。"""
    if not detail:
        return None
    blocked = {
        "password",
        "password_hash",
        "csrf",
        "csrf_token",
        "session",
        "session_token",
        "email",
        "phone",
        "secret",
        "api_key",
    }
    safe: dict[str, Any] = {}
    for key, value in detail.items():
        if key.lower() in blocked:
            continue
        if isinstance(value, (str, int, bool)) or value is None:
            safe[key] = value
        elif isinstance(value, (list, tuple)):
            safe[key] = [
                item
                for item in value[:100]
                if isinstance(item, (str, int, bool))
            ]
    return safe


async def record_admin_audit(
    db: AsyncSession,
    *,
    actor_kind: str,
    actor_id: int | None,
    actor_username: str,
    action: str,
    target_type: str,
    target_id: str,
    outcome: str = "success",
    tenant_id: str | None = None,
    detail: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> AdminAuditEvent:
    """向当前事务追加一条脱敏审计记录。"""
    row = AdminAuditEvent(
        actor_kind=actor_kind,
        actor_id=actor_id,
        actor_username=actor_username[:64],
        tenant_id=tenant_id,
        action=action[:128],
        target_type=target_type[:64],
        target_id=target_id[:128],
        outcome=outcome[:16],
        detail=_safe_detail(detail),
        trace_id=trace_id[:64] if trace_id else None,
    )
    db.add(row)
    await db.flush()
    return row


async def audit_principal_action(
    db: AsyncSession,
    principal: AdminPrincipal,
    *,
    action: str,
    target_type: str,
    target_id: str,
    outcome: str = "success",
    tenant_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> AdminAuditEvent:
    """从已验证后台主体写入审计记录。"""
    return await record_admin_audit(
        db,
        actor_kind=principal.kind,
        actor_id=principal.principal_id,
        actor_username=principal.username,
        action=action,
        target_type=target_type,
        target_id=target_id,
        outcome=outcome,
        tenant_id=tenant_id,
        detail=detail,
    )
