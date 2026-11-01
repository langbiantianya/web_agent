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
        self, db: AsyncSession, code: str, fields: dict, bump_version: bool = True
    ) -> Workflow | None:
        """按 code 局部更新字段（值为 None 的键跳过）；不存在时返回 None。

        ``bump_version=True``（默认）时顺手让 ``version+1``。``version`` 是**配置
        版本号**：它随响应返回给客户端，客户端据此判断「我手上的配置是不是已经
        被别人改过了」。自增用 SQL 表达式 ``version = version + 1``，而不是
        「读出来 +1 再写回」——后者在并发更新下会丢版本：两个请求都读到 3，
        都写回 4，其中一个的改动就被吞了。

        Args:
            db: 请求级 AsyncSession。
            code: 业务唯一 code。
            fields: 要改的字段；值为 ``None`` 的键会被跳过（区分「不传」与「清空」）。
            bump_version: 是否自增版本号。
        """
        wf = await self.get_by_code(db, code)
        if wf is None:
            return None
        for k, v in fields.items():
            if v is not None:
                setattr(wf, k, v)
        if bump_version:
            # 原子自增（见 docstring）。用 SQL 表达式而不是 ORM 属性赋值。
            await db.execute(
                sa.update(Workflow)
                .where(Workflow.code == code)
                .values(version=Workflow.version + 1)
            )
        await db.commit()
        await db.refresh(wf)
        return wf

    async def delete(self, db: AsyncSession, code: str) -> bool:
        """按 code 硬删除 Workflow；不存在返回 False，由 API 层转 404。

        删除无法用版本号表达失效——行都没了，谈不上版本。但删掉的配置可能被
        正在跑的 run 引用（run 启动时已把 definition 快照进内存，不受影响），
        后续发消息会以「Workflow 不存在或已停用」被拒（409）。
        """
        wf = await self.get_by_code(db, code)
        if wf is None:
            return False
        await db.delete(wf)
        await db.commit()
        return True


workflow_repo = WorkflowRepo()
