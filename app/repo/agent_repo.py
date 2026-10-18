"""Agent 数据访问。"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent


class AgentRepo:
    async def create(self, db: AsyncSession, **kwargs) -> Agent:
        """插入新 Agent 并立刻 commit + refresh，使返回对象带齐自增 id 与库端默认值。

        Args:
            db: 由依赖注入拿到的请求级 AsyncSession。
            **kwargs: Agent 表字段（code、system_prompt、api_key_cipher 等）。
        """
        agent = Agent(**kwargs)
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        return agent

    async def get_by_code(self, db: AsyncSession, code: str) -> Agent | None:
        """按业务唯一 code 取 Agent；编排流程与缓存键都以 code 为主键语义。"""
        return await db.scalar(sa.select(Agent).where(Agent.code == code))

    async def get_by_id(self, db: AsyncSession, agent_id: int) -> Agent | None:
        """按自增 id 取 Agent，走主键查询。"""
        return await db.get(Agent, agent_id)

    async def get_by_codes(self, db: AsyncSession, codes: list[str]) -> dict[str, Agent]:
        """批量取 Agent 配置，避免逐节点查库。"""
        if not codes:
            return {}
        rows = await db.scalars(sa.select(Agent).where(Agent.code.in_(set(codes))))
        return {a.code: a for a in rows}

    async def list(
        self,
        db: AsyncSession,
        only_enabled: bool = False,
        limit: int = 200,
        offset: int = 0,
    ) -> list[Agent]:
        """分页列出 Agent，按 id 倒序（新建的排前面）。

        Args:
            only_enabled: 只返回 enabled=1 的配置，避免编排引用到已停用的 Agent。
            limit/offset: 分页窗口，由 API 层的 Query 约束上界。
        """
        stmt = sa.select(Agent)
        if only_enabled:
            stmt = stmt.where(Agent.enabled == 1)
        stmt = stmt.order_by(Agent.id.desc()).limit(limit).offset(offset)
        return list(await db.scalars(stmt))

    async def update(
        self, db: AsyncSession, code: str, fields: dict, bump_version: bool = True
    ) -> Agent | None:
        """按 code 局部更新字段（``None`` 值视为「不修改」而被忽略）。

        ``bump_version`` 是缓存一致性的关键：Agent 缓存键含 version，
        版本号 +1 后旧键自然失效，运行中的 run 也会读到新配置。
        置为 False 仅用于不改语义的字段写入。
        """
        agent = await self.get_by_code(db, code)
        if agent is None:
            return None
        for k, v in fields.items():
            if v is not None:
                setattr(agent, k, v)
        if bump_version:
            agent.version = (agent.version or 0) + 1
        await db.commit()
        await db.refresh(agent)
        return agent

    async def delete(self, db: AsyncSession, code: str) -> bool:
        """按 code 硬删除；不存在时返回 False，由 API 层转成 404。

        注意删除无法用版本号表达失效，调用方需记得清空 Agent 缓存。
        """
        agent = await self.get_by_code(db, code)
        if agent is None:
            return False
        await db.delete(agent)
        await db.commit()
        return True


agent_repo = AgentRepo()
