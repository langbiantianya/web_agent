"""Workflow 数据访问。"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.workflow import Workflow


class WorkflowRepo:
    async def create(self, db: AsyncSession, **kwargs) -> Workflow:
        """插入 Workflow 并 commit + refresh，返回值可直接喂给缓存与响应模型。

        Args:
            db: 依赖注入的请求级 AsyncSession。
            **kwargs: workflow 表字段，其中 definition 已由 API 层校验过图结构。
        """
        wf = Workflow(**kwargs)
        db.add(wf)
        await db.commit()
        await db.refresh(wf)
        return wf

    async def get_by_code(self, db: AsyncSession, code: str) -> Workflow | None:
        """按业务唯一 code 取 Workflow；缓存键也以 code 为准。"""
        return await db.scalar(sa.select(Workflow).where(Workflow.code == code))

    async def get_by_id(self, db: AsyncSession, wf_id: int) -> Workflow | None:
        """按自增 id 取 Workflow（会话绑定的是 workflow_id）。"""
        return await db.get(Workflow, wf_id)

    async def list(
        self, db: AsyncSession, only_enabled: bool = False, limit: int = 200, offset: int = 0
    ) -> list[Workflow]:
        """分页列出 Workflow，按 id 倒序；``only_enabled`` 过滤掉已停用的编排。"""
        stmt = sa.select(Workflow)
        if only_enabled:
            stmt = stmt.where(Workflow.enabled == 1)
        stmt = stmt.order_by(Workflow.id.desc()).limit(limit).offset(offset)
        return list(await db.scalars(stmt))

    async def update(
        self, db: AsyncSession, code: str, fields: dict
    ) -> Workflow | None:
        """按 code 局部更新字段（值为 None 的键跳过）；不存在时返回 None。

        workflow 表没有 version 列，无法靠版本号让缓存自然失效，
        所以调用方必须在提交后显式刷新 Workflow 缓存。
        """
        wf = await self.get_by_code(db, code)
        if wf is None:
            return None
        for k, v in fields.items():
            if v is not None:
                setattr(wf, k, v)
        await db.commit()
        await db.refresh(wf)
        return wf

    async def delete(self, db: AsyncSession, code: str) -> bool:
        """按 code 硬删除 Workflow；不存在返回 False，由 API 层转 404。

        删除后必须 ``workflow_cache.clear()``，否则内存里仍是旧定义。
        """
        wf = await self.get_by_code(db, code)
        if wf is None:
            return False
        await db.delete(wf)
        await db.commit()
        return True


workflow_repo = WorkflowRepo()
