"""LLM 接口配置数据访问。"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.llm_endpoint import LlmEndpoint


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
    ) -> list[LlmEndpoint]:
        """按 priority、id 排序返回配置。"""
        stmt = sa.select(LlmEndpoint)
        if only_enabled:
            stmt = stmt.where(LlmEndpoint.enabled == 1)
        stmt = (
            stmt.order_by(LlmEndpoint.priority.asc(), LlmEndpoint.id.asc())
            .limit(limit)
            .offset(offset)
        )
        return list(await db.scalars(stmt))

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
