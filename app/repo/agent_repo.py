"""Agent 数据访问。"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent


def _agent_sort(sort: str) -> tuple[Any, ...]:
    """把排序字段名翻译成 ORDER BY 片段。

    白名单在**代码里**而不是来自请求：排序字段直接进 SQL 的 ORDER BY，
    放开未知取值等于把 SQL 片段暴露给用户。
    """
    if sort == "name":
        return (Agent.name.asc(), Agent.id.desc())
    if sort == "code":
        return (Agent.code.asc(), Agent.id.desc())
    if sort == "created":
        return (Agent.id.desc(),)
    return (Agent.id.desc(),)


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

    async def get_by_code(
        self,
        db: AsyncSession,
        code: str,
        tenant_id: str | None = None,
    ) -> Agent | None:
        """按业务唯一 code 取 Agent；编排流程与缓存键都以 code 为主键语义。"""
        stmt = sa.select(Agent).where(Agent.code == code)
        if tenant_id is not None:
            stmt = stmt.where(Agent.tenant_id == tenant_id)
        return await db.scalar(stmt)

    async def get_by_id(self, db: AsyncSession, agent_id: int) -> Agent | None:
        """按自增 id 取 Agent，走主键查询。"""
        return await db.get(Agent, agent_id)

    async def get_by_codes(self, db: AsyncSession, codes: list[str]) -> dict[str, Agent]:
        """批量取 Agent 配置，避免逐节点查库。"""
        if not codes:
            return {}
        rows = await db.scalars(sa.select(Agent).where(Agent.code.in_(set(codes))))
        return {a.code: a for a in rows}

    async def count_for_llm_endpoint(
        self, db: AsyncSession, llm_endpoint_code: str
    ) -> int:
        """统计仍引用指定 LLM 连接的 Agent 数量。"""
        count = await db.scalar(
            sa.select(sa.func.count(Agent.id)).where(
                Agent.llm_endpoint_code == llm_endpoint_code
            )
        )
        return int(count or 0)

    async def llm_endpoint_binding_map(
        self, db: AsyncSession, tenant_id: str | None = None
    ) -> dict[str, list[str]]:
        """返回 LLM endpoint code → 绑定它的 Agent code 列表。"""
        stmt = sa.select(Agent.code, Agent.llm_endpoint_code)
        if tenant_id is not None:
            stmt = stmt.where(Agent.tenant_id == tenant_id)
        out: dict[str, list[str]] = {}
        for agent_code, endpoint_code in await db.execute(stmt):
            if isinstance(endpoint_code, str) and endpoint_code:
                out.setdefault(endpoint_code, []).append(agent_code)
        return out

    async def skill_binding_map(
        self, db: AsyncSession, tenant_id: str | None = None
    ) -> dict[str, list[str]]:
        """返回 Skill code → 绑定它的 Agent code 列表。"""
        stmt = sa.select(Agent.code, Agent.skills)
        if tenant_id is not None:
            stmt = stmt.where(Agent.tenant_id == tenant_id)
        out: dict[str, list[str]] = {}
        for code, skills in await db.execute(stmt):
            for skill_code in skills or []:
                if isinstance(skill_code, str):
                    out.setdefault(skill_code, []).append(code)
        return out

    async def tool_binding_map(
        self, db: AsyncSession, tenant_id: str | None = None
    ) -> dict[str, list[str]]:
        """返回工具名 → 绑定它的 Agent code 列表。"""
        stmt = sa.select(Agent.code, Agent.tools)
        if tenant_id is not None:
            stmt = stmt.where(Agent.tenant_id == tenant_id)
        out: dict[str, list[str]] = {}
        for code, tools in await db.execute(stmt):
            for tool_name in tools or []:
                if isinstance(tool_name, str):
                    out.setdefault(tool_name, []).append(code)
        return out

    async def list(
        self,
        db: AsyncSession,
        only_enabled: bool = False,
        limit: int = 200,
        offset: int = 0,
        tenant_id: str | None = None,
        search: str | None = None,
        sort: str = "",
    ) -> list[Agent]:
        """分页列出 Agent，按 id 倒序（新建的排前面）。

        Args:
            only_enabled: 只返回 enabled=1 的配置，避免编排引用到已停用的 Agent。
            limit/offset: 分页窗口，由 API 层的 Query 约束上界。
            search: 按编码或名称做包含匹配（管理端列表搜索，UX-06）。
            sort: 排序字段：``name`` / ``code`` / ``created``；空串为默认。
        """
        stmt = sa.select(Agent)
        if only_enabled:
            stmt = stmt.where(Agent.enabled == 1)
        if tenant_id is not None:
            stmt = stmt.where(Agent.tenant_id == tenant_id)
        if search:
            pattern = f"%{search}%"
            stmt = stmt.where(
                sa.or_(
                    Agent.code.like(pattern),
                    Agent.name.like(pattern),
                )
            )
        stmt = stmt.order_by(*_agent_sort(sort)).limit(limit).offset(offset)
        return list(await db.scalars(stmt))

    async def count(
        self,
        db: AsyncSession,
        *,
        tenant_id: str | None = None,
        search: str | None = None,
    ) -> int:
        """统计符合条件的 Agent 数量（管理端分页用，UX-06）。"""
        stmt = sa.select(sa.func.count(Agent.id))
        if tenant_id is not None:
            stmt = stmt.where(Agent.tenant_id == tenant_id)
        if search:
            pattern = f"%{search}%"
            stmt = stmt.where(
                sa.or_(
                    Agent.code.like(pattern),
                    Agent.name.like(pattern),
                )
            )
        return int(await db.scalar(stmt) or 0)

    async def update(
        self,
        db: AsyncSession,
        code: str,
        fields: dict,
        bump_version: bool = True,
        tenant_id: str | None = None,
    ) -> Agent | None:
        """按 code 局部更新字段（``None`` 值视为「不修改」而被忽略）。

        ``bump_version`` 会让 ``version`` 自增——它是**配置版本号**，随响应返回
        给客户端，客户端据此判断「我手上的配置是不是已经被别人改过了」。

        自增用 SQL 表达式 ``version = version + 1``，而不是「读出来 +1 再写回」：
        后者在并发更新下会丢版本（两个请求都读到 3、都写回 4，其中一个的改动
        就被吞了）。

        置为 False 仅用于不改语义的字段写入。
        """
        agent = await self.get_by_code(db, code, tenant_id)
        if agent is None:
            return None
        for k, v in fields.items():
            if v is not None:
                setattr(agent, k, v)
        if bump_version:
            await db.execute(
                sa.update(Agent)
                .where(
                    Agent.code == code,
                    *(
                        [Agent.tenant_id == tenant_id]
                        if tenant_id is not None
                        else []
                    ),
                )
                .values(version=Agent.version + 1)
            )
        await db.commit()
        await db.refresh(agent)
        return agent

    async def delete(
        self,
        db: AsyncSession,
        code: str,
        tenant_id: str | None = None,
    ) -> bool:
        """按 code 硬删除；不存在时返回 False，由 API 层转成 404。

        注意删除无法用版本号表达失效，调用方需记得清空 Agent 缓存。
        """
        agent = await self.get_by_code(db, code, tenant_id)
        if agent is None:
            return False
        await db.delete(agent)
        await db.commit()
        return True


agent_repo = AgentRepo()
