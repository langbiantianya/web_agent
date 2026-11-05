"""工具调用审计数据访问。"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tool_call_audit import ToolCallAudit


def audit_filters(
    *,
    tenant_id: str | None = None,
    run_id: int | None = None,
    node_id: str | None = None,
    tool_name: str | None = None,
    source: str | None = None,
    ok: bool | None = None,
) -> list:
    """构造管理员查询的公共过滤条件。

    Args:
        tenant_id: 限定租户。
        run_id: 限定 run。
        node_id: 限定工作流节点。
        tool_name: 限定工具名。
        source: 限定 builtin / skill / mcp。
        ok: 限定成功或失败。

    Returns:
        可直接展开给 ``where(*conditions)`` 的 SQLAlchemy 条件列表。
    """
    conditions = []
    if tenant_id:
        conditions.append(ToolCallAudit.tenant_id == tenant_id)
    if run_id is not None:
        conditions.append(ToolCallAudit.run_id == run_id)
    if node_id:
        conditions.append(ToolCallAudit.node_id == node_id)
    if tool_name:
        conditions.append(ToolCallAudit.tool_name == tool_name)
    if source:
        conditions.append(ToolCallAudit.source == source)
    if ok is not None:
        conditions.append(ToolCallAudit.ok == (1 if ok else 0))
    return conditions


class ToolAuditRepo:
    """工具审计记录的写入、查询与过期清理。"""

    async def create(self, db: AsyncSession, **kwargs) -> ToolCallAudit:
        """新增一条审计记录并立即提交。

        独立提交确保 run 后续崩溃或事务回滚时，已经发生的工具调用事实仍然可查。
        """
        row = ToolCallAudit(**kwargs)
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    async def get_by_id(
        self, db: AsyncSession, audit_id: int
    ) -> ToolCallAudit | None:
        """按自增 id 查询一条审计详情。"""
        return await db.get(ToolCallAudit, audit_id)

    async def get_for_tenant(
        self, db: AsyncSession, audit_id: int, tenant_id: str
    ) -> ToolCallAudit | None:
        """按 id 与租户读取详情，防止跨租户枚举。"""
        return await db.scalar(
            sa.select(ToolCallAudit).where(
                ToolCallAudit.id == audit_id,
                ToolCallAudit.tenant_id == tenant_id,
            )
        )

    async def list(
        self,
        db: AsyncSession,
        *,
        tenant_id: str | None = None,
        run_id: int | None = None,
        node_id: str | None = None,
        tool_name: str | None = None,
        source: str | None = None,
        ok: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[ToolCallAudit], int]:
        """按条件分页查询，返回当前页与总条数。"""
        conditions = audit_filters(
            tenant_id=tenant_id,
            run_id=run_id,
            node_id=node_id,
            tool_name=tool_name,
            source=source,
            ok=ok,
        )
        count_stmt = (
            sa.select(sa.func.count())
            .select_from(ToolCallAudit)
            .where(*conditions)
        )
        total = int(await db.scalar(count_stmt) or 0)
        stmt = (
            sa.select(ToolCallAudit)
            .where(*conditions)
            .order_by(ToolCallAudit.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(await db.scalars(stmt)), total

    async def purge_expired(
        self,
        db: AsyncSession,
        *,
        now: datetime,
        limit: int,
    ) -> int:
        """删除一批已过期记录，限制单次删除量以保护小内存部署。

        Args:
            db: 独立清理会话。
            now: UTC 裸时间，与表中的 ``expires_at`` 同一口径。
            limit: 单次最多处理的记录数。

        Returns:
            实际删除条数。
        """
        ids = list(
            await db.scalars(
                sa.select(ToolCallAudit.id)
                .where(ToolCallAudit.expires_at <= now)
                .order_by(ToolCallAudit.id.asc())
                .limit(limit)
            )
        )
        if not ids:
            return 0
        result = await db.execute(
            sa.delete(ToolCallAudit).where(ToolCallAudit.id.in_(ids))
        )
        await db.commit()
        return int(result.rowcount or 0)


tool_audit_repo = ToolAuditRepo()
