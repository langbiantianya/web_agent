"""LLM 接口配置数据访问。"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.llm_endpoint import LlmEndpoint


def _llm_sort(sort: str) -> tuple[Any, ...]:
    """把排序字段名翻译成 ORDER BY 片段（白名单，见 agent_repo 的同类函数）。"""
    if sort == "name":
        return (LlmEndpoint.name.asc(), LlmEndpoint.id.asc())
    if sort == "code":
        return (LlmEndpoint.code.asc(), LlmEndpoint.id.asc())
    if sort == "priority":
        return (LlmEndpoint.priority.asc(), LlmEndpoint.id.asc())
    return (LlmEndpoint.priority.asc(), LlmEndpoint.id.asc())


class LlmEndpointRepo:
    """按优先级维护 OpenAI 兼容接口配置。"""

    async def create(self, db: AsyncSession, **kwargs) -> LlmEndpoint:
        """新增配置并提交。"""
        row = LlmEndpoint(**kwargs)
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    async def get_by_code(self, db: AsyncSession, code: str) -> LlmEndpoint | None:
        """按稳定 code 查询单条配置。"""
        return await db.scalar(
            sa.select(LlmEndpoint).where(LlmEndpoint.code == code)
        )

    async def list(
        self,
        db: AsyncSession,
        *,
        only_enabled: bool = False,
        limit: int = 200,
        offset: int = 0,
        search: str | None = None,
        sort: str = "",
    ) -> list[LlmEndpoint]:
        """按 priority、id 排序返回配置。

        ``search`` 按编码或名称做包含匹配，``sort`` 支持
        ``name`` / ``code`` / ``priority``（管理端列表，UX-06）。
        """
        stmt = sa.select(LlmEndpoint)
        if only_enabled:
            stmt = stmt.where(LlmEndpoint.enabled == 1)
        if search:
            pattern = f"%{search}%"
            stmt = stmt.where(
                sa.or_(
                    LlmEndpoint.code.like(pattern),
                    LlmEndpoint.name.like(pattern),
                )
            )
        stmt = stmt.order_by(*_llm_sort(sort)).limit(limit).offset(offset)
        return list(await db.scalars(stmt))

    async def count(
        self,
        db: AsyncSession,
        *,
        search: str | None = None,
    ) -> int:
        """统计符合条件的端点数量（管理端分页用，UX-06）。"""
        stmt = sa.select(sa.func.count(LlmEndpoint.id))
        if search:
            pattern = f"%{search}%"
            stmt = stmt.where(
                sa.or_(
                    LlmEndpoint.code.like(pattern),
                    LlmEndpoint.name.like(pattern),
                )
            )
        return int(await db.scalar(stmt) or 0)

    async def has_any(self, db: AsyncSession) -> bool:
        """数据库里是否至少有一条接口配置，包括停用项。"""
        return await db.scalar(
            sa.select(LlmEndpoint.id).order_by(LlmEndpoint.id.asc()).limit(1)
        ) is not None

    async def update(
        self,
        db: AsyncSession,
        code: str,
        fields: dict,
        *,
        bump_version: bool = True,
    ) -> LlmEndpoint | None:
        """局部更新配置；``None`` 视为不修改。"""
        row = await self.get_by_code(db, code)
        if row is None:
            return None
        for key, value in fields.items():
            if value is not None:
                setattr(row, key, value)
        if bump_version:
            await db.execute(
                sa.update(LlmEndpoint)
                .where(LlmEndpoint.code == code)
                .values(version=LlmEndpoint.version + 1)
            )
        await db.commit()
        await db.refresh(row)
        return row

    async def delete(self, db: AsyncSession, code: str) -> bool:
        """按 code 删除配置。"""
        row = await self.get_by_code(db, code)
        if row is None:
            return False
        await db.delete(row)
        await db.commit()
        return True


llm_endpoint_repo = LlmEndpointRepo()
